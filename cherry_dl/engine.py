"""
Engine de descargas paralelas.

Implementa un pool de workers async donde cada slot libre inmediatamente
toma la siguiente tarea de la cola, sin esperar a que los otros terminen.

Manejo de DDoS-Guard (DDG):
  - Header obligatorio:  Accept: text/css  (documentado por el creador de kemono)
  - Cookies DDG persistidas entre sesiones (~/.cherry-dl/session.json)
  - Retry con backoff exponencial via tenacity
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import AsyncIterator, Callable

import httpx
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import UserConfig, load_session, save_session
from .hasher import sha256_bytes


# ── Helpers de I/O en thread ───────────────────────────────────────────────────

def _finalize_download(
    chunks: list[bytes],
    tmp_file: Path,
    dest_file: Path,
) -> str:
    """
    Une los chunks, calcula el hash SHA-256, escribe el archivo .tmp y lo
    renombra atómicamente al destino final.

    Se ejecuta en un thread executor para no bloquear el event loop de asyncio:
    tanto b"".join() como sha256_bytes() y write_bytes() son operaciones
    CPU/I/O síncronas que pueden tardar varios segundos en archivos grandes.

    Retorna el hash SHA-256 del archivo.
    """
    data = b"".join(chunks)
    file_hash = sha256_bytes(data)
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file.write_bytes(data)
    try:
        tmp_file.rename(dest_file)
    except OSError:
        tmp_file.unlink(missing_ok=True)
        raise
    return file_hash


# ── Clasificación de errores ───────────────────────────────────────────────────

class ErrorKind:
    """Categorías de error para decidir la estrategia de reintento."""
    NOT_FOUND    = "not_found"    # 404 — el archivo no existe; saltar
    AUTH         = "auth"         # 401 — sin credenciales; saltar
    CLOUDFLARE   = "cloudflare"   # CF challenge/ban; esperar y reintentar
    RATE_LIMIT   = "rate_limit"   # 429 — demasiadas peticiones; esperar y reintentar
    SERVER       = "server_error" # 5xx — error del servidor; reintentar
    NETWORK      = "network"      # error de conexión; reintentar
    TIMEOUT      = "timeout"      # tiempo de espera; reintentar
    STALL        = "stall"        # descarga iniciada pero sin datos por N s; diferir
    UNKNOWN      = "unknown"      # otro error no clasificado

    # Tipos que deben ir a la cola diferida en lugar de descartarse
    DEFERRABLE = {"stall", "timeout", "network", "server_error", "cloudflare",
                  "rate_limit"}


# ── Constantes ─────────────────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Header documentado por el creador de kemono para bypass de DDG
_DDG_HEADERS = {
    "Accept": "text/css",
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}

# Nombre de las cookies DDG a persistir
_DDG_COOKIES = {"__ddg1_", "__ddg8_", "__ddg9_", "__ddg10_"}


# ── Resultado de descarga ──────────────────────────────────────────────────────

class DownloadResult:
    __slots__ = ("url", "filename", "dest", "file_hash", "file_size", "skipped",
                 "error", "error_kind")

    def __init__(
        self,
        url: str,
        filename: str,
        dest: Path | None = None,
        file_hash: str | None = None,
        file_size: int = 0,
        skipped: bool = False,
        error: str | None = None,
        error_kind: str | None = None,
    ) -> None:
        self.url        = url
        self.filename   = filename
        self.dest       = dest
        self.file_hash  = file_hash
        self.file_size  = file_size
        self.skipped    = skipped
        self.error      = error
        self.error_kind = error_kind  # uno de ErrorKind.*

    @property
    def ok(self) -> bool:
        return self.error is None


# ── Engine ─────────────────────────────────────────────────────────────────────

class DownloadEngine:
    """
    Gestiona un pool de workers async y un cliente HTTP compartido.

    Uso como context manager:
        async with DownloadEngine(config) as engine:
            result = await engine.download(url, dest)
    """

    def __init__(self, config: UserConfig, workers: int | None = None) -> None:
        self.config = config
        self._workers = workers or config.workers
        self._semaphore = asyncio.Semaphore(self._workers)
        self._client: httpx.AsyncClient | None = None
        # Solo cookies planas (strings) — los bloques de servicios como
        # {"pixiv": {...}} son dicts anidados y no son cookies HTTP.
        raw = load_session()
        self._session_cookies: dict[str, str] = {
            k: v for k, v in raw.items() if isinstance(v, str)
        }

    async def __aenter__(self) -> "DownloadEngine":
        # Timeout de conexión y escritura = config.timeout (corto, 30 s).
        # Timeout de lectura = stall_timeout (largo, 120 s por defecto).
        # httpx aplica el read_timeout POR CHUNK: si el servidor no envía
        # ningún byte en stall_timeout segundos, lanza ReadTimeout, que es
        # subclase de TimeoutException y se reintenta con backoff.
        # Esto reemplaza cualquier mecanismo de watchdog manual.
        self._client = httpx.AsyncClient(
            headers=_DDG_HEADERS,
            cookies=self._session_cookies,
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(
                connect=self.config.timeout,
                read=self.config.network.stall_timeout,
                write=self.config.timeout,
                pool=self.config.timeout,
            ),
        )
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            # Persistir cookies DDG actualizadas antes de cerrar
            self._persist_ddg_cookies()
            await self._client.aclose()

    def _persist_ddg_cookies(self) -> None:
        """Guarda las cookies DDG del cliente en disco para la próxima sesión."""
        if self._client is None:
            return
        # Iterar el jar directamente para evitar CookieConflict de httpx
        # cuando existen múltiples cookies con el mismo nombre (distintos paths)
        updated = {}
        for cookie in self._client.cookies.jar:
            if cookie.name in _DDG_COOKIES:
                updated[cookie.name] = cookie.value
        if updated:
            self._session_cookies.update(updated)
            save_session(self._session_cookies)

    # ── API pública ──────────────────────────────────────────────────────────

    async def get_json(self, url: str, retries: int | None = None) -> dict | list:
        """
        GET con retry. Retorna el JSON parseado.
        Respeta el delay configurado entre requests.
        """
        max_attempts = retries or self.config.network.retries_api
        return await self._get_json_with_retry(url, max_attempts)

    async def download(
        self,
        url: str,
        dest_dir: Path,
        filename: str,
        progress: Progress | None = None,
        task_id: TaskID | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        extra_headers: dict | None = None,
    ) -> DownloadResult:
        """
        Descarga un archivo al directorio destino.
        Respeta el semáforo del pool (max N descargas simultáneas).
        Retorna DownloadResult con hash SHA-256 y tamaño.

        on_progress:    callback(bytes_done, total_bytes) por chunk.
        extra_headers:  headers adicionales por archivo (ej. Referer de Pixiv).
                        Se fusionan con los headers base del cliente.
        """
        async with self._semaphore:
            return await self._do_download(
                url, dest_dir, filename, progress, task_id,
                on_progress, extra_headers,
            )

    async def run_queue(
        self,
        tasks: AsyncIterator[tuple[str, Path, str]],
        progress: Progress,
    ) -> list[DownloadResult]:
        """
        Procesa una cola de tareas (url, dest_dir, filename) con el pool.
        Cada worker libre toma la siguiente tarea inmediatamente.
        Retorna lista de DownloadResult al terminar.
        """
        results: list[DownloadResult] = []
        pending: set[asyncio.Task] = set()

        async for url, dest_dir, filename in tasks:
            task_id = progress.add_task(f"[cyan]{filename[:40]}", total=None)
            coro = self.download(url, dest_dir, filename, progress, task_id)
            t = asyncio.create_task(coro)
            pending.add(t)
            t.add_done_callback(pending.discard)

            # Limitar tareas en vuelo al doble del pool para no saturar RAM
            while len(pending) >= self._workers * 2:
                done, pending_set = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    results.append(finished.result())
                pending = set(pending_set)

        # Esperar las restantes
        if pending:
            done, _ = await asyncio.wait(pending)
            for finished in done:
                results.append(finished.result())

        return results

    # ── Internals ────────────────────────────────────────────────────────────

    async def _get_json_with_retry(self, url: str, max_attempts: int) -> dict | list:
        """
        GET con backoff exponencial para llamadas a API.

        Estrategias por código:
          404      → error permanente; lanza inmediatamente (el recurso no existe)
          403 CF   → Cloudflare; espera 60 s + 30 s/intento, luego retry
          429      → rate limit; espera 35 s + 10 s/intento, luego retry
          5xx      → error servidor; backoff exponencial, luego retry
          red/tout → backoff exponencial, luego retry
        """
        last_error: Exception | None = None

        for attempt in range(max_attempts):
            try:
                await self._delay()
                resp = await self._client.get(url)
                code = resp.status_code

                # 404 — recurso permanentemente ausente
                if code == 404:
                    raise httpx.HTTPStatusError(
                        f"404 Not Found: {url}", request=resp.request, response=resp
                    )

                # 403 Cloudflare — temporal, hay que esperar
                if code == 403 and _is_cloudflare(resp):
                    wait = 60 + attempt * 30
                    last_error = RuntimeError(f"Cloudflare 403 en {url}")
                    await asyncio.sleep(wait)
                    continue

                # 403 sin CF — acceso denegado permanente
                if code == 403:
                    raise httpx.HTTPStatusError(
                        f"403 Forbidden: {url}", request=resp.request, response=resp
                    )

                # 429 — rate limit
                if code == 429:
                    wait = 35 + attempt * 10
                    last_error = RuntimeError(f"Rate limit 429 en {url}")
                    await asyncio.sleep(wait)
                    continue

                # 5xx — error de servidor, reintentar
                if code >= 500:
                    backoff = 2 ** attempt + random.uniform(0, 1)
                    last_error = RuntimeError(f"HTTP {code} en {url}")
                    await asyncio.sleep(backoff)
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise   # 404 / 403 permanente — propagar sin retry

            except (httpx.RequestError, httpx.TimeoutException) as e:
                last_error = e
                backoff = 2 ** attempt + random.uniform(0, 1)
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"Falló tras {max_attempts} intentos ({last_error}): {url}"
        )

    async def _do_download(
        self,
        url: str,
        dest_dir: Path,
        filename: str,
        progress: Progress | None,
        task_id: TaskID | None,
        on_progress: Callable[[int, int], None] | None = None,
        extra_headers: dict | None = None,
    ) -> DownloadResult:
        """
        Descarga un archivo con retry clasificado por tipo de error.

        Estrategias:
          404 / 401  → retorno inmediato, sin retry
          403 CF     → esperar 60 s + 30 s por intento, luego retry
          429        → esperar 35 s + 10 s por intento, luego retry
          5xx        → backoff exponencial, luego retry
          red/timeout → backoff exponencial, luego retry
        """
        max_attempts = self.config.network.retries_file
        last_error: str = ""
        last_kind:  str = ErrorKind.UNKNOWN

        for attempt in range(max_attempts):
            try:
                await self._delay()
                chunks: list[bytes] = []
                total = 0

                async with self._client.stream(
                    "GET", url, headers=extra_headers or {}
                ) as resp:
                    code = resp.status_code

                    # ── Errores permanentes: no reintentar ─────────────────
                    if code == 404:
                        return DownloadResult(
                            url=url, filename=filename,
                            error="Archivo no encontrado en el servidor (404)",
                            error_kind=ErrorKind.NOT_FOUND,
                        )
                    if code == 401:
                        return DownloadResult(
                            url=url, filename=filename,
                            error="No autorizado — el archivo requiere sesión (401)",
                            error_kind=ErrorKind.AUTH,
                        )

                    # ── Cloudflare: 403 con cabeceras CF ──────────────────
                    if code == 403 and _is_cloudflare(resp):
                        wait = 60 + attempt * 30
                        last_error = f"Cloudflare bloqueó la descarga (403 CF) — esperando {wait}s"
                        last_kind  = ErrorKind.CLOUDFLARE
                        await asyncio.sleep(wait)
                        continue

                    # ── Rate limit ─────────────────────────────────────────
                    if code == 429:
                        wait = 35 + attempt * 10
                        last_error = f"Demasiadas peticiones (429) — esperando {wait}s"
                        last_kind  = ErrorKind.RATE_LIMIT
                        await asyncio.sleep(wait)
                        continue

                    # ── Error de servidor ──────────────────────────────────
                    if code >= 500:
                        backoff = 2 ** attempt + random.uniform(0, 1)
                        last_error = f"Error del servidor ({code})"
                        last_kind  = ErrorKind.SERVER
                        await asyncio.sleep(backoff)
                        continue

                    # ── Otros códigos HTTP no exitosos ─────────────────────
                    if code >= 400:
                        return DownloadResult(
                            url=url, filename=filename,
                            error=f"HTTP {code} — no se puede descargar",
                            error_kind=ErrorKind.UNKNOWN,
                        )

                    # ── Descarga exitosa ───────────────────────────────────
                    content_length = int(resp.headers.get("content-length", 0))
                    if progress and task_id is not None:
                        progress.update(task_id, total=content_length or None)

                    # El stall se detecta automáticamente vía httpx:
                    # read_timeout = stall_timeout (configurado en __aenter__).
                    # Si el servidor no envía ningún byte durante stall_timeout
                    # segundos, httpx lanza ReadTimeout → reintento con backoff.
                    # No se usan Tasks separadas para evitar corrupción del
                    # estado HTTP/2 al cancelar mid-stream.
                    _total_bytes = content_length
                    total = 0

                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        chunks.append(chunk)
                        total += len(chunk)
                        if progress and task_id is not None:
                            progress.update(task_id, advance=len(chunk))
                        if on_progress:
                            on_progress(total, _total_bytes)

                dest_file = dest_dir / filename
                tmp_file  = dest_dir / (filename + ".tmp")

                # Mover join+hash+write+rename a thread para no bloquear
                # el event loop durante operaciones CPU/I/O pesadas.
                file_hash = await asyncio.to_thread(
                    _finalize_download, chunks, tmp_file, dest_file
                )

                if progress and task_id is not None:
                    progress.update(task_id, description=f"[green]{filename[:40]}")

                return DownloadResult(
                    url=url, filename=filename,
                    dest=dest_file, file_hash=file_hash, file_size=total,
                )

            except httpx.TimeoutException as e:
                backoff = 2 ** attempt + random.uniform(0, 1)
                last_error = f"Timeout tras {int(backoff)}s de espera"
                last_kind  = ErrorKind.TIMEOUT
                await asyncio.sleep(backoff)

            except httpx.RequestError as e:
                backoff = 2 ** attempt + random.uniform(0, 1)
                last_error = f"Error de conexión: {type(e).__name__}"
                last_kind  = ErrorKind.NETWORK
                await asyncio.sleep(backoff)

        return DownloadResult(
            url=url, filename=filename,
            error=f"{last_error} (tras {max_attempts} intentos)",
            error_kind=last_kind,
        )

    async def _delay(self) -> None:
        """Espera aleatoria entre delay_min y delay_max segundos."""
        d = random.uniform(
            self.config.network.delay_min,
            self.config.network.delay_max,
        )
        await asyncio.sleep(d)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_cloudflare(resp: httpx.Response) -> bool:
    """Detecta respuestas bloqueadas por Cloudflare."""
    # CF siempre incluye el header cf-ray o server: cloudflare
    headers = resp.headers
    if "cf-ray" in headers or headers.get("server", "").lower() == "cloudflare":
        return True
    # Algunos challenge pages devuelven 403 sin cf-ray pero con cf-mitigated
    if "cf-mitigated" in headers:
        return True
    return False


# ── Progress bar factory ───────────────────────────────────────────────────────

def make_progress() -> Progress:
    """Crea una barra de progreso Rich para el engine."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        expand=True,
    )
