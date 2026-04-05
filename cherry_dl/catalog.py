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

# Cola de descarga persistente por artista.
# Cada URL descubierta se agrega aquí antes de descargar; se elimina al
# completarse. Si el proceso se interrumpe, la cola sobrevive en disco y
# la próxima sesión retoma sin re-escanear la API.
# profile_url_id identifica qué fuente descubrió el archivo — permite que
# un artista con múltiples fuentes (kemono + patreon) tenga colas separadas.
_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS pending_queue (
    url_source      TEXT PRIMARY KEY,
    download_url    TEXT NOT NULL,
    filename_hint   TEXT NOT NULL,
    post_id         TEXT,
    post_published  TEXT,
    remote_hash     TEXT,
    extra_headers   TEXT,
    profile_url_id  INTEGER,
    discovered_at   INTEGER NOT NULL
);
"""

_CREATE_IDX_PENDING = (
    "CREATE INDEX IF NOT EXISTS idx_pending_url_id "
    "ON pending_queue(profile_url_id);"
)

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
        await db.execute(_CREATE_PENDING)
        await db.execute(_CREATE_IDX_PENDING)

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


# ── Meta genérica (enteros) ────────────────────────────────────────────────────

async def set_meta_int(artist_dir: Path, key: str, value: int) -> None:
    """
    Guarda (o actualiza) un entero en la tabla meta con clave arbitraria.
    Útil para guardar totales de batch, fronteras de scan, etc.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def get_meta_int(artist_dir: Path, key: str) -> int | None:
    """
    Lee un entero de la tabla meta por clave. Retorna None si no existe.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return None
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ── Cola de pendientes ─────────────────────────────────────────────────────────

async def add_pending(
    artist_dir: Path,
    url_source: str,
    download_url: str,
    filename_hint: str,
    post_id: str = "",
    post_published: str = "",
    remote_hash: str = "",
    extra_headers: str | None = None,
    profile_url_id: int | None = None,
) -> None:
    """
    Agrega un archivo a la cola de pendientes (INSERT OR IGNORE).
    Si la url_source ya existe no hace nada — seguro llamar múltiples veces.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO pending_queue
                (url_source, download_url, filename_hint, post_id,
                 post_published, remote_hash, extra_headers,
                 profile_url_id, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url_source, download_url, filename_hint,
                post_id or "", post_published or "", remote_hash or "",
                extra_headers, profile_url_id, int(time.time()),
            ),
        )
        await db.commit()


async def pending_url_exists(artist_dir: Path, url_source: str) -> bool:
    """Retorna True si la URL ya está en la cola de pendientes."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return False
    async with _db(db_path) as db:
        async with db.execute(
            "SELECT 1 FROM pending_queue WHERE url_source = ? LIMIT 1",
            (url_source,),
        ) as cur:
            return await cur.fetchone() is not None


async def pending_count(
    artist_dir: Path,
    profile_url_id: int | None = None,
) -> int:
    """
    Retorna la cantidad de archivos en la cola de pendientes.
    Si profile_url_id está definido, filtra por esa fuente.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0
    async with _db(db_path) as db:
        if profile_url_id is not None:
            async with db.execute(
                "SELECT COUNT(*) FROM pending_queue WHERE profile_url_id = ?",
                (profile_url_id,),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with db.execute(
                "SELECT COUNT(*) FROM pending_queue"
            ) as cur:
                row = await cur.fetchone()
    return row[0] if row else 0


async def get_pending_files(
    artist_dir: Path,
    profile_url_id: int | None = None,
) -> list[dict]:
    """
    Retorna los archivos pendientes de descarga como lista de dicts.
    Si profile_url_id está definido, filtra por esa fuente.
    Ordenados por orden de descubrimiento (FIFO).
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return []
    async with _db(db_path) as db:
        db.row_factory = aiosqlite.Row
        if profile_url_id is not None:
            async with db.execute(
                """
                SELECT url_source, download_url, filename_hint,
                       post_id, post_published, remote_hash, extra_headers
                FROM pending_queue
                WHERE profile_url_id = ?
                ORDER BY discovered_at
                """,
                (profile_url_id,),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with db.execute(
                """
                SELECT url_source, download_url, filename_hint,
                       post_id, post_published, remote_hash, extra_headers
                FROM pending_queue
                ORDER BY discovered_at
                """
            ) as cur:
                rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def remove_pending(artist_dir: Path, url_source: str) -> None:
    """
    Elimina un archivo de la cola de pendientes.
    Llamar tras descarga exitosa o skip por dedup.
    """
    db_path = artist_dir / CATALOG_NAME
    async with _db(db_path) as db:
        await db.execute(
            "DELETE FROM pending_queue WHERE url_source = ?", (url_source,)
        )
        await db.commit()


async def compare_catalogs(folder_a: Path, folder_b: Path) -> dict:
    """
    Compara dos catálogos de artista por hashes SHA-256.

    Diseñado para detectar perfiles duplicados: si una fracción alta de los
    hashes de B ya existe en A, ambas carpetas probablemente son el mismo artista.

    Retorna:
        total_a     — archivos en catálogo A (el más grande / más antiguo)
        total_b     — archivos en catálogo B (candidato a fusión)
        matches     — hashes presentes en ambos catálogos
        coverage    — fracción de B que ya existe en A  (0.0 – 1.0)
        unique_to_b — hashes en B que NO están en A (necesitarían moverse)
    """
    hashes_a = await get_all_hashes(folder_a)
    hashes_b = await get_all_hashes(folder_b)

    if not hashes_b:
        return {
            "total_a": len(hashes_a),
            "total_b": 0,
            "matches": 0,
            "coverage": 0.0,
            "unique_to_b": [],
        }

    common = hashes_a & hashes_b
    unique = hashes_b - hashes_a
    coverage = len(common) / len(hashes_b)

    return {
        "total_a":     len(hashes_a),
        "total_b":     len(hashes_b),
        "matches":     len(common),
        "coverage":    coverage,
        "unique_to_b": list(unique),
    }


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
