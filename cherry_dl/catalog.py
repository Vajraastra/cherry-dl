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

# idx_hash es redundante (hash ya es PRIMARY KEY), pero se mantiene por
# compatibilidad con catálogos existentes que lo tengan creado.
_CREATE_IDX = "CREATE INDEX IF NOT EXISTS idx_hash ON files(hash);"

# Índice en url_source: url_exists() hace SELECT por esta columna en cada
# archivo procesado → sin índice es un table scan O(n) por archivo.
_CREATE_IDX_URL = "CREATE INDEX IF NOT EXISTS idx_url_source ON files(url_source);"

# Migraciones para catálogos existentes creados antes de agregar estas columnas
_MIGRATE_COUNTER = """
ALTER TABLE files ADD COLUMN counter INTEGER;
"""
_MIGRATE_META = """
INSERT OR IGNORE INTO meta (key, value) VALUES ('counter', 0);
"""


# ── Inicialización ─────────────────────────────────────────────────────────────

def _db(db_path) -> aiosqlite.Connection:
    """Abre catalog.db con timeout=30 s — soporta workers concurrentes."""
    return aiosqlite.connect(db_path, timeout=30)


async def init_catalog(artist_dir: Path) -> None:
    """Crea o migra catalog.db en la carpeta del artista."""
    artist_dir.mkdir(parents=True, exist_ok=True)
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        # WAL: lectores no bloquean escritores ni viceversa
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(_CREATE_FILES)
        await db.execute(_CREATE_META)
        await db.execute(_CREATE_IDX)
        await db.execute(_CREATE_IDX_URL)

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

    Usa UPDATE … RETURNING value (SQLite 3.35+) para obtener el nuevo valor
    en una sola instrucción atómica — sin gap entre escritura y lectura que
    otro worker pudiera aprovechar para obtener el mismo contador.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        async with db.execute(
            "UPDATE meta SET value = value + 1 WHERE key = 'counter' RETURNING value"
        ) as cur:
            row = await cur.fetchone()
        await db.commit()

    if row is None:
        raise RuntimeError(
            f"catalog.db corrompido: falta la fila 'counter' en meta ({db_path}). "
            "Borra el archivo para regenerarlo."
        )
    return row[0]


# ── Consultas ──────────────────────────────────────────────────────────────────

async def url_exists(artist_dir: Path, url: str) -> bool:
    """
    Retorna True si la URL ya está registrada en el catálogo.
    Permite detectar duplicados ANTES de descargar, sin necesidad de hash.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM files WHERE url_source = ? LIMIT 1", (url,)
        ) as cur:
            return await cur.fetchone() is not None


async def hash_exists(artist_dir: Path, file_hash: str) -> bool:
    """Retorna True si el hash ya está registrado en el catálogo."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM files WHERE hash = ? LIMIT 1", (file_hash,)
        ) as cur:
            return await cur.fetchone() is not None


async def get_all_hashes(artist_dir: Path) -> set[str]:
    """Retorna todos los hashes registrados en el catálogo del artista."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return set()
    async with _db(db_path) as db:
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
    async with _db(db_path) as db:
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
    async with _db(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hash, filename, url_source, file_size, counter FROM files"
        ) as cur:
            rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def remove_file(artist_dir: Path, file_hash: str) -> None:
    """Elimina un registro del catálogo por hash (usado en repair al re-indexar)."""
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute("DELETE FROM files WHERE hash = ?", (file_hash,))
        await db.commit()


async def get_numbered_files(
    artist_dir: Path,
) -> list[tuple[int, str, str]]:
    """
    Retorna archivos numerados que existen en disco.

    Resultado: lista de (counter, filename, hash) ordenada por el
    contador extraído del nombre del archivo (no del campo counter en DB,
    que puede estar desactualizado).

    Solo incluye archivos presentes físicamente en la carpeta —
    los archivos purgados quedan en el catálogo pero se omiten aquí.
    """
    import re as _re
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return []
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT filename, hash FROM files WHERE counter IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

    result = []
    for filename, file_hash in rows:
        if not (artist_dir / filename).exists():
            continue
        m = _re.search(r'_(\d{5})\.[^.]*$', filename)
        if m:
            result.append((int(m.group(1)), filename, file_hash))

    result.sort(key=lambda x: x[0])
    return result


def plan_compaction(
    files: list[tuple[int, str, str]],
) -> list[tuple[str, str, str, int]]:
    """
    Calcula los renombres necesarios para eliminar huecos.

    Entrada: lista de (counter, filename, hash) ordenada por counter.
    Salida:  lista de (old_name, new_name, hash, new_counter).
             Solo incluye archivos cuyo nombre cambia.

    El nuevo counter se asigna secuencialmente desde 1.
    Reemplaza el patrón _NNNNN. en el nombre de archivo.
    """
    import re
    plan = []
    for new_counter, (_, filename, file_hash) in enumerate(files, start=1):
        new_name = re.sub(
            r'_(\d{5})(\.[^.]*$)',
            f'_{new_counter:05d}\\2',
            filename,
        )
        if new_name == filename:
            continue
        plan.append((filename, new_name, file_hash, new_counter))
    return plan


async def apply_compaction(
    artist_dir: Path,
    plan: list[tuple[str, str, str, int]],
    new_total: int,
) -> None:
    """
    Ejecuta el plan de compactación en dos fases (anti-colisión):

    Fase 1: old_name → old_name.tmp  (todos)
    Fase 2: old_name.tmp → new_name  (todos)

    Después actualiza catalog.db en una transacción atómica:
    - NULL-ifica registros "fantasma" que ocupan el nuevo nombre
      (archivos purgados cuyo slot se reutiliza — se preserva su hash
      para evitar re-descargas pero se limpia el filename)
    - SET filename, counter WHERE hash = ? (update por clave primaria,
      evita el problema de UPDATE encadenado por filename)
    - SET counter (meta) = new_total
    """
    if not plan:
        return

    db_path = artist_dir / CATALOG_NAME

    # Fase 1: → .tmp
    for old_name, _, _, _ in plan:
        (artist_dir / old_name).rename(
            artist_dir / (old_name + ".tmp")
        )

    # Fase 2: .tmp → new_name
    for old_name, new_name, _, _ in plan:
        (artist_dir / (old_name + ".tmp")).rename(
            artist_dir / new_name
        )

    # Actualizar DB en transacción atómica
    async with _db(db_path) as db:
        # Paso 1: "apartar" cualquier registro que ya tenga
        # filename = new_name pero que NO sea el archivo que movemos.
        # Esto evita la "reactivación" de registros de archivos
        # purgados cuyo slot es reutilizado.
        # Se usa el prefijo '_purged_' (no coincide con _\d{5}\.ext)
        # para respetar la restricción NOT NULL de la columna.
        for _, new_name, file_hash, _ in plan:
            await db.execute(
                "UPDATE files SET filename = '_purged_' || hash"
                " WHERE filename = ? AND hash != ?",
                (new_name, file_hash),
            )
        # Paso 2: actualizar por hash (clave primaria) — sin riesgo
        # de UPDATE encadenado por filename.
        for _, new_name, file_hash, new_ctr in plan:
            await db.execute(
                "UPDATE files SET filename = ?, counter = ?"
                " WHERE hash = ?",
                (new_name, new_ctr, file_hash),
            )
        await db.execute(
            "UPDATE meta SET value = ? WHERE key = 'counter'",
            (new_total,),
        )
        await db.commit()


async def get_stats(artist_dir: Path) -> dict:
    """Retorna estadísticas básicas del catálogo."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return {"total": 0, "total_size": 0}
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM files"
        ) as cur:
            row = await cur.fetchone()
    return {"total": row[0], "total_size": row[1]}
