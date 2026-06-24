"""
Helpers de descarga compartidos, agnósticos de UI.

Reúne las utilidades que usan tanto la TUI como la GUI (y el CLI/organizer):
construcción de nombres de archivo, filtros por extensión y mapeo de hashes
locales. Antes vivían en `gui/bridge.py` (módulo de la GUI Dear PyGui original);
se extrajeron aquí para romper la dependencia invertida TUI → gui.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

# Extensiones a considerar al escanear archivos locales (excluir .db, .json, etc.)
_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv",
    ".mp3", ".ogg", ".flac", ".wav", ".aac",
    ".zip", ".rar", ".7z", ".pdf", ".psd", ".clip",
}


# ── Utilidades de nombres ──────────────────────────────────────────────────────

def _safe_prefix(name: str) -> str:
    """Genera un prefijo limpio para nombres de archivo: solo alfanum y guiones."""
    prefix = re.sub(r"[^\w\-]", "_", name).strip("_")
    return prefix[:40] or "artist"  # máx 40 chars para no hacer nombres kilométricos


def build_filename(artist_name: str, counter: int, original_filename: str) -> str:
    """
    Construye el nombre final del archivo:
      {prefijo_artista}_{contador:05d}{extensión_original}

    Ejemplos:
      ViciNeko_00001.jpg
      SomeArtist_00042.psd
      another_artist_00100.png
    """
    ext = Path(original_filename).suffix.lower()
    prefix = _safe_prefix(artist_name)
    return f"{prefix}_{counter:05d}{ext}"


# ── Filtros por extensión ──────────────────────────────────────────────────────

def _parse_ext_filter(raw: str | None) -> set[str]:
    """
    Convierte una cadena como '.zip, .rar, jpg' en un set normalizado:
    {'.zip', '.rar', '.jpg'}
    Retorna set vacío si raw es None o cadena vacía.
    """
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        result.add(ext)
    return result


def _passes_ext_filter(filename: str, ext_filter: set[str], exclude_mode: bool) -> bool:
    """
    Retorna True si el archivo debe descargarse según el filtro.
      exclude_mode=True  → excluir las extensiones listadas (descargar todo lo demás)
      exclude_mode=False → incluir solo las extensiones listadas
    Si ext_filter está vacío, siempre retorna True.
    """
    if not ext_filter:
        return True
    ext = Path(filename).suffix.lower()
    if exclude_mode:
        return ext not in ext_filter
    else:
        return ext in ext_filter


# ── Mapa de hashes locales ─────────────────────────────────────────────────────

async def _build_local_hash_map(artist_dir: Path) -> dict[str, Path]:
    """
    Retorna {sha256: path} sólo para archivos de medios que existen
    físicamente en artist_dir pero NO están registrados en catalog.db.

    Flujo rápido:
      1. Leer catalog.db para obtener {filename: hash} de archivos indexados
         → sin acceso a disco, sólo una query SQL.
      2. Filtrar archivos físicos: sólo los que no están en el catálogo
         necesitan ser hasheados.
      3. Hashear los restantes en paralelo con run_in_executor.

    Para colecciones establecidas (todo catalogado) esto es casi instantáneo.
    Para importaciones iniciales (carpeta sin catalog.db) hashea en paralelo.

    También limpia archivos .tmp huérfanos de sesiones interrumpidas.
    """
    from .catalog import get_all_files
    from .hasher import sha256_file

    # Eliminar .tmp huérfanos de sesiones anteriores
    try:
        for tmp in artist_dir.iterdir():
            if tmp.is_file() and tmp.name.endswith(".tmp"):
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except OSError:
        return {}

    # Paso 1: hashes ya conocidos por el catálogo (sin leer disco)
    try:
        cataloged: dict[str, str] = {
            e["filename"]: e["hash"]
            for e in await get_all_files(artist_dir)
        }
    except Exception:
        cataloged = {}

    # Paso 2: archivos físicos que NO están en el catálogo
    try:
        physical = [
            f for f in artist_dir.iterdir()
            if f.is_file() and f.suffix.lower() in _MEDIA_EXTENSIONS
        ]
    except OSError:
        return {}

    uncataloged = [f for f in physical if f.name not in cataloged]
    if not uncataloged:
        return {}

    # Paso 3: hashear en paralelo sólo los no catalogados
    loop = asyncio.get_running_loop()

    async def _hash_one(f: Path) -> tuple[str | None, Path]:
        try:
            h = await loop.run_in_executor(None, sha256_file, f)
            return h, f
        except Exception:
            return None, f

    pairs = await asyncio.gather(*(_hash_one(f) for f in uncataloged))
    return {h: f for h, f in pairs if h is not None}
