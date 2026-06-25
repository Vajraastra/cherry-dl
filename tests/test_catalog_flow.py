"""
Tests funcionales del backend crítico: catalog + pending_queue + dedup + meta.

No tocan la red ni el índice global: cada test usa un catalog.db temporal.
Formato síncrono (asyncio.run) para no depender de pytest-asyncio.
"""
import asyncio
import tempfile
from pathlib import Path

from cherry_dl import catalog as C


def _run(coro):
    return asyncio.run(coro)


def test_next_counter_monotonico():
    async def body():
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            await C.init_catalog(folder)
            c1 = await C.next_counter(folder)
            c2 = await C.next_counter(folder)
            assert c2 == c1 + 1
    _run(body())


def test_add_file_y_dedup():
    async def body():
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            await C.init_catalog(folder)
            await C.add_file(folder, file_hash="HASH_A", filename="a.jpg",
                             url_source="src://a", file_size=10, counter=1)
            assert await C.url_exists(folder, "src://a")
            assert not await C.url_exists(folder, "src://z")
            assert await C.hash_exists(folder, "HASH_A")
            assert not await C.hash_exists(folder, "HASH_Z")
    _run(body())


def test_pending_queue_count_y_filtro():
    async def body():
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            await C.init_catalog(folder)
            await C.add_pending(folder, "src://b", "http://x/b.zip", "b.zip", profile_url_id=1)
            await C.add_pending(folder, "src://c", "http://x/c.jpg", "c.jpg", profile_url_id=1)
            await C.add_pending(folder, "src://d", "http://x/d.jpg", "d.jpg", profile_url_id=2)
            assert await C.pending_count(folder) == 3
            assert await C.pending_count(folder, 1) == 2
            assert await C.pending_count(folder, ext_filter={".jpg"}) == 2
            assert await C.pending_count(folder, ext_filter={".zip"}) == 1
    _run(body())


def test_add_pending_idempotente_y_remove():
    async def body():
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            await C.init_catalog(folder)
            await C.add_pending(folder, "src://b", "http://x/b.zip", "b.zip", profile_url_id=1)
            await C.add_pending(folder, "src://b", "http://x/b.zip", "b.zip", profile_url_id=1)
            assert await C.pending_count(folder) == 1  # INSERT OR IGNORE
            await C.remove_pending(folder, "src://b")
            assert await C.pending_count(folder) == 0
    _run(body())


def test_clean_pending_catalog_overlap():
    async def body():
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            await C.init_catalog(folder)
            await C.add_pending(folder, "src://c", "http://x/c.jpg", "c.jpg", profile_url_id=1)
            # Se cataloga el mismo url_source -> debe limpiarse de la cola.
            await C.add_file(folder, file_hash="HASH_C", filename="2_c.jpg",
                             url_source="src://c", file_size=5, counter=2)
            cleaned = await C.clean_pending_catalog_overlap(folder, 1)
            assert cleaned >= 1
            assert not await C.pending_url_exists(folder, "src://c")
    _run(body())


def test_meta_int_roundtrip():
    async def body():
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            await C.init_catalog(folder)
            await C.set_meta_int(folder, "k", 42)
            assert await C.get_meta_int(folder, "k") == 42
            assert await C.get_meta_int(folder, "noexiste") is None
    _run(body())
