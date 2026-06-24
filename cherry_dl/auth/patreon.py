"""
Autenticación con Patreon via cookies del navegador del sistema.

Flujo en ensure_patreon_session():
  1. session.json["patreon"] válido  →  usar directamente
  2. browser_cookie3: leer cookies de Firefox/Chrome/Brave/Edge
     →  si encuentra session_id: guardar en session.json y continuar
  3. login guiado (navegador real + CDP)  →  captura session_id y continúa
  4. raise NeedsManualAuth  →  sin navegador; TUI muestra PatreonAuthModal

Firefox se intenta antes que Chrome en Linux porque no requiere keyring.
Chrome/Brave/Edge requieren GNOME Keyring o KWallet desbloqueados
(siempre disponibles en sesiones de escritorio activas).

Sin caducidad por tiempo: la sesión guardada vale hasta que Patreon la
rechace (401). Ahí el template llama clear_patreon_session() y el siguiente
uso reinicia el flujo desde el paso 2 (re-login automático).
"""

from __future__ import annotations

import time

from ..config import load_session, save_session

# ── Constantes ────────────────────────────────────────────────────────────────

_SESSION_KEY = "patreon"

# Cookies relevantes de patreon.com que se persisten
COOKIES_TO_KEEP = frozenset({
    "session_id",
    "patreon_device_id",
    "__cf_bm",
    "__cfruid",
})

# Orden de browsers a intentar. Firefox primero: sin keyring en Linux.
_BROWSER_LOADERS = [
    "firefox",
    "chrome",
    "chromium",
    "brave",
    "edge",
]


# ── Excepción ─────────────────────────────────────────────────────────────────

class NeedsManualAuth(Exception):
    """
    No se encontró sesión activa de Patreon en el navegador.
    La TUI debe mostrar PatreonAuthModal para que el usuario inicie sesión.
    """


# ── API pública ───────────────────────────────────────────────────────────────

def load_patreon_cookies() -> dict[str, str] | None:
    """
    Carga cookies de Patreon guardadas en session.json.

    Sin caducidad por tiempo: la sesión vale hasta que Patreon la rechace
    (401/403), momento en que el template llama clear_patreon_session() y el
    siguiente uso dispara el login guiado automáticamente. Retorna None si no
    existen o falta el session_id.
    """
    data = load_session()
    block = data.get(_SESSION_KEY)
    if not isinstance(block, dict):
        return None

    cookies = {k: v for k, v in block.items() if not k.startswith("_")}
    return cookies if cookies.get("session_id") else None


def save_patreon_cookies(cookies: dict[str, str]) -> None:
    """Guarda cookies de Patreon en session.json con timestamp actual."""
    data = load_session()
    block = dict(cookies)
    block["_saved_at"] = int(time.time())
    data[_SESSION_KEY] = block
    save_session(data)


def clear_patreon_session() -> None:
    """Elimina las cookies guardadas (fuerza re-autenticación)."""
    data = load_session()
    data.pop(_SESSION_KEY, None)
    save_session(data)


def refresh_patreon_cookies(new_cookies: dict[str, str]) -> None:
    """
    Actualiza cookies de corta duración (__cf_bm, __cfruid) sin resetear
    session_id ni patreon_device_id. Llamar tras cada request a la API.
    """
    existing = load_patreon_cookies() or {}
    save_patreon_cookies({**existing, **new_cookies})


async def ensure_patreon_session(
    *, allow_guided: bool = True, on_status=None
) -> dict[str, str]:
    """
    Garantiza una sesión válida de Patreon (autenticación perezosa).

    Pasos:
      1. session.json válido         →  retorna cookies guardadas
      2. browser_cookie3             →  lee del browser (funciona en Linux; en
                                         Windows el cifrado App-Bound lo bloquea)
      3. login guiado (allow_guided) →  abre el navegador real y captura por CDP
      4. raise NeedsManualAuth       →  sin navegador o login cancelado/fallido

    El paso 3 hace que el login sea automático: al sincronizar un perfil de
    Patreon sin sesión, la app abre el navegador sola — no hace falta correr
    `patreon-login` por adelantado. `allow_guided=False` lo desactiva (p. ej.
    para contextos no interactivos que prefieran manejar NeedsManualAuth ellos
    mismos).
    """
    existing = load_patreon_cookies()
    if existing:
        return existing

    from_browser = load_from_browser()
    if from_browser:
        save_patreon_cookies(from_browser)
        return from_browser

    if allow_guided:
        from .browser_login import find_browser

        if find_browser() is not None:
            cookies = await guided_login_patreon(on_status=on_status)
            if cookies and cookies.get("session_id"):
                return cookies

    raise NeedsManualAuth(
        "No se encontró sesión activa de Patreon en el navegador."
    )


async def guided_login_patreon(on_status=None) -> dict[str, str] | None:
    """
    Login guiado: abre el navegador real del usuario, espera el login y captura
    la cookie session_id por CDP. Funciona en Windows (esquiva App-Bound) y con
    logins de Google (navegador real, no detectable como automatización).

    Retorna las cookies guardadas, o None si no se pudo capturar.
    """
    from .browser_login import capture_cookies

    cookies = await capture_cookies(
        login_url="https://www.patreon.com/login",
        domain="patreon.com",
        wanted=set(COOKIES_TO_KEEP),
        require="session_id",
        on_status=on_status,
    )
    if cookies and cookies.get("session_id"):
        save_patreon_cookies(cookies)
        return cookies
    return None


# ── Lectura de cookies del browser ────────────────────────────────────────────

def load_from_browser() -> dict[str, str] | None:
    """
    Lee cookies de Patreon del browser instalado en el sistema.

    Intenta cada browser en _BROWSER_LOADERS en orden. Retorna el
    primer dict que contenga session_id, o None si ninguno lo tiene.

    Errores por keyring bloqueado, browser no instalado o perfil
    corrupto se silencian — se pasa al siguiente browser.
    """
    try:
        import browser_cookie3
    except ImportError:
        return None

    loaders = {
        "firefox":  browser_cookie3.firefox,
        "chrome":   browser_cookie3.chrome,
        "chromium": browser_cookie3.chromium,
        "brave":    browser_cookie3.brave,
        "edge":     browser_cookie3.edge,
    }

    for name in _BROWSER_LOADERS:
        loader = loaders.get(name)
        if loader is None:
            continue
        try:
            jar = loader(domain_name="patreon.com")
            cookies = {
                c.name: c.value
                for c in jar
                if c.name in COOKIES_TO_KEEP
            }
            if cookies.get("session_id"):
                return cookies
        except Exception:
            continue

    return None
