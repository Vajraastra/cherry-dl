"""
Catálogo por artista — catalog.db.
Cada carpeta de artista contiene su propio catalog.db con hashes y metadatos.
El catálogo viaja junto a los archivos, haciendo la colección auto-contenida.

Esquema:
  files — registro de cada archivo (hash, nombre final, URL, fecha, tamaño, contador)
  meta  — valores únicos por catálogo (ej: contador global de archivos del artista)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Callable

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

# Metadata del perfil embebida en el catálogo → la carpeta es auto-describible.
# Permite reconstruir index.db (caché por-máquina) desde las carpetas al cambiar
# de OS, sin perder las URLs de scrape ni el estado de sincronización.
# Fila única (id=1) con un blob JSON: display_name, primary_site, urls[], ext_filter.
_CREATE_PROFILE_META = """
CREATE TABLE IF NOT EXISTS profile_meta (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    data  TEXT NOT NULL
);
"""

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
        await db.execute(_CREATE_PROFILE_META)

        # Migrar columna counter si no existe (catálogos previos)
        async with db.execute(
            "SELECT name FROM pragma_table_info('files') WHERE name='counter'"
        ) as cur:
            if not await cur.fetchone():
                await db.execute(_MIGRATE_COUNTER)

        # Asegurar fila del contador en meta
        await db.execute(_MIGRATE_META)
        await db.commit()


# ── Metadata del perfil (auto-descripción para reindex cross-OS) ───────────────

async def write_profile_meta(artist_dir: Path, meta: dict) -> None:
    """Persiste/actualiza la metadata del perfil dentro del catalog.db.

    `meta` debe contener al menos: display_name, primary_site, urls (lista de
    dicts con url/site/artist_id/enabled/last_synced/file_count) y ext_filter.
    """
    db_path = artist_dir / CATALOG_NAME
    payload = json.dumps(meta, ensure_ascii=False)
    async with _db(db_path) as db:
        await db.execute(_CREATE_PROFILE_META)   # idempotente (catálogos previos)
        await db.execute(
            "INSERT INTO profile_meta (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (payload,),
        )
        await db.commit()


async def read_profile_meta(artist_dir: Path) -> dict | None:
    """Lee la metadata del perfil del catalog.db. None si no existe o es inválida."""
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return None
    async with _db(db_path) as db:
        await db.execute(_CREATE_PROFILE_META)
        async with db.execute(
            "SELECT data FROM profile_meta WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


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

    # replace() (no rename()): en Windows rename lanza FileExistsError si el
    # destino existe (slots reutilizados tras purgas); replace sobrescribe en
    # ambos OS de forma atómica.
    # Fase 1: → .tmp
    for old_name, _, _, _ in plan:
        (artist_dir / old_name).replace(
            artist_dir / (old_name + ".tmp")
        )

    # Fase 2: .tmp → new_name
    for old_name, new_name, _, _ in plan:
        (artist_dir / (old_name + ".tmp")).replace(
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

async def clean_pending_catalog_overlap(
    artist_dir: Path,
    profile_url_id: int | None = None,
) -> int:
    """
    Elimina de pending_queue los archivos que ya están en el catálogo
    (descargados en sesiones anteriores pero no removidos de la cola).
    Retorna el número de entradas eliminadas.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0

    pu_clause = "AND profile_url_id = ?" if profile_url_id is not None else ""
    params    = [profile_url_id] if profile_url_id is not None else []

    async with _db(db_path) as db:
        async with db.execute(
            f"SELECT COUNT(*) FROM pending_queue "
            f"WHERE url_source IN (SELECT url_source FROM files) {pu_clause}",
            params,
        ) as cur:
            row = await cur.fetchone()
        removed = row[0] if row else 0
        if removed:
            await db.execute(
                f"DELETE FROM pending_queue "
                f"WHERE url_source IN (SELECT url_source FROM files) {pu_clause}",
                params,
            )
            await db.commit()
    return removed


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
    ext_filter: set[str] | None = None,
) -> int:
    """
    Retorna la cantidad de archivos en la cola de pendientes.
    - profile_url_id: filtra por fuente (opcional).
    - ext_filter: set de extensiones con punto, p.ej. {'.jpg','.zip'}.
      Si está vacío o es None, cuenta todo.
    """
    db_path = artist_dir / CATALOG_NAME
    if not db_path.exists():
        return 0

    conditions: list[str] = []
    params: list = []

    if profile_url_id is not None:
        conditions.append("profile_url_id = ?")
        params.append(profile_url_id)

    if ext_filter:
        # filename_hint termina en una de las extensiones del filtro
        like_clauses = " OR ".join(
            "LOWER(filename_hint) LIKE ?" for _ in ext_filter
        )
        conditions.append(f"({like_clauses})")
        params.extend(f"%{ext.lower()}" for ext in ext_filter)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql   = f"SELECT COUNT(*) FROM pending_queue {where}"

    async with _db(db_path) as db:
        async with db.execute(sql, params) as cur:
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


async def compare_by_hash_join(folder_a: Path, folder_b: Path) -> dict:
    """
    Compara dos catálogos por hashes usando SQL ATTACH + INNER JOIN.
    No lee archivos del disco — solo consulta las catalog.db existentes.

    Retorna:
        total_a  — archivos registrados en A
        total_b  — archivos registrados en B
        matches  — hashes presentes en ambos catálogos
        coverage — matches / min(total_a, total_b)  (métrica simétrica)
    """
    db_a = folder_a / CATALOG_NAME
    db_b = folder_b / CATALOG_NAME

    if not db_a.exists() or not db_b.exists():
        return {"total_a": 0, "total_b": 0, "matches": 0, "coverage": 0.0}

    async with aiosqlite.connect(db_a, timeout=30) as db:
        await db.execute("ATTACH DATABASE ? AS cat_b", (str(db_b),))
        try:
            # Un catalog.db de un perfil sin descargar sólo tiene `profile_meta`
            # (lo crea write_profile_meta), no `files`. Si falta la tabla en
            # cualquiera de los dos no puede haber solape de hashes → 0.
            # Además, SIEMPRE cualificar con main./cat_b.: un `FROM files` sin
            # prefijo, si `files` no existe en main, cae a la base adjunta y
            # cuenta los archivos del OTRO catálogo → falso 100%.
            async with db.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM main.sqlite_master "
                " WHERE type='table' AND name='files'), "
                "(SELECT COUNT(*) FROM cat_b.sqlite_master "
                " WHERE type='table' AND name='files')"
            ) as cur:
                has_a, has_b = await cur.fetchone()
            if not has_a or not has_b:
                return {"total_a": 0, "total_b": 0, "matches": 0, "coverage": 0.0}

            async with db.execute("SELECT COUNT(*) FROM main.files") as cur:
                total_a = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM cat_b.files") as cur:
                total_b = (await cur.fetchone())[0]

            if total_a == 0 or total_b == 0:
                return {
                    "total_a": total_a, "total_b": total_b,
                    "matches": 0, "coverage": 0.0,
                }

            async with db.execute(
                "SELECT COUNT(*) FROM main.files a "
                "INNER JOIN cat_b.files b ON a.hash = b.hash"
            ) as cur:
                matches = (await cur.fetchone())[0]
        finally:
            try:
                await db.execute("DETACH DATABASE cat_b")
            except Exception:
                pass

    coverage = matches / min(total_a, total_b)
    return {
        "total_a":  total_a,
        "total_b":  total_b,
        "matches":  matches,
        "coverage": coverage,
    }


async def migrate_unique_files(
    folder_src: Path,
    folder_dst: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """
    Mueve archivos únicos (por hash) de folder_src a folder_dst.

    - Hash NO existe en dst + archivo existe en disco  → mover, registrar en dst
    - Hash SÍ existe en dst                            → dejar en src (huérfano)
    - En catalog de src pero no en disco               → omitir (purgado)

    Retorna:
        moved          — archivos migrados exitosamente
        orphaned       — archivos que ya existen en dst (quedan en src)
        purged         — archivos en catalog sin presencia física (omitidos)
        errors         — lista de strings describiendo fallos
        orphaned_paths — rutas físicas de los archivos huérfanos en src
    """
    src_files = await get_all_files(folder_src)
    total = len(src_files)

    moved    = 0
    orphaned = 0
    purged   = 0
    errors: list[str] = []
    orphaned_paths: list[Path] = []

    for i, f in enumerate(src_files):
        src_path = folder_src / f["filename"]

        if not src_path.exists():
            purged += 1
            if on_progress:
                on_progress(i + 1, total)
            continue

        if await hash_exists(folder_dst, f["hash"]):
            orphaned += 1
            orphaned_paths.append(src_path)
            if on_progress:
                on_progress(i + 1, total)
            continue

        try:
            ctr  = await next_counter(folder_dst)
            ext  = Path(f["filename"]).suffix
            pad  = max(4, len(str(ctr)))
            new_name = f"{ctr:0{pad}d}{ext}"
            new_path = folder_dst / new_name

            await asyncio.to_thread(shutil.move, str(src_path), str(new_path))
            await add_file(
                folder_dst, f["hash"], new_name,
                f.get("url_source"), f.get("file_size"), ctr,
            )
            moved += 1
        except Exception as exc:
            errors.append(f"{f['filename']}: {exc}")

        if on_progress:
            on_progress(i + 1, total)

    return {
        "moved":          moved,
        "orphaned":       orphaned,
        "purged":         purged,
        "errors":         errors,
        "orphaned_paths": orphaned_paths,
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
