"""
Bridge entre el event loop asyncio y el hilo principal de Dear PyGui.

DPG corre en el hilo principal. El engine de descargas usa asyncio.
Este módulo:
  - Levanta un event loop asyncio en un hilo daemon
  - Expone submit() para despachar coroutines desde el hilo principal
  - Define ProgressUpdate (dataclass) para comunicar estado a la UI
  - Implementa download_for_gui() — versión del downloader sin Rich
"""

from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Extensiones a considerar al escanear archivos locales (excluir .db, .json, etc.)
_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif",
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv",
    ".mp3", ".ogg", ".flac", ".wav", ".aac",
    ".zip", ".rar", ".7z", ".pdf", ".psd", ".clip",
}


# ── Mensajes de progreso ───────────────────────────────────────────────────────

@dataclass
class ProgressUpdate:
    """
    Mensaje enviado desde el worker async al hilo principal de DPG.

    update_type:
      started         — el artista fue identificado, comienza el listado
      scanning        — fase 1: recopilando lista de archivos desde la API
      scan_done       — fase 1 terminada, files_total contiene el total real
      prescan_start   — iniciando escaneo de carpeta existente
      prescan_file    — archivo procesado durante pre-scan
      prescan_done    — pre-scan terminado, iniciando descarga
      file_done       — archivo nuevo descargado y catalogado
      file_renamed    — archivo existente renombrado al formato cherry-dl (filename=nombre_viejo, error=nombre_nuevo)
      file_skip       — archivo duplicado ignorado
      error           — error en un archivo (no fatal); error_kind clasifica el tipo
      completed       — descarga del artista terminada
      fatal           — error fatal (no se pudo iniciar la descarga)
      collections     — datos de colecciones cargados (payload = lista)
    """
    session_id: str
    update_type: str
    artist_name: str = ""
    filename: str = ""
    files_done: int = 0
    files_total: int = 0
    error: str = ""
    error_kind: str = ""          # uno de ErrorKind.* cuando update_type == "error"
    payload: Any = field(default=None, compare=False)


# ── Bridge asyncio ─────────────────────────────────────────────────────────────

class AsyncBridge:
    """
    Levanta un asyncio event loop en un hilo daemon.
    Permite enviar coroutines desde el hilo principal con submit().
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="cherry-async"
        )

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()  # esperar hasta que el loop esté corriendo

    def submit(self, coro) -> "asyncio.Future":
        """Despacha una coroutine al loop asyncio desde cualquier hilo."""
        if self._loop is None:
            raise RuntimeError("AsyncBridge no iniciado")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()


# ── Utilidades de nombres ──────────────────────────────────────────────────────

def _safe_dirname(name: str) -> str:
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    return name.strip("._") or "unknown"


def _safe_prefix(name: str) -> str:
    """Genera un prefijo limpio para nombres de archivo: solo alfanum y guiones."""
    import re
    prefix = re.sub(r"[^\w\-]", "_", name).strip("_")
    return prefix[:40] or "artist"  # máx 40 chars para no hacer nombres kilométricos


def build_filename(artist_name: str, counter: int, original_filename: str) -> str:
    """
    Construye el nombre final del archivo:
      {prefijo_artista}_{contador:05d}{extensión_original}

    Ejemplos:
      ViciNeko_00001.jpg
      SomeArtist_00042.psd
      another_artist_00100.png
    """
    from pathlib import Path
    ext = Path(original_filename).suffix.lower()
    prefix = _safe_prefix(artist_name)
    return f"{prefix}_{counter:05d}{ext}"


# ── Descarga para GUI (sin Rich) ───────────────────────────────────────────────


def _parse_ext_filter(raw: str | None) -> set[str]:
    """
    Convierte una cadena como '.zip, .rar, jpg' en un set normalizado:
    {'.zip', '.rar', '.jpg'}
    Retorna set vacío si raw es None o cadena vacía.
    """
    if not raw:
        return set()
    result = set()
    for part in raw.split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        result.add(ext)
    return result


def _passes_ext_filter(filename: str, ext_filter: set[str], exclude_mode: bool) -> bool:
    """
    Retorna True si el archivo debe descargarse según el filtro.
      exclude_mode=True  → excluir las extensiones listadas (descargar todo lo demás)
      exclude_mode=False → incluir solo las extensiones listadas
    Si ext_filter está vacío, siempre retorna True.
    """
    if not ext_filter:
        return True
    ext = Path(filename).suffix.lower()
    if exclude_mode:
        return ext not in ext_filter
    else:
        return ext in ext_filter


async def download_for_gui(
    url: str,
    config,                        # UserConfig
    workers: int,
    updates: "queue.Queue[ProgressUpdate]",
    session_id: str,
    ext_filter: set[str] | None = None,
    exclude_mode: bool = True,
) -> None:
    """
    Descarga todos los archivos de un artista y envía actualizaciones
    a la cola `updates` para que la GUI las procese en el hilo principal.
    """
    from ..config import INDEX_DB
    from ..engine import DownloadEngine
    from ..templates._registry import get_template
    from ..catalog import init_catalog, hash_exists, url_exists, add_file, next_counter
    from ..index import init_index, get_or_create_site, get_or_create_artist

    def put(update_type: str, **kwargs) -> None:
        updates.put(ProgressUpdate(
            session_id=session_id,
            update_type=update_type,
            **kwargs,
        ))

    try:
        async with DownloadEngine(config, workers=workers) as engine:
            template = get_template(url, engine)
            if template is None:
                put("fatal", error=f"Sin template para: {url}")
                return

            # Obtener info del artista
            artist = await template.get_artist_info(url)
            put("started", artist_name=artist.name)

            # Preparar directorio y catálogo
            artist_dir = config.download_path / artist.site / _safe_dirname(artist.name)
            artist_dir.mkdir(parents=True, exist_ok=True)
            await init_catalog(artist_dir)

            # Registrar en índice central
            await init_index(INDEX_DB)
            site_id = await get_or_create_site(INDEX_DB, artist.site)
            await get_or_create_artist(
                db_path=INDEX_DB,
                site_id=site_id,
                artist_id=artist.artist_id,
                name=artist.name,
                folder_path=artist_dir,
            )

            # Mapa de archivos locales existentes {hash: path} para detectar
            # archivos con nombres incorrectos y renombrarlos al vuelo
            local_hashes = await _build_local_hash_map(artist_dir)
            _filter = ext_filter or set()

            files_done = 0
            files_total = 0

            async for file_info in template.iter_files(artist):
                # Aplicar filtro de extensiones antes de descargar
                if not _passes_ext_filter(file_info.filename, _filter, exclude_mode):
                    continue

                # Skip por hash remoto (extraído del path del servidor, sin descargar)
                if file_info.remote_hash and await hash_exists(artist_dir, file_info.remote_hash):
                    put("file_skip",
                        filename=file_info.filename,
                        files_done=files_done,
                        files_total=files_total)
                    continue

                # Skip por URL (fallback si el sitio no expone hash en el path)
                if await url_exists(artist_dir, file_info.url):
                    put("file_skip",
                        filename=file_info.filename,
                        files_done=files_done,
                        files_total=files_total)
                    continue

                files_total += 1
                counter = await next_counter(artist_dir)
                final_name = build_filename(artist.name, counter, file_info.filename)

                result = await engine.download(
                    url=file_info.url,
                    dest_dir=artist_dir,
                    filename=final_name,
                )

                if not result.ok:
                    put("error",
                        filename=file_info.filename,
                        error=result.error or "",
                        error_kind=result.error_kind or "",
                        files_done=files_done,
                        files_total=files_total)
                    continue

                if await hash_exists(artist_dir, result.file_hash):
                    # Ya en catálogo → duplicado normal
                    if result.dest and result.dest.exists():
                        result.dest.unlink()
                    put("file_skip",
                        filename=file_info.filename,
                        files_done=files_done,
                        files_total=files_total)

                elif result.file_hash in local_hashes:
                    # Existe físicamente con otro nombre → renombrar al formato cherry-dl
                    old_path = local_hashes[result.file_hash]
                    new_path = artist_dir / final_name
                    try:
                        old_path.rename(new_path)
                        local_hashes[result.file_hash] = new_path
                    except Exception:
                        new_path = old_path  # si falla el rename, registrar con nombre original
                    # Eliminar el archivo recién descargado (ya tenemos el original)
                    if result.dest and result.dest.exists():
                        result.dest.unlink()
                    await add_file(
                        artist_dir=artist_dir,
                        file_hash=result.file_hash,
                        filename=final_name,
                        url_source=file_info.url,
                        file_size=result.file_size,
                        counter=counter,
                    )
                    files_done += 1
                    put("file_renamed",
                        filename=old_path.name,
                        error=final_name,   # reutilizamos error como "nuevo nombre"
                        files_done=files_done,
                        files_total=files_total)

                else:
                    # Archivo nuevo
                    await add_file(
                        artist_dir=artist_dir,
                        file_hash=result.file_hash,
                        filename=final_name,
                        url_source=file_info.url,
                        file_size=result.file_size,
                        counter=counter,
                    )
                    local_hashes[result.file_hash] = result.dest
                    files_done += 1
                    put("file_done",
                        filename=final_name,
                        files_done=files_done,
                        files_total=files_total)

        put("completed",
            artist_name=artist.name,
            files_done=files_done,
            files_total=files_total)

    except Exception as exc:
        put("fatal", error=str(exc))


async def prescan_and_download(
    url: str,
    source_dir: Path,
    config,
    workers: int,
    updates: "queue.Queue[ProgressUpdate]",
    session_id: str,
    ext_filter: set[str] | None = None,
    exclude_mode: bool = True,
) -> None:
    """
    Pre-escanea una carpeta existente y luego inicia la descarga normal.

    Flujo:
      1. Resolver nombre del artista desde la URL
      2. Crear directorio del artista e inicializar catalog.db
      3. Mover y renombrar archivos de source_dir → artist_dir, indexarlos
      4. Iniciar descarga — los hashes ya indexados se saltarán automáticamente
    """
    from ..config import INDEX_DB
    from ..engine import DownloadEngine
    from ..templates._registry import get_template
    from ..catalog import init_catalog
    from ..index import init_index, get_or_create_site, get_or_create_artist
    from ..organizer import organize

    def put(update_type: str, **kwargs) -> None:
        updates.put(ProgressUpdate(
            session_id=session_id,
            update_type=update_type,
            **kwargs,
        ))

    try:
        async with DownloadEngine(config, workers=workers) as engine:
            template = get_template(url, engine)
            if template is None:
                put("fatal", error=f"Sin template para: {url}")
                return

            # 1. Resolver artista
            artist = await template.get_artist_info(url)
            put("started", artist_name=artist.name)

            # 2. Preparar directorio
            artist_dir = config.download_path / artist.site / _safe_dirname(artist.name)
            artist_dir.mkdir(parents=True, exist_ok=True)
            await init_catalog(artist_dir)

            await init_index(INDEX_DB)
            site_id = await get_or_create_site(INDEX_DB, artist.site)
            await get_or_create_artist(
                db_path=INDEX_DB,
                site_id=site_id,
                artist_id=artist.artist_id,
                name=artist.name,
                folder_path=artist_dir,
            )

            # 3. Pre-scan de la carpeta existente
            put("prescan_start", artist_name=artist.name)

            def on_progress(processed: int, total: int, filename: str) -> None:
                updates.put(ProgressUpdate(
                    session_id=session_id,
                    update_type="prescan_file",
                    filename=filename,
                    files_done=processed,
                    files_total=total,
                ))

            scan_result, _ = await organize(
                source_dir=source_dir,
                artist_name=artist.name,
                artist_id=artist.artist_id,
                site=artist.site,
                dest_root=config.download_path,
                progress_cb=on_progress,
            )

            put("prescan_done",
                artist_name=artist.name,
                files_done=scan_result.moved,
                files_total=scan_result.total_scanned)

            # 4. Descarga normal (los hashes pre-indexados se saltarán)
            from ..catalog import hash_exists, url_exists, add_file, next_counter

            _filter = ext_filter or set()
            files_done = 0
            files_total = 0

            async for file_info in template.iter_files(artist):
                if not _passes_ext_filter(file_info.filename, _filter, exclude_mode):
                    continue

                if file_info.remote_hash and await hash_exists(artist_dir, file_info.remote_hash):
                    put("file_skip",
                        filename=file_info.filename,
                        files_done=files_done,
                        files_total=files_total)
                    continue

                if await url_exists(artist_dir, file_info.url):
                    put("file_skip",
                        filename=file_info.filename,
                        files_done=files_done,
                        files_total=files_total)
                    continue

                files_total += 1
                counter = await next_counter(artist_dir)
                final_name = build_filename(artist.name, counter, file_info.filename)

                result = await engine.download(
                    url=file_info.url,
                    dest_dir=artist_dir,
                    filename=final_name,
                )

                if not result.ok:
                    put("error",
                        filename=file_info.filename,
                        error=result.error or "",
                        error_kind=result.error_kind or "",
                        files_done=files_done,
                        files_total=files_total)
                    continue

                if await hash_exists(artist_dir, result.file_hash):
                    if result.dest and result.dest.exists():
                        result.dest.unlink()
                    put("file_skip",
                        filename=file_info.filename,
                        files_done=files_done,
                        files_total=files_total)
                else:
                    await add_file(
                        artist_dir=artist_dir,
                        file_hash=result.file_hash,
                        filename=final_name,
                        url_source=file_info.url,
                        file_size=result.file_size,
                        counter=counter,
                    )
                    files_done += 1
                    put("file_done",
                        filename=final_name,
                        files_done=files_done,
                        files_total=files_total)

        put("completed",
            artist_name=artist.name,
            files_done=files_done,
            files_total=files_total)

    except Exception as exc:
        put("fatal", error=str(exc))


async def repair_async(
    artist_data: dict,
    config,
    workers: int,
    updates: "queue.Queue[ProgressUpdate]",
    session_id: str,
) -> None:
    """
    Repara una colección re-descargando archivos físicamente ausentes.

    Flujo:
      1. Lee catalog.db → lista completa de archivos registrados
      2. Compara contra archivos físicos en la carpeta del artista
      3. Los que faltan y tienen url_source → re-descarga
      4. Elimina y re-registra en catalog.db con el mismo counter
    """
    from ..engine import DownloadEngine
    from ..catalog import get_all_files, add_file, remove_file, init_catalog

    def put(update_type: str, **kwargs) -> None:
        updates.put(ProgressUpdate(
            session_id=session_id,
            update_type=update_type,
            **kwargs,
        ))

    artist_name = artist_data.get("name", artist_data.get("artist_id", "?"))
    folder = Path(artist_data.get("folder_path", ""))

    try:
        if not folder.exists():
            put("fatal", error=f"Carpeta no encontrada: {folder}")
            return

        await init_catalog(folder)
        all_files = await get_all_files(folder)

        # Detectar archivos faltantes con URL conocida
        missing = [
            f for f in all_files
            if f["url_source"]
            and not (folder / f["filename"]).exists()
        ]

        files_total = len(missing)
        put("started", artist_name=artist_name, files_total=files_total)

        if files_total == 0:
            put("completed", artist_name=artist_name, files_done=0, files_total=0)
            return

        files_done = 0
        async with DownloadEngine(config, workers=workers) as engine:
            for entry in missing:
                result = await engine.download(
                    url=entry["url_source"],
                    dest_dir=folder,
                    filename=entry["filename"],
                )

                if not result.ok:
                    put("error",
                        filename=entry["filename"],
                        error=result.error or "",
                        error_kind=result.error_kind or "",
                        files_done=files_done,
                        files_total=files_total)
                    continue

                # Re-registrar con el hash real (puede diferir si el archivo cambió en origen)
                await remove_file(folder, entry["hash"])
                await add_file(
                    artist_dir=folder,
                    file_hash=result.file_hash,
                    filename=entry["filename"],
                    url_source=entry["url_source"],
                    file_size=result.file_size,
                    counter=entry["counter"],
                )
                files_done += 1
                put("file_done",
                    filename=entry["filename"],
                    files_done=files_done,
                    files_total=files_total)

        put("completed", artist_name=artist_name,
            files_done=files_done, files_total=files_total)

    except Exception as exc:
        put("fatal", error=str(exc))


async def update_async(
    artist_data: dict,
    config,
    workers: int,
    updates: "queue.Queue[ProgressUpdate]",
    session_id: str,
    ext_filter: set[str] | None = None,
    exclude_mode: bool = True,
) -> None:
    """
    Actualiza una colección descargando solo los archivos nuevos en kemono.

    Reconstruye la URL desde site + artist_id y corre el flujo de descarga normal.
    El hash-dedup automáticamente salta los archivos ya indexados.
    """
    site      = artist_data.get("site", "")
    artist_id = artist_data.get("artist_id", "")
    url = f"https://kemono.cr/{site}/user/{artist_id}"

    await download_for_gui(
        url=url,
        config=config,
        workers=workers,
        updates=updates,
        session_id=session_id,
        ext_filter=ext_filter,
        exclude_mode=exclude_mode,
    )


async def _build_local_hash_map(artist_dir: Path) -> dict[str, Path]:
    """
    Retorna {sha256: path} sólo para archivos de medios que existen
    físicamente en artist_dir pero NO están registrados en catalog.db.

    Flujo rápido:
      1. Leer catalog.db para obtener {filename: hash} de archivos indexados
         → sin acceso a disco, sólo una query SQL.
      2. Filtrar archivos físicos: sólo los que no están en el catálogo
         necesitan ser hasheados.
      3. Hashear los restantes en paralelo con run_in_executor.

    Para colecciones establecidas (todo catalogado) esto es casi instantáneo.
    Para importaciones iniciales (carpeta sin catalog.db) hashea en paralelo.

    También limpia archivos .tmp huérfanos de sesiones interrumpidas.
    """
    from ..catalog import get_all_files
    from ..hasher import sha256_file

    # Eliminar .tmp huérfanos de sesiones anteriores
    try:
        for tmp in artist_dir.iterdir():
            if tmp.is_file() and tmp.name.endswith(".tmp"):
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except OSError:
        return {}

    # Paso 1: hashes ya conocidos por el catálogo (sin leer disco)
    try:
        cataloged: dict[str, str] = {
            e["filename"]: e["hash"]
            for e in await get_all_files(artist_dir)
        }
    except Exception:
        cataloged = {}

    # Paso 2: archivos físicos que NO están en el catálogo
    try:
        physical = [
            f for f in artist_dir.iterdir()
            if f.is_file() and f.suffix.lower() in _MEDIA_EXTENSIONS
        ]
    except OSError:
        return {}

    uncataloged = [f for f in physical if f.name not in cataloged]
    if not uncataloged:
        return {}

    # Paso 3: hashear en paralelo sólo los no catalogados
    loop = asyncio.get_event_loop()

    async def _hash_one(f: Path) -> tuple[str | None, Path]:
        try:
            h = await loop.run_in_executor(None, sha256_file, f)
            return h, f
        except Exception:
            return None, f

    pairs = await asyncio.gather(*(_hash_one(f) for f in uncataloged))
    return {h: f for h, f in pairs if h is not None}


async def load_collections_async(config) -> list[dict]:
    """Carga la lista de colecciones desde index.db con sus estadísticas."""
    from ..config import INDEX_DB
    from ..index import init_index, list_all
    from ..catalog import get_stats

    await init_index(INDEX_DB)
    artists = await list_all(INDEX_DB)

    results = []
    for a in artists:
        folder = Path(a["folder_path"])
        stats = await get_stats(folder) if folder.exists() else {"total": 0, "total_size": 0}
        results.append({**a, **stats})
    return results
