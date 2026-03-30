"""
Template para Kemono.cr

Kemono es un archivador de contenido de múltiples plataformas.
API base: https://kemono.cr/api/v1/

Servicios soportados: patreon, fanbox, discord, fantia, afdian,
                      boosty, dlsite, gumroad, subscribestar

Estado verificado 2026-03-18:
  - /posts-legacy fue eliminado. Usar /posts.
  - ?o=0 y sin offset devuelven el mismo resultado (bug anterior corregido).
  - Paginación: offset en múltiplos de 50.
  - DDoS-Guard bypass: header `Accept: text/css` (documentado por el creador).
  - Cookies DDG (__ddg1_ etc.) deben persistirse entre sesiones.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from .base import ArtistInfo, FileInfo, SiteTemplate, parse_date_utc

# ── Constantes ─────────────────────────────────────────────────────────────────

_BASE = "https://kemono.cr"

_SERVICES = frozenset([
    "patreon", "fanbox", "discord", "fantia",
    "afdian", "boosty", "dlsite", "gumroad", "subscribestar",
])

# URL válida: https://kemono.cr/{service}/user/{id}
_URL_RE = re.compile(
    r"https?://kemono\.cr/([a-z]+)/user/([^/?#]+)",
    re.IGNORECASE,
)

_PAGE_SIZE = 50  # Kemono retorna 50 posts por página


# ── Template ───────────────────────────────────────────────────────────────────

class KemonoTemplate(SiteTemplate):
    name = "kemono"
    base_url = _BASE
    workers = 3                  # Conservador — kemono tiene rate limits agresivos
    provides_file_hashes = True  # Kemono expone SHA-256 en el path del CDN

    # ── Detección ──────────────────────────────────────────────────────────────

    @classmethod
    def can_handle(cls, url: str) -> bool:
        m = _URL_RE.match(url)
        if not m:
            return False
        service = m.group(1).lower()
        return service in _SERVICES

    # ── Caché de creadores (por instancia de template) ────────────────────────
    # Se carga una vez por sesión y se reutiliza para todas las búsquedas.
    _creators_cache: dict[str, dict] | None = None

    async def _get_creators_index(self) -> dict[str, dict]:
        """
        Carga /api/v1/creators una sola vez y construye un índice
        {(service, id): name} para búsqueda O(1).
        """
        if KemonoTemplate._creators_cache is not None:
            return KemonoTemplate._creators_cache

        creators = await self.engine.get_json(f"{_BASE}/api/v1/creators")
        index = {}
        if isinstance(creators, list):
            for c in creators:
                key = (c.get("service", ""), str(c.get("id", "")))
                index[key] = c.get("name", "")
        KemonoTemplate._creators_cache = index
        return index

    # ── Info del artista ───────────────────────────────────────────────────────

    async def get_artist_info(self, url: str) -> ArtistInfo:
        """Extrae service y creator_id de la URL y resuelve el nombre real."""
        m = _URL_RE.match(url)
        if not m:
            raise ValueError(f"URL no reconocida por KemonoTemplate: {url}")

        service = m.group(1).lower()
        creator_id = m.group(2)

        # Buscar nombre real en el índice de creadores
        index = await self._get_creators_index()
        name = index.get((service, creator_id), creator_id)

        return ArtistInfo(
            artist_id=creator_id,
            name=name,
            service=service,
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
        Itera sobre todos los archivos del artista.

        Paginación:
          - 1ª llamada: ?o=0, siguientes ?o=50, ?o=100...
          - Termina cuando la respuesta es lista vacía o 400/404.

        Si `since` está definido, se detiene en cuanto encuentra un post
        publicado antes de esa fecha (los posts vienen de más nuevo a más
        antiguo, por lo que todos los siguientes también serían anteriores).
        """
        service    = artist.service
        creator_id = artist.artist_id
        offset: int = 0

        while True:
            url = (
                f"{_BASE}/api/v1/{service}/user/{creator_id}"
                f"/posts?o={offset}"
            )

            try:
                posts = await self.engine.get_json(url)
            except httpx.HTTPStatusError as e:
                # 400 / 404 en paginación = offset fuera de rango → fin normal
                if e.response.status_code in (400, 404):
                    break
                raise

            if not posts:
                break

            for post in posts:
                if since is not None:
                    pub = parse_date_utc(post.get("published", ""))
                    if pub is not None and pub < since:
                        return  # posts son newest-first → parar paginación
                for file_info in _extract_files_from_post(post, artist):
                    yield file_info

            offset += _PAGE_SIZE


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_files_from_post(post: dict, artist: ArtistInfo):
    """
    Generador que yield FileInfo por cada archivo en un post.
    Cada post puede tener:
      - file:        archivo principal (puede ser None)
      - attachments: lista de archivos adjuntos

    Se incluye Referer apuntando a la página del creador para mejorar
    el cache hit rate en el CDN de kemono.cr.
    """
    post_id = str(post.get("id", ""))
    post_title = post.get("title", "")
    date_published = post.get("published", "")
    referer = {"Referer": artist.url}

    # Archivo principal del post
    main_file = post.get("file")
    if main_file and main_file.get("path"):
        path = main_file["path"]
        filename = _safe_filename(main_file.get("name") or _name_from_path(path))
        yield FileInfo(
            url=f"{_BASE}{path}",
            filename=filename,
            artist_id=artist.artist_id,
            artist_name=artist.name,
            post_id=post_id,
            post_title=post_title,
            date_published=date_published,
            remote_hash=_hash_from_path(path) or "",
            extra_headers=referer,
        )

    # Archivos adjuntos
    for att in post.get("attachments", []):
        if not att.get("path"):
            continue
        path = att["path"]
        filename = _safe_filename(att.get("name") or _name_from_path(path))
        yield FileInfo(
            url=f"{_BASE}{path}",
            filename=filename,
            artist_id=artist.artist_id,
            artist_name=artist.name,
            post_id=post_id,
            post_title=post_title,
            date_published=date_published,
            remote_hash=_hash_from_path(path) or "",
            extra_headers=referer,
        )


def _hash_from_path(path: str) -> str | None:
    """
    Extrae el hash SHA-256 del path de kemono.

    Formato antiguo: /data/{2chars}/{2chars}/{sha256}/{filename}
    Formato actual:  /{2chars}/{2chars}/{sha256}/{filename}

    Retorna el hash en minúsculas o None si el path no tiene esa estructura.
    """
    parts = [p for p in path.split("/") if p]

    if len(parts) >= 4 and parts[0] == "data":
        # Formato legado: ['data', 'ab', 'cd', sha256, filename]
        candidate = parts[3]
    elif len(parts) >= 3 and len(parts[0]) == 2 and len(parts[1]) == 2:
        # Formato actual: ['ab', 'cd', sha256, filename]
        candidate = parts[2]
    else:
        return None

    if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate.lower()):
        return candidate.lower()
    return None


def _name_from_path(path: str) -> str:
    """Extrae el nombre de archivo de una ruta URL."""
    return path.split("/")[-1] or "file"


def _safe_filename(name: str) -> str:
    """Sanitiza un nombre de archivo eliminando caracteres no válidos."""
    # Reemplazar caracteres problemáticos en Linux/Windows
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip() or "file"
