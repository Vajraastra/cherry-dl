"""
Login guiado por el navegador real del usuario (vía CDP / nodriver).

Por qué: en Windows el cifrado App-Bound de Brave/Chrome impide leer las cookies
del navegador con browser_cookie3 (requiere admin). Y los logins con Google
bloquean los navegadores embebidos/automatizados. La solución que esquiva ambos
problemas es abrir el navegador REAL del usuario (Brave/Edge/Chrome) con un perfil
dedicado, dejar que se loguee normalmente, y leer la cookie por el protocolo de
depuración (CDP) — sin descifrar archivos ni descargar un navegador.

Flujo no técnico: el usuario hace clic, se loguea, listo.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from ..config import CHERRY_DIR

# Perfil dedicado y persistente → el login sobrevive entre sesiones.
_PROFILE_DIR = CHERRY_DIR / "browser-profile"


# ── Detección del navegador instalado (cross-OS) ───────────────────────────────

def _windows_candidates() -> list[tuple[str, str]]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    out = []
    for name, sub in [
        ("brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("edge",  r"Microsoft\Edge\Application\msedge.exe"),
        ("chrome", r"Google\Chrome\Application\chrome.exe"),
    ]:
        for root in (pf, pfx86, local):
            if root:
                out.append((name, str(Path(root) / sub)))
    return out


def find_browser() -> tuple[str, str] | None:
    """Devuelve (nombre, ruta_ejecutable) del primer navegador Chromium hallado."""
    import sys

    if sys.platform == "win32":
        for name, path in _windows_candidates():
            if Path(path).is_file():
                return name, path
        return None

    if sys.platform == "darwin":
        mac = [
            ("brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            ("edge",  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for name, path in mac:
            if Path(path).is_file():
                return name, path
        return None

    # Linux / otros: buscar en PATH
    for name, exe in [
        ("brave", "brave-browser"), ("brave", "brave"),
        ("chrome", "google-chrome"), ("chrome", "google-chrome-stable"),
        ("chromium", "chromium"), ("chromium", "chromium-browser"),
        ("edge", "microsoft-edge"),
    ]:
        found = shutil.which(exe)
        if found:
            return name, found
    return None


# ── Captura de cookies por CDP ─────────────────────────────────────────────────

async def capture_cookies(
    login_url: str,
    domain: str,
    wanted: set[str],
    *,
    require: str,
    timeout: int = 240,
    on_status=None,
) -> dict[str, str] | None:
    """
    Abre el navegador real en `login_url`, espera a que el usuario se loguee y
    devuelve las cookies de `wanted` presentes en `domain`. Termina en cuanto
    aparece la cookie `require` (o None si vence `timeout`).

    `on_status(msg)` opcional: callback para reportar progreso a la UI/CLI.
    """
    import nodriver as uc

    def _say(msg: str) -> None:
        if on_status:
            on_status(msg)

    info = find_browser()
    if info is None:
        _say("No se encontró Brave/Edge/Chrome instalado.")
        return None
    browser_name, exe = info
    _say(f"Abriendo {browser_name}…")

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    browser = await uc.start(
        browser_executable_path=exe,
        user_data_dir=str(_PROFILE_DIR),
        headless=False,
    )
    try:
        await browser.get(login_url)
        _say("Iniciá sesión en la ventana del navegador…")
        for i in range(timeout // 3):
            await asyncio.sleep(3)
            try:
                cookies = await browser.cookies.get_all()
            except Exception:
                continue
            found = {}
            for c in cookies:
                name = getattr(c, "name", None)
                dom = getattr(c, "domain", "") or ""
                if name in wanted and domain in dom:
                    found[name] = getattr(c, "value", "")
            if found.get(require):
                _say("Sesión capturada.")
                return found
        _say("Tiempo agotado sin detectar la sesión.")
        return None
    finally:
        try:
            browser.stop()
        except Exception:
            pass
