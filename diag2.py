"""
Descarga completa de un archivo > 4MB usando Range requests.
Mide si el CDN trunca y si el resume funciona.
"""
import asyncio
import hashlib
import time
import httpx

BASE = "https://kemono.cr"
UA   = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DDG  = {"Accept": "text/css", "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
FILE_HEADERS = {"Accept": "*/*", "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}


def _parse_cr_total(header: str) -> int | None:
    try:
        s = header.rsplit("/", 1)[-1].strip()
        return None if not s or s == "*" else int(s)
    except (ValueError, IndexError):
        return None


async def find_large_file(client, min_mb=4):
    """Busca un archivo >= min_mb MB en la API."""
    r = await client.get(f"{BASE}/api/v1/creators", headers=DDG)
    for creator in r.json()[:100]:
        svc, cid = creator.get("service",""), str(creator.get("id",""))
        try:
            pr = await client.get(
                f"{BASE}/api/v1/{svc}/user/{cid}/posts?o=0", headers=DDG
            )
            if pr.status_code != 200: continue
            for post in pr.json() or []:
                for entry in ([post.get("file",{})] + post.get("attachments",[])):
                    if not entry or not entry.get("path"): continue
                    url = f"{BASE}{entry['path']}"
                    h = await client.head(url, timeout=8)
                    size = int(h.headers.get("content-length", 0))
                    if size >= min_mb * 1024 * 1024:
                        name = entry.get("name") or url.split("/")[-1][:40]
                        cURL = f"{BASE}/{svc}/user/{cid}"
                        print(f"  ✓ {name}  ({size/1024/1024:.1f} MB)  {url[-50:]}")
                        return url, size, cURL
        except Exception:
            continue
    return None, 0, ""


async def full_download(client, url: str, creator_url: str, declared_size: int):
    """
    Descarga el archivo completo usando Range requests en chunks.
    Simula exactamente el comportamiento de _do_download.
    """
    chunks = []
    resume_from = 0
    full_size = declared_size
    chunk_n = 0
    t_total = time.perf_counter()

    print(f"\nIniciando descarga: {url[-60:]}")
    print(f"Tamaño declarado:  {full_size/1024/1024:.2f} MB")
    print()

    while True:
        chunk_n += 1
        req = dict(FILE_HEADERS)
        req["Referer"] = creator_url
        if resume_from > 0:
            req["Range"] = f"bytes={resume_from}-"

        print(f"[Request {chunk_n}]  Range: bytes={resume_from}-  "
              f"({resume_from/1024/1024:.2f} MB recibidos hasta ahora)")

        t0 = time.perf_counter()
        received = 0

        try:
            async with client.stream("GET", url, headers=req,
                                     timeout=60.0) as resp:
                code = resp.status_code
                ct = resp.headers.get("content-type","?")
                cl = resp.headers.get("content-length","?")
                cr = resp.headers.get("content-range","")
                xs = resp.headers.get("x-cache-status","?")

                print(f"  Status: {code}  |  CT: {ct}  |  CL: {cl}")
                if cr: print(f"  Content-Range: {cr}")
                print(f"  X-Cache: {xs}")

                if code == 206:
                    t = _parse_cr_total(cr)
                    if t: full_size = t
                elif code == 200:
                    if resume_from > 0:
                        print("  ⚠ Servidor ignoró Range → reset y descarga completa")
                        chunks.clear(); resume_from = 0
                    full_size = int(resp.headers.get("content-length", full_size))
                else:
                    body = await resp.aread()
                    print(f"  ✗ Error {code}: {body[:200]}")
                    return False

                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    chunks.append(chunk)
                    received += len(chunk)

        except httpx.TimeoutException as e:
            print(f"  ✗ Timeout ({type(e).__name__})")
            return False
        except httpx.RequestError as e:
            print(f"  ✗ Error de conexión: {e}")
            return False

        elapsed = time.perf_counter() - t0
        speed = (received / 1024) / elapsed if elapsed > 0 else 0
        resume_from += received
        total = resume_from

        print(f"  Recibidos: {received/1024/1024:.3f} MB en {elapsed:.1f}s = {speed:.1f} KB/s")
        print(f"  Acumulado: {total/1024/1024:.3f} / {full_size/1024/1024:.2f} MB  "
              f"({total*100//full_size if full_size else '?'}%)")

        if received == 0 and full_size > 0 and total < full_size:
            print("  ✗ CDN devolvió 0 bytes — abortando")
            return False

        if full_size > 0 and total < full_size:
            print(f"  → Truncado al {received/1024/1024:.3f} MB — continuando con Range…\n")
            await asyncio.sleep(1.0)
            continue

        # Completo
        elapsed_total = time.perf_counter() - t_total
        data = b"".join(chunks)
        sha = hashlib.sha256(data).hexdigest()[:16]
        print(f"\n{'='*55}")
        print(f"✓ Descarga completa en {elapsed_total:.1f}s  ({chunk_n} request(s))")
        print(f"  Total: {len(data)/1024/1024:.3f} MB  |  SHA256[:16]: {sha}")
        print(f"  Velocidad promedio: {len(data)/1024/elapsed_total:.1f} KB/s")
        return True


async def main():
    async with httpx.AsyncClient(
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        http2=True,
        follow_redirects=True,
        timeout=httpx.Timeout(30),
    ) as client:
        print("Buscando archivo >= 4 MB...")
        url, size, creator = await find_large_file(client, min_mb=4)
        if not url:
            print("No se encontró archivo >= 4 MB.")
            return
        await full_download(client, url, creator, size)


if __name__ == "__main__":
    asyncio.run(main())
