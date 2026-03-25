"""
Template para Pixiv (pixiv.net).

API: Web AJAX API de www.pixiv.net — mismos endpoints que usa gallery-dl.
     Estable desde 2018, no requiere OAuth ni tokens externos.

Autenticación:
  Cookie PHPSESSID leída del browser del sistema via browser_cookie3.
  Mismo flujo que PatreonTemplate: sin contraseñas, sin tokens manuales.
  Primera vez → PixivAuthModal (abrir browser → iniciar sesión → confirmar).
  Sesión guardada 30 días en ~/.cherry-dl/session.json.

Endpoints utilizados:
  GET /ajax/user/{id}                    → nombre del artista
  GET /ajax/user/{id}/profile/all        → lista de todos los IDs de obras
  GET /ajax/user/{id}/illusts?ids[]=...  → metadatos por lote (illustType, pageCount)
  GET /ajax/illust/{id}/pages            → URLs originales (todas las obras, 1 o N págs)
  GET /ajax/illust/{id}/ugoira_meta      → URL del ZIP de frames

Nota: el endpoint /illusts?ids[]=... NO incluye urls.original en su respuesta.
  Se llama /pages para cada obra individualmente para obtener la URL original.

Headers obligatorios:
  - Cookie: PHPSESSID=xxx  (sesión del usuario)
  - Referer: https://www.pixiv.net/  (requerido en AJAX y en descargas CDN)
  - x-requested-with: XMLHttpRequest  (evita redirects a login)

Tipos soportados:
  illustType 0 — ilustración (1 pág o multi-pág)
  illustType 1 — manga (siempre multi-pág)
  illustType 2 — ugoira (animación ZIP)

Tipos NO soportados:
  novels — texto, fuera del scope

Dedup via url_source:
  "pixiv://illust/{illust_id}/p{page}"  → ilustraciones y manga
  "pixiv://ugoira/{illust_id}"          → animaciones ZIP

Rate limiting:
  max_workers = 2 (Pixiv aplica 429 agresivo con más concurrencia)
  Jitter 0.5–1.5 s entre requests a la API.

Resolución:
  Siempre se descarga la imagen original (img-original), nunca previews.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from ..auth.pixiv import (
    COOKIES_TO_KEEP,
    NeedsPixivAuth,
    ensure_pixiv_session,
    save_pixiv_cookies,
)
from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc

# ── Constantes ─────────────────────────────────────────────────────────────────

_BASE    = "https://www.pixiv.net"
_AJAX    = f"{_BASE}/ajax"
_CDN     = "https://i.pximg.net"

# Tamaño de lote para el endpoint de detalles
_BATCH_SIZE = 48

# Headers que imitan un browser haciendo peticiones AJAX
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json",
    "Accept-Language":  "en-US,en;q=0.9",
    "Accept-Encoding":  "gzip, deflate, br",
    "Referer":          "https://www.pixiv.net/",
    "x-requested-with": "XMLHttpRequest",
}

# Headers para descargas desde i.pximg.net (CDN de imágenes)
# El Referer es OBLIGATORIO — sin él el CDN devuelve 403
_DOWNLOAD_HEADERS = {
    "Referer":    "https://www.pixiv.net/",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

# Formatos de URL aceptados:
#   https://www.pixiv.net/en/users/12345
#   https://www.pixiv.net/users/12345
#   https://pixiv.net/users/12345
#   https://www.pixiv.net/en/users/12345/illustrations
#   https://www.pixiv.net/member.php?id=12345  (formato antiguo)
_URL_RE = re.compile(
    r"https?://(?:www\.)?pixiv\.net"
    r"(?:/en)?"
    r"(?:/users/(\d+)|/member\.php\?(?:.*&)?id=(\d+))",
    re.IGNORECASE,
)

# URI canónicas para dedup en catalog.db
_URI_ILLUST = "pixiv://illust/{}/p{}"
_URI_UGOIRA = "pixiv://ugoira/{}"

# Jitter entre requests a la API
_DELAY_MIN = 0.5
_DELAY_MAX = 1.5


# ── Template ───────────────────────────────────────────────────────────────────

class PixivTemplate(SiteTemplate):
    """
    Template de descarga para Pixiv usando la web AJAX API.

    No requiere OAuth, tokens externos ni instalaciones adicionales.
    Usa la cookie PHPSESSID del browser del sistema (browser_cookie3).

    max_workers = 2: Pixiv aplica rate limit agresivo con más workers.
    provides_file_hashes = False: Pixiv no expone SHA-256 en URLs.
    """

    name                 = "pixiv"
    base_url             = _BASE
    workers              = 2
    max_workers          = 2
    provides_file_hashes = False

    # Cliente HTTP propio con cookies de sesión.
    # Las descargas desde i.pximg.net van por el engine del proyecto,
    # con los extra_headers (Referer) inyectados en cada FileInfo.
    _http: httpx.AsyncClient | None = None

    # ── Detección ──────────────────────────────────────────────────────────────

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(_URL_RE.search(url))

    # ── Cliente HTTP autenticado ───────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        """Crea (o retorna) el cliente HTTP con las cookies de Pixiv."""
        if self._http is not None:
            return self._http

        cookies = await ensure_pixiv_session()

        self._http = httpx.AsyncClient(
            headers=_BASE_HEADERS,
            cookies=cookies,
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=30, read=60, write=30, pool=30,
            ),
        )
        return self._http

    async def _close_client(self) -> None:
        """Cierra el cliente y persiste cookies actualizadas."""
        if self._http is None:
            return
        updated = {
            c.name: c.value
            for c in self._http.cookies.jar
            if c.name in COOKIES_TO_KEEP
        }
        if updated:
            save_pixiv_cookies(updated)
        await self._http.aclose()
        self._http = None

    # ── Info del artista ───────────────────────────────────────────────────────

    async def get_artist_info(self, url: str) -> ArtistInfo:
        """
        Extrae el user_id de la URL y consulta /ajax/user/{id}.
        Lanza NeedsPixivAuth si no hay sesión guardada en el browser.
        """
        user_id = _extract_user_id(url)
        if not user_id:
            raise ValueError(f"URL no reconocida por PixivTemplate: {url}")

        client   = await self._get_client()
        api_url  = f"{_AJAX}/user/{user_id}?lang=en"

        await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        resp = await client.get(api_url)
        _check_response(resp)

        body = resp.json()
        if body.get("error"):
            raise ValueError(
                f"Pixiv error al obtener artista {user_id}: "
                f"{body.get('message', 'desconocido')}"
            )

        user = body.get("body", {})
        name = user.get("name") or user.get("userId") or user_id

        return ArtistInfo(
            artist_id=user_id,
            name=name,
            service="pixiv",
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
        Itera todas las obras del artista: ilustraciones, manga y ugoiras.

        Flujo:
          1. /ajax/user/{id}/profile/all  → lista de todos los IDs
          2. Por lotes de 48: /ajax/user/{id}/illusts?ids[]=... → detalles
          3. Si illustType == 2: llamada extra a /ajax/illust/{id}/ugoira_meta
          4. Otros: llamada extra a /ajax/illust/{id}/pages

        Si `since` está definido, omite obras con createDate anterior a esa
        fecha (sin hacer las llamadas extra de /pages o /ugoira_meta).
        """
        try:
            async for fi in self._iter_all(artist, since=since):
                yield fi
        finally:
            await self._close_client()

    # ── Internos ───────────────────────────────────────────────────────────────

    async def _iter_all(
        self,
        artist: ArtistInfo,
        since: datetime | None = None,
    ) -> AsyncIterator[FileInfo]:
        """Obtiene la lista completa de IDs y los procesa en lotes."""
        client = await self._get_client()

        # Paso 1 — obtener todos los IDs de ilustraciones y manga
        await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        profile_url = (
            f"{_AJAX}/user/{artist.artist_id}/profile/all?lang=en"
        )
        resp = await client.get(profile_url)
        _check_response(resp)

        body    = resp.json()
        profile = body.get("body", {})

        # El dict tiene claves "illusts" y "manga" con {id: null, ...}
        all_ids: list[str] = []
        for section in ("illusts", "manga"):
            section_ids = profile.get(section) or {}
            if isinstance(section_ids, dict):
                all_ids.extend(section_ids.keys())

        if not all_ids:
            return

        # Paso 2 — procesar por lotes
        for batch in _chunked(all_ids, _BATCH_SIZE):
            async for fi in self._process_batch(
                client, batch, artist, since=since
            ):
                yield fi

    async def _process_batch(
        self,
        client: httpx.AsyncClient,
        ids: list[str],
        artist: ArtistInfo,
        since: datetime | None = None,
    ) -> AsyncIterator[FileInfo]:
        """
        Solicita metadatos para un lote de IDs (illustType, pageCount).
        Para cada obra llama /pages (o /ugoira_meta) para obtener la URL original.

        Si `since` está definido, omite obras con createDate anterior sin
        hacer las llamadas extra de /pages o /ugoira_meta.

        GET /ajax/user/{uid}/illusts?ids[]=id1&ids[]=id2&...&lang=en
        Estructura de respuesta: {"body": {"id": {...work...}}}  — IDs en body directo.
        """
        params = "&".join(f"ids[]={i}" for i in ids)
        url    = (
            f"{_AJAX}/user/{artist.artist_id}/illusts"
            f"?{params}&lang=en"
        )

        await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        resp = await client.get(url)
        _check_response(resp)

        body  = resp.json()
        # La API devuelve los IDs directamente en body (no en body.works).
        # Estructura: {"body": {"id1": {...work...}, "id2": {...work...}}}
        works = body.get("body", {})

        for illust_id, work in works.items():
            if not work:
                continue

            # Filtrar por fecha sin hacer llamadas extra al CDN
            if since is not None:
                pub = parse_date_utc(work.get("createDate", ""))
                if pub is not None and pub < since:
                    continue

            illust_type = int(work.get("illustType", 0))
            page_count  = int(work.get("pageCount", 1))

            if illust_type == 2:
                # Ugoira — necesita llamada extra para obtener el ZIP
                async for fi in self._get_ugoira(
                    client, illust_id, work, artist
                ):
                    yield fi

            else:
                # Ilustración / manga (1 o N páginas).
                # El batch NO incluye urls.original — siempre llamar /pages.
                async for fi in self._get_pages(
                    client, illust_id, work, artist
                ):
                    yield fi

    async def _get_pages(
        self,
        client: httpx.AsyncClient,
        illust_id: str,
        work: dict,
        artist: ArtistInfo,
    ) -> AsyncIterator[FileInfo]:
        """
        GET /ajax/illust/{id}/pages → lista de URLs originales.
        Yield un FileInfo por página.
        """
        url = f"{_AJAX}/illust/{illust_id}/pages?lang=en"

        await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        resp = await client.get(url)

        if resp.status_code == 404:
            return
        _check_response(resp)

        body  = resp.json()
        pages = body.get("body", [])

        for i, page in enumerate(pages):
            orig_url = page.get("urls", {}).get("original", "")
            if orig_url:
                yield _make_file_info(illust_id, i, orig_url, work, artist)

    async def _get_ugoira(
        self,
        client: httpx.AsyncClient,
        illust_id: str,
        work: dict,
        artist: ArtistInfo,
    ) -> AsyncIterator[FileInfo]:
        """
        GET /ajax/illust/{id}/ugoira_meta → URL del ZIP de frames.
        Yield 1 FileInfo apuntando al ZIP.
        """
        url = f"{_AJAX}/illust/{illust_id}/ugoira_meta?lang=en"

        await asyncio.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
        resp = await client.get(url)

        if resp.status_code == 404:
            return
        _check_response(resp)

        meta    = resp.json().get("body", {})
        # originalSrc = ZIP en resolución original; src = ZIP en 600x600
        zip_url = meta.get("originalSrc") or meta.get("src") or ""
        if not zip_url:
            return

        filename = _safe_filename(f"{illust_id}_ugoira.zip")
        yield FileInfo(
            url            = zip_url,
            url_source     = _URI_UGOIRA.format(illust_id),
            filename       = filename,
            artist_id      = artist.artist_id,
            artist_name    = artist.name,
            post_id        = illust_id,
            post_title     = work.get("title", ""),
            date_published = work.get("createDate", ""),
            extra_headers  = dict(_DOWNLOAD_HEADERS),
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_user_id(url: str) -> str:
    """Extrae el user_id numérico de la URL de perfil de Pixiv."""
    m = _URL_RE.search(url)
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def _make_file_info(
    illust_id: str,
    page: int,
    orig_url: str,
    work: dict,
    artist: ArtistInfo,
) -> FileInfo:
    """Construye un FileInfo para una página de ilustración."""
    filename = _safe_filename(
        f"{illust_id}_p{page}{_ext_from_url(orig_url)}"
    )
    return FileInfo(
        url            = orig_url,
        url_source     = _URI_ILLUST.format(illust_id, page),
        filename       = filename,
        artist_id      = artist.artist_id,
        artist_name    = artist.name,
        post_id        = illust_id,
        post_title     = work.get("title", ""),
        date_published = work.get("createDate", ""),
        extra_headers  = dict(_DOWNLOAD_HEADERS),
    )


def _check_response(resp: httpx.Response) -> None:
    """
    Manejo centralizado de errores de la API de Pixiv.

    403 → sesión expirada o cookie inválida → limpiar + NeedsPixivAuth
    401 → igual que 403
    """
    if resp.status_code in (401, 403):
        from ..auth.pixiv import clear_pixiv_session
        clear_pixiv_session()
        raise NeedsPixivAuth(
            f"Sesión de Pixiv inválida ({resp.status_code}). "
            "Vuelve a iniciar sesión en el navegador."
        )
    resp.raise_for_status()


def _chunked(lst: list, size: int) -> list[list]:
    """Divide una lista en sublistas de tamaño máximo `size`."""
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def _ext_from_url(url: str) -> str:
    """Extrae la extensión del archivo de una URL (con el punto)."""
    path = urlparse(url).path
    if "." in path:
        ext = "." + path.rsplit(".", 1)[-1].lower()
        return ext if len(ext) <= 5 else ""
    return ""


def _safe_filename(name: str) -> str:
    """Sanitiza nombre de archivo eliminando caracteres inválidos."""
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "file"
