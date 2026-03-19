"""
Clase base abstracta para templates de sitios.

Cada template implementa la lógica específica de su sitio:
  - Detectar si puede manejar una URL
  - Extraer información del artista
  - Iterar sobre todos los archivos disponibles
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from ..engine import DownloadEngine


# ── Modelo de archivo a descargar ──────────────────────────────────────────────

@dataclass
class FileInfo:
    """Representa un archivo listo para descargar."""
    url: str
    filename: str
    artist_id: str
    artist_name: str
    post_id: str
    # Metadatos opcionales del post
    post_title: str = ""
    date_published: str = ""
    # Hash remoto extraído del path del servidor (si el sitio lo expone)
    remote_hash: str = ""


# ── Modelo de artista ──────────────────────────────────────────────────────────

@dataclass
class ArtistInfo:
    """Información del artista extraída del sitio."""
    artist_id: str
    name: str
    service: str        # nombre del servicio dentro del template (ej: "patreon")
    site: str           # nombre del template (ej: "kemono")
    url: str = ""


# ── Template base ──────────────────────────────────────────────────────────────

class SiteTemplate(ABC):
    """
    Clase base para todos los templates de descarga.

    Subclases deben definir:
      - name: str          — identificador único del sitio
      - base_url: str      — URL base
      - workers: int       — pool size recomendado para este sitio
    """

    name: str = ""
    base_url: str = ""
    workers: int = 3

    def __init__(self, engine: "DownloadEngine") -> None:
        self.engine = engine

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Retorna True si este template puede procesar la URL dada."""
        ...

    @abstractmethod
    async def get_artist_info(self, url: str) -> ArtistInfo:
        """Extrae y retorna información del artista desde la URL."""
        ...

    @abstractmethod
    async def iter_files(self, artist: ArtistInfo) -> AsyncIterator[FileInfo]:
        """
        Genera todos los archivos disponibles para un artista.
        Implementa paginación interna según el sitio.
        """
        ...

    def __repr__(self) -> str:
        return f"<Template:{self.name}>"
