"""
Utilidades de hashing SHA-256.
Soporta hashing de archivos en disco (streaming) y de bytes en memoria.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Tamaño de chunk para leer archivos grandes sin saturar RAM
_CHUNK = 1024 * 1024  # 1 MB


def sha256_file(path: Path) -> str:
    """
    Calcula SHA-256 de un archivo leyéndolo en chunks.
    Seguro para archivos de cualquier tamaño.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Calcula SHA-256 de bytes en memoria (para respuestas de descarga)."""
    return hashlib.sha256(data).hexdigest()
