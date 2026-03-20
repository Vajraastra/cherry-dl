"""
Lógica de negocio para perfiles de artista.

Un perfil agrupa N URLs de diferentes servicios bajo una sola carpeta,
con deduplicación automática vía url_source + SHA-256 en el catalog.db.

Arquitectura:
  - profiles.py   → lógica de negocio (esta capa)
  - index.py      → acceso a DB (tablas profiles / profile_urls)
  - templates/*   → detección de sitio y extracción de info de artista

Flujo típico para crear un perfil:
  1. name = await resolve_artist_name(engine, url)
  2. pid  = await create_profile(db_path, name, url, config.download_path)
  3. Para URLs adicionales: await add_url_to_profile(db_path, pid, url2)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import DownloadEngine


# ── Modelos de datos ────────────────────────────────────────────────────────────

@dataclass
class ProfileUrlData:
    id: int
    profile_id: int
    url: str | None          # None en entradas migradas de Fase 1
    site: str
    artist_id: str | None
    enabled: bool
    last_synced: str | None
    file_count: int


@dataclass
class ProfileData:
    id: int
    display_name: str
    folder_path: str
    primary_site: str
    created_at: str | None
    last_checked: str | None
    urls: list[ProfileUrlData] = field(default_factory=list)


# ── Helpers internos ───────────────────────────────────────────────────────────

def _site_from_url(url: str) -> str:
    """
    Detecta el nombre de sitio para una URL usando el registro de templates.
    Retorna "unknown" si ningún template la reconoce.
    """
    from .templates._registry import find_template
    cls = find_template(url)
    return cls.name if cls else "unknown"


def _safe_dirname(name: str) -> str:
    """Sanitiza un nombre para usar como nombre de carpeta."""
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip("._") or "unknown"


# ── API pública ────────────────────────────────────────────────────────────────

async def resolve_artist_name(engine: "DownloadEngine", url: str) -> str:
    """
    Llama al template correspondiente para obtener el nombre real del artista.
    Lanza ValueError si la URL no es reconocida por ningún template.
    Lanza cualquier excepción de red si la llamada a la API falla.
    """
    from .templates._registry import get_template
    template = get_template(url, engine)
    if template is None:
        raise ValueError(f"No hay template para la URL: {url}")
    artist = await template.get_artist_info(url)
    return artist.name


async def create_profile(
    db_path: Path,
    display_name: str,
    primary_url: str,
    base_dir: Path,
) -> int:
    """
    Crea un perfil nuevo a partir de una URL principal.

    - Detecta el sitio automáticamente desde la URL.
    - La carpeta destino se calcula como:
        {base_dir}/{site}/{safe_name}/
    - Agrega la URL principal como primera entrada en profile_urls.
    - Retorna el ID del perfil creado.

    Nota: display_name debe obtenerse previamente con resolve_artist_name().
    """
    from .index import create_profile as _db_create, add_profile_url

    site = _site_from_url(primary_url)
    folder_path = base_dir / site / _safe_dirname(display_name)

    profile_id = await _db_create(
        db_path=db_path,
        display_name=display_name,
        folder_path=folder_path,
        primary_site=site,
    )
    await add_profile_url(
        db_path=db_path,
        profile_id=profile_id,
        url=primary_url,
        site=site,
    )
    return profile_id


async def add_url_to_profile(
    db_path: Path,
    profile_id: int,
    url: str,
    site: str | None = None,
    artist_id: str | None = None,
) -> int:
    """
    Agrega una URL adicional a un perfil existente.

    Si site no se proporciona, se detecta automáticamente desde la URL.
    Retorna el ID de la nueva entrada en profile_urls.
    """
    from .index import add_profile_url

    resolved_site = site or _site_from_url(url)
    return await add_profile_url(
        db_path=db_path,
        profile_id=profile_id,
        url=url,
        site=resolved_site,
        artist_id=artist_id,
    )


async def get_all_profiles(db_path: Path) -> list[ProfileData]:
    """
    Retorna todos los perfiles con sus URLs.
    Cada perfil incluye la lista completa de ProfileUrlData.
    """
    from .index import list_profiles, get_profile as _db_get_profile

    rows = await list_profiles(db_path)
    profiles = []
    for row in rows:
        full = await _db_get_profile(db_path, row["id"])
        if full:
            profiles.append(_dict_to_profile(full))
    return profiles


async def get_profile(db_path: Path, profile_id: int) -> ProfileData | None:
    """Retorna un perfil con sus URLs, o None si no existe."""
    from .index import get_profile as _db_get_profile

    raw = await _db_get_profile(db_path, profile_id)
    return _dict_to_profile(raw) if raw else None


async def delete_profile(db_path: Path, profile_id: int) -> None:
    """Elimina un perfil y todas sus URLs asociadas."""
    from .index import delete_profile as _db_delete
    await _db_delete(db_path, profile_id)


# ── Helpers de conversión ──────────────────────────────────────────────────────

def _dict_to_profile(raw: dict) -> ProfileData:
    """Convierte el dict crudo de index.py a ProfileData."""
    urls = [
        ProfileUrlData(
            id=u["id"],
            profile_id=u["profile_id"],
            url=u["url"],
            site=u["site"],
            artist_id=u["artist_id"],
            enabled=u["enabled"],
            last_synced=u["last_synced"],
            file_count=u["file_count"],
        )
        for u in raw.get("urls", [])
    ]
    return ProfileData(
        id=raw["id"],
        display_name=raw["display_name"],
        folder_path=raw["folder_path"],
        primary_site=raw["primary_site"],
        created_at=raw["created_at"],
        last_checked=raw["last_checked"],
        urls=urls,
    )
