"""
Template para Patreon.

Autenticación: Playwright (browser visible) → session_id cookie persistida
               en ~/.cherry-dl/session.json bajo la clave 'patreon'.
               Solo se ejecuta la primera vez o al expirar (30 días).

API: endpoints internos JSON:API de www.patreon.com/api.
     - GET /api/campaigns?filter[vanity]=<username>  → campaign_id
     - GET /api/posts?filter[campaign_id]=<id>&...   → posts paginados

Paginación: cursor-based via links.next en cada respuesta.

Workers: máximo 2 (max_workers=2). Patreon penaliza con 429 con >2 workers.

Dedup: url_source = "patreon://media/{id}" o "patreon://attachment/{id}"
       IDs estables de la API — no contienen tokens que expiran.
       Primera descarga: full (sin hash pre-check, Patreon no lo expone).
       Updates: url_exists(dedup_key) → skip O(1) via índice en catalog.db.

Tipos descargados:
  - included[type=media]      → imágenes, video, audio subidos a Patreon
  - included[type=attachment] → PSD, ZIP, MP4, archivos fuente, etc.
  Excluidos: post_type text_only / video_external_link / link (sin archivos).
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from ..auth.patreon import (
    COOKIES_TO_KEEP,
    ensure_patreon_session,
    refresh_patreon_cookies,
)
from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc

# ── Constantes ─────────────────────────────────────────────────────────────────

_BASE = "https://www.patreon.com"
_API  = "https://www.patreon.com/api"

# Formatos de URL aceptados:
#   https://www.patreon.com/YoruichiArt
#   https://www.patreon.com/c/YoruichiArt
#   https://www.patreon.com/c/YoruichiArt/posts
#   https://patreon.com/YoruichiArt
_URL_RE = re.compile(
    r"https?://(?:www\.)?patreon\.com/(?:[a-z]{1,4}/)?([^/?#]+?)(?:/posts)?/?$",
    re.IGNORECASE,
)

# Posts por página (máximo práctico sin activar rate limit)
_PAGE_SIZE = 12

# Headers que imitan un navegador real para no levantar sospechas
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.patreon.com/",
    "Origin": "https://www.patreon.com",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# Posts con estos tipos no contienen archivos descargables
_SKIP_POST_TYPES = frozenset({"text_only", "video_external_link", "link"})

# Prefijos de URI canónica para dedup en catalog.db
_URI_MEDIA      = "patreon://media/{}"
_URI_ATTACHMENT = "patreon://attachment/{}"

# Jitter entre requests a la API (segundos)
_DELAY_MIN = 1.0
_DELAY_MAX = 2.5


# ── Template ───────────────────────────────────────────────────────────────────

class PatreonTemplate(SiteTemplate):
    """
    Template de descarga para Patreon.

    max_workers=2 es un límite rígido. Si el perfil tiene workers=5,
    el TUI lo capará a 2 al detectar este atributo.
    """

    name        = "patreon"
    base_url    = _BASE
    workers     = 2   # valor por defecto sugerido al crear perfil
    max_workers = 2   # límite absoluto — el TUI lo respeta

    # Cliente HTTP propio, separado del cliente del engine (que usa headers DDG
    # de Kemono). Se inicializa en la primera llamada a _get_client() y se
    # cierra en el finally de iter_files().
    _http: httpx.AsyncClient | None = None
    _session_cookies: dict[str, str] = {}

    # ── Autenticación ──────────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """Retorna (o crea) el cliente HTTP autenticado con Patreon."""
        if self._http is not None:
            return self._http

        self._session_cookies = await ensure_patreon_session()
        self._http = httpx.AsyncClient(
            headers=_BASE_HEADERS,
            cookies=self._session_cookies,
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=30, read=60, write=30, pool=30,
            ),
        )
        return self._http

    async def _close_client(self) -> None:
        """Cierra el cliente HTTP y persiste cookies actualizadas."""
        if self._http is None:
            return
        updated = {
            c.name: c.value
            for c in self._http.cookies.jar
            if c.name in COOKIES_TO_KEEP
        }
        if updated:
            refresh_patreon_cookies(updated)
        await self._http.aclose()
        self._http = None

    # ── Detección ──────────────────────────────────────────────────────────────

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_URL_RE.match(url))

    # ── Info del artista ───────────────────────────────────────────────────────

    async def get_artist_info(self, url: str) -> ArtistInfo:
        m = _URL_RE.match(url)
        if not m:
            raise ValueError(
                f"URL no reconocida por PatreonTemplate: {url}"
            )
        username = m.group(1)
        campaign_id, display_name = await self._resolve_campaign(username)
        return ArtistInfo(
            artist_id=campaign_id,
            name=display_name,
            service="patreon",
            site=self.name,
            url=url,
        )

    # ── Iteración de archivos ──────────────────────────────────────────────────

    async def iter_files(
        self,
        artist: ArtistInfo,
        since: datetime | None = None,
    ) -> AsyncIterator[FileInfo]:
        """
        Itera todos los archivos del artista de más nuevo a más viejo.

        Paginación cursor-based: sigue links.next hasta que no haya más.
        Si `since` está definido, para cuando encuentra un post publicado
        antes de esa fecha.
        """
        try:
            async for post_data, included_map in self._iter_posts(
                artist.artist_id, since=since
            ):
                for fi in _extract_files_from_post(
                    post_data, included_map, artist
                ):
                    yield fi
        finally:
            await self._close_client()

    # ── Internos ───────────────────────────────────────────────────────────────

    async def _resolve_campaign(
        self, username: str
    ) -> tuple[str, str]:
        """
        Resuelve un username de Patreon a (campaign_id, display_name).
        Lanza ValueError si el creador no existe en la API.
        Lanza RuntimeError si la sesión expiró (401).
        """
        client = await self._get_client()
        params = urlencode({
            "filter[vanity]": username,
            "fields[campaign]": "id,name,url",
        })
        url = f"{_API}/campaigns?{params}"

        await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        resp = await client.get(url)

        if resp.status_code == 401:
            from ..auth.patreon import clear_patreon_session
            clear_patreon_session()
            raise RuntimeError(
                "Sesión de Patreon expirada. "
                "Reinicia cherry-dl para re-autenticarte."
            )

        resp.raise_for_status()
        data = resp.json()

        campaigns = data.get("data", [])
        if not campaigns:
            raise ValueError(
                f"Creador '{username}' no encontrado en Patreon."
            )

        campaign   = campaigns[0]
        campaign_id = str(campaign["id"])
        attrs       = campaign.get("attributes", {})
        display_name = attrs.get("name") or username
        return campaign_id, display_name

    async def _iter_posts(
        self,
        campaign_id: str,
        since: datetime | None = None,
    ) -> AsyncIterator[tuple[dict, dict]]:
        """
        Genera (post_data, included_map) para cada post del creador.

        included_map: {(type, id): attributes} — lookup O(1) en
        _extract_files_from_post() para resolver media y attachments.

        Manejo de errores:
          429 → espera Retry-After (o 60 s) y reintenta la misma página.
          401/403 → RuntimeError con instrucción de re-login.
        """
        client = await self._get_client()
        cursor: str | None = None

        while True:
            params: dict = {
                "filter[campaign_id]": campaign_id,
                "page[count]":         str(_PAGE_SIZE),
                "sort":                "-published_at",
                "include":             "attachments,images,audio,video",
                "fields[post]": (
                    "id,title,published_at,post_type,post_metadata"
                ),
                "fields[media]": (
                    "id,file_name,size_bytes,download_url,state"
                ),
                "fields[attachment]": "id,name,url",
            }
            if cursor:
                params["page[cursor]"] = cursor

            api_url = f"{_API}/posts?{urlencode(params)}"
            await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))

            try:
                resp = await client.get(api_url)
            except httpx.RequestError as exc:
                raise RuntimeError(
                    f"Error de red al paginar posts de Patreon: {exc}"
                )

            # Rate limit — esperar y reintentar la misma página
            if resp.status_code == 429:
                wait = int(resp.headers.get("retry-after", 60))
                await asyncio.sleep(wait)
                continue

            if resp.status_code in (401, 403):
                from ..auth.patreon import clear_patreon_session
                clear_patreon_session()
                raise RuntimeError(
                    "Sesión de Patreon sin acceso (401/403). "
                    "Reinicia cherry-dl para re-autenticarte."
                )

            resp.raise_for_status()
            body = resp.json()

            # Construir mapa de included: (type, id) → attributes
            included_map: dict[tuple[str, str], dict] = {}
            for item in body.get("included", []):
                key = (item["type"], str(item["id"]))
                included_map[key] = item.get("attributes", {})

            posts = body.get("data", [])
            if not posts:
                break

            for post in posts:
                if since is not None:
                    pub = parse_date_utc(
                        post.get("attributes", {}).get("published_at", "")
                    )
                    if pub is not None and pub < since:
                        return  # posts sorted newest-first → parar
                yield post, included_map

            # Seguir cursor de la próxima página
            next_url = body.get("links", {}).get("next")
            if not next_url:
                break

            qs = parse_qs(urlparse(next_url).query)
            cursor_list = qs.get("page[cursor]")
            if not cursor_list:
                break
            cursor = cursor_list[0]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_files_from_post(
    post: dict,
    included_map: dict[tuple[str, str], dict],
    artist: ArtistInfo,
) -> list[FileInfo]:
    """
    Extrae todos los FileInfo descargables de un post.

    Fuentes:
      1. included[type=media]      → imágenes/video/audio subidos directamente
      2. included[type=attachment] → adjuntos (PSD, ZIP, MP4, archivos fuente)

    Post_types ignorados: text_only, video_external_link, link.
    Media con state != "ready" se omite (aún procesándose en el CDN).
    """
    post_id   = str(post.get("id", ""))
    attrs     = post.get("attributes", {})
    post_type = attrs.get("post_type", "")
    title     = attrs.get("title", "")
    published = attrs.get("published_at", "")

    if post_type in _SKIP_POST_TYPES:
        return []

    files: list[FileInfo] = []
    # Previene duplicados dentro del mismo post (mismo CDN URL en dos relaciones)
    seen_cdn_urls: set[str] = set()

    rels = post.get("relationships", {})

    # ── 1. Media embebida ──────────────────────────────────────────────────────
    for rel_item in rels.get("images", {}).get("data", []):
        media_id    = str(rel_item.get("id", ""))
        media_attrs = included_map.get(("media", media_id), {})

        if not media_attrs:
            continue
        if media_attrs.get("state") != "ready":
            continue  # CDN aún procesando el archivo

        cdn_url = media_attrs.get("download_url", "")
        if not cdn_url or cdn_url in seen_cdn_urls:
            continue
        seen_cdn_urls.add(cdn_url)

        filename = _safe_filename(
            media_attrs.get("file_name") or f"media_{media_id}"
        )
        files.append(FileInfo(
            url          = cdn_url,
            url_source   = _URI_MEDIA.format(media_id),
            filename     = filename,
            artist_id    = artist.artist_id,
            artist_name  = artist.name,
            post_id      = post_id,
            post_title   = title,
            date_published = published,
        ))

    # ── 2. Adjuntos (PSD, ZIP, MP4, archivos fuente, etc.) ────────────────────
    for rel_item in rels.get("attachments", {}).get("data", []):
        att_id    = str(rel_item.get("id", ""))
        att_attrs = included_map.get(("attachment", att_id), {})

        if not att_attrs:
            continue

        att_url = att_attrs.get("url", "")
        if not att_url or att_url in seen_cdn_urls:
            continue
        seen_cdn_urls.add(att_url)

        filename = _safe_filename(
            att_attrs.get("name") or f"attachment_{att_id}"
        )
        files.append(FileInfo(
            url          = att_url,
            url_source   = _URI_ATTACHMENT.format(att_id),
            filename     = filename,
            artist_id    = artist.artist_id,
            artist_name  = artist.name,
            post_id      = post_id,
            post_title   = title,
            date_published = published,
        ))

    return files


def _safe_filename(name: str) -> str:
    """Sanitiza nombre de archivo eliminando caracteres inválidos."""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "file"
