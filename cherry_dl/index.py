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

# ── Schema Fase 2: Perfiles de artista ─────────────────────────────────────────

_CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id           INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    folder_path  TEXT NOT NULL UNIQUE,  -- {download_dir}/{site_primario}/{nombre}/
    primary_site TEXT NOT NULL,         -- sitio de la URL con la que se creó
    created_at   TEXT,
    last_checked TEXT,
    ext_filter   TEXT NOT NULL DEFAULT '' -- extensiones persistidas (p.ej. "jpg,png")
);
"""

# Migración para bases de datos existentes sin la columna ext_filter
_MIGRATE_EXT_FILTER = "ALTER TABLE profiles ADD COLUMN ext_filter TEXT NOT NULL DEFAULT ''"

_CREATE_PROFILE_URLS = """
CREATE TABLE IF NOT EXISTS profile_urls (
    id          INTEGER PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    url         TEXT,                   -- NULL en entradas migradas de Fase 1
    site        TEXT NOT NULL,
    artist_id   TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_synced TEXT,
    file_count  INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_IDX_PROFILE_URLS = (
    "CREATE INDEX IF NOT EXISTS idx_profile_urls_profile "
    "ON profile_urls(profile_id);"
)


# ── Inicialización ─────────────────────────────────────────────────────────────

async def init_index(db_path: Path) -> None:
    """
    Crea index.db si no existe y aplica migraciones.

    Fase 2: crea tablas profiles/profile_urls y migra los artistas
    existentes a perfiles implícitos (un perfil = una fuente).
    La migración es idempotente — se puede ejecutar múltiples veces.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        # Fase 1 — tablas base
        await db.execute(_CREATE_SITES)
        await db.execute(_CREATE_ARTISTS)
        await db.execute(_CREATE_IDX_ARTIST)

        # Fase 2 — tablas de perfiles
        await db.execute(_CREATE_PROFILES)
        await db.execute(_CREATE_PROFILE_URLS)
        await db.execute(_CREATE_IDX_PROFILE_URLS)

        # Migración: agregar ext_filter si no existe (bases de datos anteriores)
        async with db.execute(
            "SELECT name FROM pragma_table_info('profiles') WHERE name='ext_filter'"
        ) as cur:
            if not await cur.fetchone():
                await db.execute(_MIGRATE_EXT_FILTER)

        # Migración: artistas existentes sin perfil → perfiles implícitos
        await db.execute("""
            INSERT OR IGNORE INTO profiles (display_name, folder_path, primary_site, created_at)
            SELECT a.name, a.folder_path, s.name, datetime('now')
            FROM artists a
            JOIN sites s ON a.site_id = s.id
            WHERE a.folder_path NOT IN (SELECT folder_path FROM profiles)
        """)

        # Migración: crear profile_urls para perfiles recién migrados
        await db.execute("""
            INSERT INTO profile_urls (profile_id, url, site, artist_id)
            SELECT p.id, NULL, s.name, a.artist_id
            FROM artists a
            JOIN sites s ON a.site_id = s.id
            JOIN profiles p ON p.folder_path = a.folder_path
            WHERE NOT EXISTS (
                SELECT 1 FROM profile_urls pu
                WHERE pu.profile_id = p.id
                  AND pu.artist_id = a.artist_id
                  AND pu.site = s.name
            )
        """)

        # Limpieza: eliminar entradas migradas (url=NULL) cuando ya existe
        # una entrada real (url IS NOT NULL) con el mismo artist_id y site.
        # Esto ocurre cuando el wizard crea el perfil con URL real y la
        # descarga registra el artist_id en profile_urls; la siguiente
        # llamada a init_index ya no necesita la entrada migrada.
        await db.execute("""
            DELETE FROM profile_urls
            WHERE url IS NULL
              AND artist_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM profile_urls pu2
                WHERE pu2.profile_id  = profile_urls.profile_id
                  AND pu2.artist_id   = profile_urls.artist_id
                  AND pu2.site        = profile_urls.site
                  AND pu2.url IS NOT NULL
              )
        """)

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


async def list_profiles(db_path: Path) -> list[dict]:
    """
    Retorna todos los perfiles con el conteo de fuentes.
    Cada dict: {id, display_name, folder_path, primary_site, created_at, last_checked, url_count}
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("""
            SELECT p.id, p.display_name, p.folder_path, p.primary_site,
                   p.created_at, p.last_checked,
                   COUNT(pu.id) AS url_count
            FROM profiles p
            LEFT JOIN profile_urls pu ON pu.profile_id = p.id AND pu.enabled = 1
            GROUP BY p.id
            ORDER BY p.display_name
        """) as cur:
            rows = await cur.fetchall()
    return [
        {
            "id": r[0], "display_name": r[1], "folder_path": r[2],
            "primary_site": r[3], "created_at": r[4], "last_checked": r[5],
            "url_count": r[6],
        }
        for r in rows
    ]


async def get_profile(db_path: Path, profile_id: int) -> dict | None:
    """
    Retorna un perfil con todas sus URLs.
    Dict: {id, display_name, folder_path, primary_site, created_at, last_checked, urls: [...]}
    Cada URL: {id, profile_id, url, site, artist_id, enabled, last_synced, file_count}
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id, display_name, folder_path, primary_site, created_at, "
            "last_checked, ext_filter "
            "FROM profiles WHERE id = ?",
            (profile_id,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        profile = {
            "id": row[0], "display_name": row[1], "folder_path": row[2],
            "primary_site": row[3], "created_at": row[4], "last_checked": row[5],
            "ext_filter": row[6] or "",
        }

        async with db.execute(
            "SELECT id, profile_id, url, site, artist_id, enabled, last_synced, file_count "
            "FROM profile_urls WHERE profile_id = ? ORDER BY id",
            (profile_id,),
        ) as cur:
            url_rows = await cur.fetchall()

    profile["urls"] = [
        {
            "id": u[0], "profile_id": u[1], "url": u[2], "site": u[3],
            "artist_id": u[4], "enabled": bool(u[5]), "last_synced": u[6],
            "file_count": u[7],
        }
        for u in url_rows
    ]
    return profile


async def create_profile(
    db_path: Path,
    display_name: str,
    folder_path: Path,
    primary_site: str,
) -> int:
    """
    Crea un perfil nuevo. Retorna el ID del perfil creado.
    Las URLs se agregan después con add_profile_url().
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "INSERT INTO profiles (display_name, folder_path, primary_site, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (display_name, str(folder_path), primary_site),
        ) as cur:
            profile_id = cur.lastrowid
        await db.commit()
    return profile_id


async def add_profile_url(
    db_path: Path,
    profile_id: int,
    url: str | None,
    site: str,
    artist_id: str | None = None,
) -> int:
    """
    Agrega una URL a un perfil existente. Retorna el ID de la entrada.
    url puede ser None para entradas migradas de Fase 1.
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "INSERT INTO profile_urls (profile_id, url, site, artist_id) VALUES (?, ?, ?, ?)",
            (profile_id, url, site, artist_id),
        ) as cur:
            url_id = cur.lastrowid
        await db.commit()
    return url_id


async def update_profile_url_sync(
    db_path: Path,
    url_id: int,
    artist_id: str | None = None,
    file_count: int | None = None,
) -> None:
    """Actualiza artist_id, file_count y last_synced de una URL de perfil."""
    async with aiosqlite.connect(db_path) as db:
        if artist_id is not None:
            await db.execute(
                "UPDATE profile_urls SET artist_id = ? WHERE id = ?",
                (artist_id, url_id),
            )
        if file_count is not None:
            await db.execute(
                "UPDATE profile_urls SET file_count = ? WHERE id = ?",
                (file_count, url_id),
            )
        await db.execute(
            "UPDATE profile_urls SET last_synced = datetime('now') WHERE id = ?",
            (url_id,),
        )
        await db.commit()


async def update_profile_last_checked(db_path: Path, profile_id: int) -> None:
    """Actualiza last_checked del perfil al momento actual."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE profiles SET last_checked = datetime('now') WHERE id = ?",
            (profile_id,),
        )
        await db.commit()


async def update_profile_ext_filter(
    db_path: Path, profile_id: int, ext_filter: str
) -> None:
    """Persiste el filtro de extensiones del perfil."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE profiles SET ext_filter = ? WHERE id = ?",
            (ext_filter.strip(), profile_id),
        )
        await db.commit()


async def delete_profile(db_path: Path, profile_id: int) -> None:
    """
    Elimina un perfil, sus URLs (CASCADE) y el artista asociado.
    También borra el artista de la tabla artists para que la migración
    de init_index no lo re-cree automáticamente en la próxima carga.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        async with db.execute(
            "SELECT folder_path FROM profiles WHERE id = ?", (profile_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                "DELETE FROM artists WHERE folder_path = ?", (row[0],)
            )
        await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()


async def merge_profiles(
    db_path: Path,
    keep_id: int,
    remove_id: int,
) -> int:
    """
    Fusiona el perfil remove_id en keep_id.

    - Todas las profile_urls de remove_id se reasignan a keep_id.
    - Se elimina el perfil remove_id y su entrada en artists.
    - La carpeta en disco NO se toca — el llamador es responsable de
      mover los archivos únicos antes de llamar a esta función.

    Retorna el número de URLs reasignadas.
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys = OFF")   # gestión manual

        # Reasignar URLs del perfil a eliminar
        async with db.execute(
            "UPDATE profile_urls SET profile_id = ? WHERE profile_id = ?",
            (keep_id, remove_id),
        ) as cur:
            moved = cur.rowcount

        # Eliminar entrada de artists vinculada al perfil que desaparece
        async with db.execute(
            "SELECT folder_path FROM profiles WHERE id = ?", (remove_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                "DELETE FROM artists WHERE folder_path = ?", (row[0],)
            )

        # Eliminar el perfil (sus URLs ya fueron reasignadas)
        await db.execute("DELETE FROM profiles WHERE id = ?", (remove_id,))
        await db.commit()

    return moved


async def set_profile_url_enabled(
    db_path: Path, url_id: int, enabled: bool
) -> None:
    """Activa o desactiva una URL de perfil."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE profile_urls SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, url_id),
        )
        await db.commit()


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
