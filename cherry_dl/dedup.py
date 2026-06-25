"""
Helpers puros de detección y fusión de perfiles duplicados.

Compartidos entre la GUI (`gui/views/duplicates_view.py`) y la TUI (que los
reimporta). La lógica de BD vive en `catalog.py` (compare_by_hash_join,
migrate_unique_files) e `index.py` (merge_profiles, exclusiones); aquí solo
viven los helpers de orquestación sin estado.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


def normalize_name(name: str) -> str:
    """Normaliza un nombre para comparación: lowercase, solo alfanumérico."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def name_similarity(a: str, b: str) -> float:
    """Ratio de similitud (0–1) entre dos nombres, ignorando capitalización y símbolos."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def url_overlap(urls_a: list[dict], urls_b: list[dict]) -> str:
    """
    Detecta si dos listas de profile_urls comparten el mismo artista en el
    mismo sitio (site + artist_id coinciden). Retorna una descripción del
    primer match, o "" si no hay.
    """
    for ua in urls_a:
        if not ua.get("artist_id") or not ua.get("site"):
            continue
        for ub in urls_b:
            if (ub.get("site") == ua["site"]
                    and ub.get("artist_id") == ua["artist_id"]):
                return f"{ua['site']}/{ua['artist_id']}"
    return ""


def dup_keep_remove(pair: dict) -> tuple[int, int, str, str]:
    """
    Dado un par, retorna (keep_id, remove_id, keep_name, remove_name).
    El perfil más antiguo (created_at menor, o id menor) absorbe al más nuevo.
    """
    def _dt(s: str | None) -> datetime:
        if not s:
            return datetime.max
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return datetime.max

    dt_a = _dt(pair.get("created_at_a"))
    dt_b = _dt(pair.get("created_at_b"))
    if dt_a <= dt_b:
        return pair["id_a"], pair["id_b"], pair["name_a"], pair["name_b"]
    return pair["id_b"], pair["id_a"], pair["name_b"], pair["name_a"]


def handle_orphans(folder: Path, orphaned_paths: list[Path], action: str) -> None:
    """Borra o renombra los archivos/carpeta huérfanos. action ∈ delete|rename|ignore."""
    if action == "delete":
        for p in orphaned_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        # Borrar carpeta si quedó vacía (solo tiene catalog.db / *.db)
        try:
            remaining = [
                f for f in folder.iterdir()
                if f.name != "catalog.db" and not f.name.endswith(".db")
            ]
            if not remaining:
                shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass

    elif action == "rename":
        new_name = folder.parent / f"orphan_{folder.name}"
        try:
            folder.rename(new_name)
        except Exception:
            pass  # Si falla (ya existe, permisos), se deja como está


async def compact_folders(folders: list[tuple[int, str]]) -> None:
    """Compacta la numeración de las carpetas que recibieron archivos migrados."""
    from .catalog import apply_compaction, get_numbered_files, plan_compaction

    for _, folder_str in folders:
        folder = Path(folder_str)
        try:
            files = await get_numbered_files(folder)
            plan = plan_compaction(files)
            if plan:
                new_total = files[-1][0] if files else 0
                await apply_compaction(folder, plan, new_total)
        except Exception:
            pass
