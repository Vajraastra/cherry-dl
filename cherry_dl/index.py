"""
Índice central — ~/.cherry-dl/index.db
Registra sitios, artistas y rutas de sus carpetas.
Permite relinkar carpetas movidas sin perder el historial.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

# ── Schema ─────────────────────────────────────────────────────────────────────

_CREATE_SITES = """
CREATE TABLE IF NOT EXISTS sites (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT UNIQUE NOT NULL,
    url     TEXT
);
"""

_CREATE_ARTISTS = """
CREATE TABLE IF NOT EXISTS artists (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    artist_id   TEXT NOT NULL,          -- ID en el sitio de origen
    folder_path TEXT NOT NULL,          -- ruta absoluta en disco
    FOREIGN KEY (site_id) REFERENCES sites(id),
    UNIQUE(site_id, artist_id)
);
"""

_CREATE_IDX_ARTIST = "CREATE INDEX IF NOT EXISTS idx_artist_id ON artists(artist_id);"


# ── Inicialización ─────────────────────────────────────────────────────────────

async def init_index(db_path: Path) -> None:
    """Crea index.db si no existe."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_SITES)
        await db.execute(_CREATE_ARTISTS)
        await db.execute(_CREATE_IDX_ARTIST)
        await db.commit()


# ── Sitios ─────────────────────────────────────────────────────────────────────

async def get_or_create_site(db_path: Path, name: str, url: str = "") -> int:
    """Retorna el ID del sitio, creándolo si no existe."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id FROM sites WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()

        if row:
            return row[0]

        async with db.execute(
            "INSERT INTO sites (name, url) VALUES (?, ?)", (name, url)
        ) as cur:
            site_id = cur.lastrowid
        await db.commit()
    return site_id


# ── Artistas ───────────────────────────────────────────────────────────────────

async def get_or_create_artist(
    db_path: Path,
    site_id: int,
    artist_id: str,
    name: str,
    folder_path: Path,
) -> int:
    """
    Retorna el ID del artista en el índice.
    Si no existe lo crea. Si existe, actualiza su folder_path (por si se movió).
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id FROM artists WHERE site_id = ? AND artist_id = ?",
            (site_id, artist_id),
        ) as cur:
            row = await cur.fetchone()

        if row:
            # Actualizar ruta por si la carpeta fue movida
            await db.execute(
                "UPDATE artists SET folder_path = ?, name = ? WHERE id = ?",
                (str(folder_path), name, row[0]),
            )
            await db.commit()
            return row[0]

        async with db.execute(
            "INSERT INTO artists (site_id, name, artist_id, folder_path) VALUES (?, ?, ?, ?)",
            (site_id, name, artist_id, str(folder_path)),
        ) as cur:
            new_id = cur.lastrowid
        await db.commit()
    return new_id


async def relink_artist(
    db_path: Path, artist_id: str, site_name: str, new_path: Path
) -> bool:
    """
    Actualiza la ruta de la carpeta de un artista.
    Retorna True si se encontró y actualizó, False si no existe.
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """
            UPDATE artists SET folder_path = ?
            WHERE artist_id = ?
              AND site_id = (SELECT id FROM sites WHERE name = ?)
            """,
            (str(new_path), artist_id, site_name),
        ) as cur:
            updated = cur.rowcount
        await db.commit()
    return updated > 0


async def get_artist_folder(
    db_path: Path, artist_id: str, site_name: str
) -> Path | None:
    """Retorna la ruta de carpeta de un artista o None si no está indexado."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """
            SELECT a.folder_path FROM artists a
            JOIN sites s ON a.site_id = s.id
            WHERE a.artist_id = ? AND s.name = ?
            """,
            (artist_id, site_name),
        ) as cur:
            row = await cur.fetchone()
    return Path(row[0]) if row else None


async def migrate_all_folders(
    db_path: Path,
    old_root: Path,
    new_root: Path,
    progress_cb=None,   # Callable[[int, int, str], None] | None
) -> tuple[int, list[str]]:
    """
    Mueve todas las carpetas de artistas de old_root a new_root y actualiza index.db.

    Estructura esperada:  {root}/{site}/{artist}/
    Retorna (moved_count, errors).
    """
    import shutil

    artists = await list_all(db_path)
    total   = len(artists)
    moved   = 0
    errors: list[str] = []

    async with aiosqlite.connect(db_path) as db:
        for idx, a in enumerate(artists, 1):
            old_path = Path(a["folder_path"])

            # Calcular nueva ruta manteniendo {site}/{artist}
            try:
                rel = old_path.relative_to(old_root)
            except ValueError:
                # Esta carpeta no está bajo old_root — saltar
                if progress_cb:
                    progress_cb(idx, total, a["name"])
                continue

            new_path = new_root / rel

            if progress_cb:
                progress_cb(idx, total, a["name"])

            try:
                if old_path.exists():
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_path), new_path)

                # Actualizar index.db con la nueva ruta
                await db.execute(
                    "UPDATE artists SET folder_path = ? WHERE folder_path = ?",
                    (str(new_path), str(old_path)),
                )
                moved += 1

            except Exception as e:
                errors.append(f"{a['name']}: {e}")

        await db.commit()

    return moved, errors


async def list_all(db_path: Path) -> list[dict]:
    """Retorna lista de todos los artistas indexados con su sitio y ruta."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """
            SELECT s.name, a.name, a.artist_id, a.folder_path
            FROM artists a
            JOIN sites s ON a.site_id = s.id
            ORDER BY s.name, a.name
            """
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"site": r[0], "name": r[1], "artist_id": r[2], "folder_path": r[3]}
        for r in rows
    ]
