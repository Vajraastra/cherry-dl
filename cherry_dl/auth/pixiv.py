"""
Autenticación con Pixiv via cookies del navegador del sistema.

Flujo en ensure_pixiv_session():
  1. session.json["pixiv"] válido (<30 días)  →  usar directamente
  2. browser_cookie3: leer cookies de Firefox/Chrome/Brave/Edge
     →  si encuentra PHPSESSID: guardar en session.json y continuar
  3. raise NeedsPixivAuth  →  TUI muestra PixivAuthModal

PixivAuthModal (en tui/app.py):
  - Botón "Abrir pixiv.net/login" → webbrowser.open() (stdlib)
  - Botón "Ya inicié sesión"      → reintenta browser_cookie3
  - Al encontrar PHPSESSID: guarda en session.json y continúa

Firefox se intenta antes que Chrome en Linux porque no requiere keyring.
Chrome/Brave/Edge requieren GNOME Keyring o KWallet desbloqueados
(siempre disponibles en sesiones de escritorio activas).

TTL de sesión guardada: 30 días. Al recibir 401/403 se llama
clear_pixiv_session() y el flujo se reinicia desde el paso 2.

La API que usa el template es la web AJAX API de pixiv.net:
  GET /ajax/user/{id}                    → info del artista
  GET /ajax/user/{id}/profile/all        → todos los IDs de obras
  GET /ajax/user/{id}/illusts?ids[]=...  → detalles por lote
  GET /ajax/illust/{id}/pages            → páginas de obra multi-pág
  GET /ajax/illust/{id}/ugoira_meta      → ZIP de animación

Estas rutas son las mismas que usa gallery-dl y son estables desde 2018.
"""

from __future__ import annotations

import time

from ..config import load_session, save_session

# ── Constantes ────────────────────────────────────────────────────────────────

_SESSION_TTL = 30 * 24 * 3600   # 30 días en segundos
_SESSION_KEY = "pixiv"

# Cookies relevantes de pixiv.net que se persisten
COOKIES_TO_KEEP = frozenset({
    "PHPSESSID",
    "device_token",
    "p_ab_id",
    "p_ab_id_2",
    "p_ab_d_id",
    "privacy_policy_agreement",
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

class NeedsPixivAuth(Exception):
    """
    No se encontró sesión activa de Pixiv en el navegador.
    La TUI debe mostrar PixivAuthModal para que el usuario inicie sesión.
    """


# ── API pública ───────────────────────────────────────────────────────────────

def load_pixiv_cookies() -> dict[str, str] | None:
    """
    Carga cookies de Pixiv guardadas en session.json.
    Retorna None si no existen, faltan datos esenciales, o han expirado.
    """
    data  = load_session()
    block = data.get(_SESSION_KEY)
    if not isinstance(block, dict):
        return None

    if time.time() - block.get("_saved_at", 0) > _SESSION_TTL:
        return None

    cookies = {k: v for k, v in block.items() if not k.startswith("_")}
    return cookies if cookies.get("PHPSESSID") else None


def save_pixiv_cookies(cookies: dict[str, str]) -> None:
    """Guarda cookies de Pixiv en session.json con timestamp actual."""
    data  = load_session()
    block = dict(cookies)
    block["_saved_at"] = int(time.time())
    data[_SESSION_KEY] = block
    save_session(data)


def clear_pixiv_session() -> None:
    """Elimina las cookies guardadas (fuerza re-autenticación)."""
    data = load_session()
    data.pop(_SESSION_KEY, None)
    save_session(data)


async def ensure_pixiv_session() -> dict[str, str]:
    """
    Garantiza una sesión válida de Pixiv.

    Pasos:
      1. session.json válido  →  retorna cookies guardadas
      2. browser_cookie3      →  lee del browser, guarda y retorna
      3. raise NeedsPixivAuth →  la TUI muestra PixivAuthModal
    """
    existing = load_pixiv_cookies()
    if existing:
        return existing

    from_browser = load_from_browser()
    if from_browser:
        save_pixiv_cookies(from_browser)
        return from_browser

    raise NeedsPixivAuth(
        "No se encontró sesión activa de Pixiv en el navegador."
    )


# ── Lectura de cookies del browser ────────────────────────────────────────────

def load_from_browser() -> dict[str, str] | None:
    """
    Lee cookies de Pixiv del browser instalado en el sistema.

    Intenta cada browser en _BROWSER_LOADERS en orden. Retorna el
    primer dict que contenga PHPSESSID, o None si ninguno lo tiene.

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
            jar = loader(domain_name="pixiv.net")
            cookies = {
                c.name: c.value
                for c in jar
                if c.name in COOKIES_TO_KEEP
            }
            if cookies.get("PHPSESSID"):
                return cookies
        except Exception:
            continue

    return None
