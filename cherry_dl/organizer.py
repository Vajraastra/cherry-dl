"""
Organizador de archivos externos.

Permite incorporar a cherry-dl archivos descargados por otros medios.
El proceso:
  1. Escanea la carpeta fuente y calcula SHA-256 de cada archivo
  2. Compara contra catalog.db del artista (y contra los vistos en esta misma sesión)
  3. Renombra los no-duplicados al formato: {artista}_{contador:05d}{ext}
  4. Mueve los archivos al directorio del artista en cherry-dl
  5. Registra los nuevos hashes en catalog.db
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import add_file, get_all_hashes, init_catalog, next_counter
from .gui.bridge import build_filename
from .hasher import sha256_file

# Extensiones de medios soportadas
_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv",
    ".mp3", ".ogg", ".flac", ".wav", ".aac",
    ".zip", ".rar", ".7z", ".pdf", ".psd", ".clip",
}

# Tipo de callback de progreso: (archivos_procesados, total, nombre_archivo)
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class OrganizeResult:
    """Resultado de una operación de organización."""
    moved: int = 0
    skipped_duplicates: int = 0
    skipped_unsupported: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_scanned(self) -> int:
        return self.moved + self.skipped_duplicates + self.skipped_unsupported

    def summary(self) -> str:
        return (
            f"Movidos: {self.moved} | "
            f"Duplicados ignorados: {self.skipped_duplicates} | "
            f"No soportados: {self.skipped_unsupported} | "
            f"Errores: {len(self.errors)}"
        )


async def organize(
    source_dir: Path,
    artist_name: str,
    artist_id: str,
    site: str,
    dest_root: Path,
    progress_cb: ProgressCallback | None = None,
) -> tuple[OrganizeResult, Path]:
    """
    Incorpora archivos de source_dir al catálogo de cherry-dl.

    Los archivos son renombrados al formato {artista}_{contador:05d}{ext}
    y movidos al directorio correspondiente dentro de dest_root.

    Args:
        source_dir:   Carpeta con los archivos a incorporar.
        artist_name:  Nombre del artista (para renombrado y carpeta).
        artist_id:    ID del artista en el sitio de origen.
        site:         Nombre del sitio (ej: "kemono").
        dest_root:    Directorio raíz de descargas de cherry-dl.
        progress_cb:  Callback opcional (procesados, total, nombre_archivo).

    Returns:
        (OrganizeResult, artist_dir) — estadísticas y ruta del directorio del artista.
    """
    result = OrganizeResult()

    # Directorio destino del artista
    artist_dir = dest_root / site / _safe_dirname(artist_name or artist_id)
    artist_dir.mkdir(parents=True, exist_ok=True)

    # Inicializar catalog.db si no existe
    await init_catalog(artist_dir)

    # Cargar hashes existentes del artista
    existing_hashes = await get_all_hashes(artist_dir)

    # Recolectar archivos de medios de la fuente
    source_files = _collect_media_files(source_dir)

    if not source_files:
        return result, artist_dir

    total = len(source_files)

    for idx, src_file in enumerate(source_files, 1):
        if progress_cb:
            progress_cb(idx, total, src_file.name)

        try:
            file_hash = sha256_file(src_file)

            # Duplicado contra el catálogo existente o contra la misma sesión
            if file_hash in existing_hashes:
                result.skipped_duplicates += 1
                continue

            # Obtener contador y construir nombre final
            counter = await next_counter(artist_dir)
            final_name = build_filename(artist_name, counter, src_file.name)
            dest_file = artist_dir / final_name

            # Mover el archivo
            shutil.move(str(src_file), dest_file)

            # Registrar en catalog.db
            await add_file(
                artist_dir=artist_dir,
                file_hash=file_hash,
                filename=final_name,
                url_source=None,   # origen externo, sin URL
                file_size=src_file.stat().st_size if src_file.exists() else dest_file.stat().st_size,
                counter=counter,
            )

            # Agregar al set local para detectar duplicados dentro de la sesión
            existing_hashes.add(file_hash)
            result.moved += 1

        except Exception as e:
            result.errors.append(f"{src_file.name}: {e}")

    return result, artist_dir


# ── Helpers ────────────────────────────────────────────────────────────────────

def _collect_media_files(directory: Path) -> list[Path]:
    """Recopila archivos de medios (no recursivo)."""
    return sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in _MEDIA_EXTENSIONS
    )


def _safe_dirname(name: str) -> str:
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip("._") or "unknown"
