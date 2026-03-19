"""
Catálogo por artista — catalog.db.
Cada carpeta de artista contiene su propio catalog.db con hashes y metadatos.
El catálogo viaja junto a los archivos, haciendo la colección auto-contenida.

Esquema:
  files — registro de cada archivo (hash, nombre final, URL, fecha, tamaño, contador)
  meta  — valores únicos por catálogo (ej: contador global de archivos del artista)
"""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

CATALOG_NAME = "catalog.db"

# ── Schema ─────────────────────────────────────────────────────────────────────

_CREATE_FILES = """
CREATE TABLE IF NOT EXISTS files (
    hash        TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,       -- nombre final en disco (con prefijo artista)
    url_source  TEXT,
    date_added  INTEGER NOT NULL,    -- unix timestamp
    file_size   INTEGER,             -- bytes
    counter     INTEGER              -- número secuencial global del artista
);
"""

# Tabla meta: almacena el contador global incremental del artista
_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_IDX = "CREATE INDEX IF NOT EXISTS idx_hash ON files(hash);"

# Migraciones para catálogos existentes creados antes de agregar estas columnas
_MIGRATE_COUNTER = """
ALTER TABLE files ADD COLUMN counter INTEGER;
"""
_MIGRATE_META = """
INSERT OR IGNORE INTO meta (key, value) VALUES ('counter', 0);
"""


# ── Inicialización ─────────────────────────────────────────────────────────────

async def init_catalog(artist_dir: Path) -> None:
    """Crea o migra catalog.db en la carpeta del artista."""
    artist_dir.mkdir(parents=True, exist_ok=True)
    db_path = artist_dir / CATALOG_NAME
    async with aiosqlite.connect(db_path) as db:
        await db.execute(_CREATE_FILES)
        await db.execute(_CREATE_META)
        await db.execute(_CREATE_IDX)

        # Migrar columna counter si no existe (catálogos previos)
        async with db.execute(
            "SELECT name FROM pragma_table_info('files') WHERE name='counter'"
        ) as cur:
            if not await cur.fetchone():
                await db.execute(_MIGRATE_COUNTER)

        # Asegurar fila del contador en meta
        await db.execute(_MIGRATE_META)
        await db.commit()


# ── Contador global del artista ────────────────────────────────────────────────

async def next_counter(artist_dir: Path) -> int:
    """
    Incrementa atómicamente y retorna el siguiente número secuencial del artista.
    SQLite serializa escrituras, por lo que esto es seguro con múltiples workers.
    """
    db_path = artist_dir / CATALOG_NAME
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE meta SET value = value + 1 WHERE key = 'counter'"
        )
        async with db.execute(
            "SELECT value FROM meta WHERE key = 'counter'"
        ) as cur:
            row = await cur.fetchone()
        await db.commit()
    return row[0]


# ── Consultas ──────────────────────────────────────────────────────────────────

async def url_exists(artist_dir: Path, url: str) -> bool:
    """
    Retorna True si la URL ya está registrada en el catálogo.
    Permite detectar duplicados ANTES de descargar, sin necesidad de hash.
    """
    db_path = artist_dir / CATALOG_NAME
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM files WHERE url_source = ? LIMIT 1", (url,)
        ) as cur:
            return await cur.fetchone() is not None


async def hash_exists(artist_dir: Path, file_hash: str) -> bool:
    """Retorna True si el hash ya está registrado en el catálogo."""
    db_path = artist_dir / CATALOG_NAME
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM files WHERE hash = ? LIMIT 1", (file_hash,)
        ) as cur:
            return await cur.fetchone() is not None


async def get_all_hashes(artist_dir: Path) -> set[str]:
    """Retorna todos los hashes registrados en el catálogo del artista."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return set()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT hash FROM files") as cur:
            rows = await cur.fetchall()
    return {row[0] for row in rows}


async def add_file(
    artist_dir: Path,
    file_hash: str,
    filename: str,
    url_source: str | None = None,
    file_size: int | None = None,
    counter: int | None = None,
) -> None:
    """Registra un archivo nuevo en el catálogo."""
    db_path = artist_dir / CATALOG_NAME
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO files
                (hash, filename, url_source, date_added, file_size, counter)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_hash, filename, url_source, int(time.time()), file_size, counter),
        )
        await db.commit()


async def get_all_files(artist_dir: Path) -> list[dict]:
    """
    Retorna todos los registros del catálogo como lista de dicts.
    Útil para repair: comparar archivos físicos contra el catálogo.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return []
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hash, filename, url_source, file_size, counter FROM files"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def remove_file(artist_dir: Path, file_hash: str) -> None:
    """Elimina un registro del catálogo por hash (usado en repair al re-indexar)."""
    db_path = artist_dir / CATALOG_NAME
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM files WHERE hash = ?", (file_hash,))
        await db.commit()


async def get_stats(artist_dir: Path) -> dict:
    """Retorna estadísticas básicas del catálogo."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return {"total": 0, "total_size": 0}
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files"
        ) as cur:
            row = await cur.fetchone()
    return {"total": row[0], "total_size": row[1]}
