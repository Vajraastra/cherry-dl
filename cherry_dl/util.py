"""
Utilidades compartidas, agnósticas de UI.
"""

from __future__ import annotations

import re

# Nombres de dispositivo reservados en Windows (no pueden ser nombre de archivo
# ni de carpeta, con o sin extensión).
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def safe_dirname(name: str) -> str:
    """Sanitiza un nombre para usarlo como carpeta/archivo en cualquier OS.

    Cubre las restricciones de Windows (las más estrictas): caracteres ilegales,
    caracteres de control, puntos/espacios finales y nombres de dispositivo
    reservados. En Linux estas reglas son un superconjunto seguro.
    """
    # Caracteres ilegales en Windows → '_'
    name = _ILLEGAL.sub("_", name)
    # Eliminar caracteres de control (< 0x20)
    name = "".join(c for c in name if ord(c) >= 32)
    # Windows prohíbe puntos/espacios al final del nombre
    name = name.strip().rstrip(". ")
    # Compatibilidad con el comportamiento previo (recorte de ._ envolventes)
    name = name.strip("._")
    if not name:
        return "unknown"
    # Nombres de dispositivo reservados (CON, NUL, COM1, …) → prefijar
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name
