"""
Recuperación de perfiles desde las carpetas + directorio de creadores de kemono.

Las colecciones creadas antes de `profile_meta` no almacenan la URL de scrape del
artista (el `catalog.db` solo guarda archivos: hash, nombre, contador). Este
módulo reconstruye esa URL cruzando el NOMBRE de la carpeta contra el directorio
público de creadores de kemono (`/api/v1/creators`), que mapea nombre → (servicio,
id). De ahí se arma la URL `https://kemono.cr/{service}/user/{id}`.

El directorio se cachea en `{download_dir}/.recovery/kemono_creators_full.json`
para no depender de que kemono siga vivo (cierre anunciado ~2026-07-04).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

_CREATORS_URL = "https://kemono.cr/api/v1/creators"
_KEMONO_BASE = "https://kemono.cr"
_RECOVERY_DIR = ".recovery"
_SNAPSHOT = "kemono_creators_full.json"
_REVIEW = "recovery_review.txt"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def kemono_url(service: str, creator_id: str) -> str:
    return f"{_KEMONO_BASE}/{service}/user/{creator_id}"


# ── Directorio de creadores ────────────────────────────────────────────────────

def load_kemono_directory(download_dir: Path, allow_fetch: bool = True) -> list[dict]:
    """Carga el directorio de creadores: snapshot local si existe, si no lo baja.

    Guarda el snapshot en `{download_dir}/.recovery/` para uso offline futuro.
    """
    snap = download_dir / _RECOVERY_DIR / _SNAPSHOT
    if snap.exists():
        return json.loads(snap.read_text(encoding="utf-8"))
    if not allow_fetch:
        return []
    r = httpx.get(
        _CREATORS_URL,
        headers={"Accept": "text/css", "User-Agent": "Mozilla/5.0"},
        timeout=60,
        follow_redirects=True,
    )
    r.raise_for_status()
    data = r.json()
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _build_lookup(directory: list[dict]) -> tuple[dict, dict]:
    by_exact: dict[str, list[dict]] = {}
    by_norm: dict[str, list[dict]] = {}
    for c in directory:
        by_exact.setdefault(c.get("name", "").lower(), []).append(c)
        by_norm.setdefault(_norm(c.get("name", "")), []).append(c)
    return by_exact, by_norm


def _match(name: str, by_exact: dict, by_norm: dict) -> list[dict]:
    return by_exact.get(name.lower()) or by_norm.get(_norm(name)) or []


def _choose(cands: list[dict], prefer_service: str) -> dict | None:
    """Elige un candidato: único directo; en colisión prefiere `prefer_service`."""
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for c in cands:
        if c.get("service") == prefer_service:
            return c
    return None   # colisión sin servicio preferido → revisión manual


# ── Orquestación ───────────────────────────────────────────────────────────────

async def recover_profiles(
    index_db: Path,
    download_dir: Path,
    prefer_service: str = "patreon",
    dry_run: bool = False,
) -> dict:
    """Reconstruye perfiles desde las carpetas usando el directorio de kemono.

    - Carpetas que ya tienen `profile_meta` se saltan (ya auto-describibles).
    - Match único o colisión resuelta por `prefer_service` → se crea el perfil +
      URL de kemono y se escribe `profile_meta` (vía add_profile_url).
    - Colisiones sin servicio preferido y sin-match → archivo de revisión.

    Retorna: {recovered, review, already, no_match, multi}.
    """
    from . import index as idx
    from . import catalog
    import aiosqlite

    download_dir = Path(download_dir)
    directory = load_kemono_directory(download_dir, allow_fetch=not dry_run)
    by_exact, by_norm = _build_lookup(directory)
    await idx.init_index(index_db)

    res = {"recovered": [], "review": [], "already": [], "no_catalog": 0}

    for folder in sorted(p for p in download_dir.iterdir() if p.is_dir()):
        if not (folder / catalog.CATALOG_NAME).exists():
            continue
        if await catalog.read_profile_meta(folder):
            res["already"].append(folder.name)
            continue

        cands = _match(folder.name, by_exact, by_norm)
        chosen = _choose(cands, prefer_service)
        if not chosen:
            res["review"].append({
                "folder": folder.name,
                "candidates": [
                    {"service": c.get("service"), "id": c.get("id"),
                     "name": c.get("name")} for c in cands
                ],
            })
            continue

        url = kemono_url(chosen["service"], str(chosen["id"]))
        if dry_run:
            res["recovered"].append({"folder": folder.name, "url": url,
                                     "service": chosen["service"]})
            continue

        # Upsert del perfil por folder_path; luego añadir la URL (sync_profile_meta
        # se dispara dentro de add_profile_url y deja la carpeta auto-describible).
        async with aiosqlite.connect(index_db) as db:
            async with db.execute(
                "SELECT id FROM profiles WHERE folder_path = ?", (str(folder),)
            ) as cur:
                row = await cur.fetchone()
            pid = row[0] if row else None

        if pid is None:
            pid = await idx.create_profile(
                index_db, folder.name, folder, chosen["service"]
            )
        await idx.add_profile_url(
            index_db, pid, url, "kemono", str(chosen["id"])
        )
        res["recovered"].append({"folder": folder.name, "url": url,
                                 "service": chosen["service"]})

    # Escribir archivo de revisión para los ambiguos / sin-match
    if res["review"] and not dry_run:
        _write_review(download_dir, res["review"])

    return res


def _write_review(download_dir: Path, review: list[dict]) -> Path:
    out = download_dir / _RECOVERY_DIR / _REVIEW
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Revisión manual de recuperación — cherry-dl",
        "# Carpetas con colisión de servicio o sin match en el directorio kemono.",
        "# Editá la línea poniendo la URL correcta tras '=' y luego corré:",
        "#   cherry-dl recover --apply-review",
        "# Formato:  NombreCarpeta = https://kemono.cr/<service>/user/<id>",
        "#           NombreCarpeta = https://www.patreon.com/<vanity>",
        "",
    ]
    for item in review:
        cands = item["candidates"]
        if cands:
            hint = "  candidatos: " + ", ".join(
                f"{c['service']}/{c['id']}" for c in cands
            )
        else:
            hint = "  (sin match en kemono — poné la URL a mano)"
        lines.append(f"{item['folder']} = {hint}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


async def apply_review(index_db: Path, download_dir: Path) -> dict:
    """Aplica el archivo de revisión editado por el usuario."""
    from . import index as idx
    from . import catalog
    from .templates._registry import find_template
    import aiosqlite

    download_dir = Path(download_dir)
    review_file = download_dir / _RECOVERY_DIR / _REVIEW
    res = {"applied": [], "skipped": []}
    if not review_file.exists():
        return res

    await idx.init_index(index_db)
    for raw in review_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        folder_name, _, rhs = line.partition("=")
        folder_name = folder_name.strip()
        url = rhs.strip()
        # Ignorar líneas que aún tienen el texto de candidatos (no una URL real)
        if not url.startswith("http"):
            res["skipped"].append(folder_name)
            continue
        folder = download_dir / folder_name
        if not (folder / catalog.CATALOG_NAME).exists():
            res["skipped"].append(folder_name)
            continue
        tmpl = find_template(url)
        if tmpl is None:
            res["skipped"].append(folder_name)
            continue
        site = tmpl.name
        async with aiosqlite.connect(index_db) as db:
            async with db.execute(
                "SELECT id FROM profiles WHERE folder_path = ?", (str(folder),)
            ) as cur:
                row = await cur.fetchone()
            pid = row[0] if row else None
        if pid is None:
            pid = await idx.create_profile(index_db, folder_name, folder, site)
        await idx.add_profile_url(index_db, pid, url, site, None)
        res["applied"].append(folder_name)
    return res
