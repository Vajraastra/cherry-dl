"""
Registro de templates.

Los templates se registran automáticamente al importar este módulo.
Para añadir un nuevo template, simplemente impórtalo aquí.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import SiteTemplate
from .kemono import KemonoTemplate

if TYPE_CHECKING:
    from ..engine import DownloadEngine

# Lista de todos los templates disponibles (en orden de prioridad)
_TEMPLATES: list[type[SiteTemplate]] = [
    KemonoTemplate,
]


def find_template(url: str) -> type[SiteTemplate] | None:
    """
    Retorna la clase de template que puede manejar la URL dada.
    Retorna None si ningún template la reconoce.
    """
    for template_cls in _TEMPLATES:
        if template_cls.can_handle(url):
            return template_cls
    return None


def get_template(url: str, engine: "DownloadEngine") -> SiteTemplate | None:
    """
    Instancia y retorna el template adecuado para la URL.
    Retorna None si ningún template la reconoce.
    """
    cls = find_template(url)
    return cls(engine) if cls else None


def list_templates() -> list[str]:
    """Retorna los nombres de todos los templates registrados."""
    return [t.name for t in _TEMPLATES]
