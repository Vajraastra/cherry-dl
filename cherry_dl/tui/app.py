"""
cherry-dl TUI — Textual interface (POC)

Screens:
  ProfilesScreen  — lista de perfiles (pantalla principal)
  ArtistScreen    — detalle + descarga por perfil
  SettingsScreen  — configuración global
  NewProfileModal — modal creación de perfil
  AddUrlModal     — modal agregar URL a perfil
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import aiosqlite
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Rule,
    Static,
)

from ..catalog import (
    add_file, add_pending, clean_pending_catalog_overlap,
    compare_catalogs, compare_by_hash_join,
    get_all_files, get_meta_int, get_pending_files, get_stats,
    hash_exists, init_catalog, migrate_unique_files, next_counter,
    pending_count, pending_url_exists, remove_pending, set_meta_int, url_exists,
)
from ..config import INDEX_DB, load_config, save_config
from ..util import safe_dirname
from ..index import (
    add_exclusion,
    add_profile_url,
    create_profile,
    delete_profile,
    get_exclusions,
    get_profile,
    init_index,
    list_profiles,
    merge_profiles,
    reindex_from_folders,
    set_profile_url_enabled,
    sync_profile_meta,
    update_profile_ext_filter,
    update_profile_last_checked,
)


# ── Grupos de extensiones para filtro de batch ─────────────────────────────

EXT_GROUPS: dict[str, tuple[str, set[str]]] = {
    "images":  ("Imágenes",          {"jpg","jpeg","png","webp","bmp","tiff","tif","avif","jxl"}),
    "anim":    ("Animaciones",        {"gif","apng"}),
    "video":   ("Video",              {"mp4","webm","mkv","avi","mov","wmv","flv","m4v","mpg","mpeg"}),
    "audio":   ("Audio",              {"mp3","flac","ogg","wav","aac","m4a","opus","wma"}),
    "zip":     ("Comprimidos",        {"zip","rar","7z","tar","gz","bz2","xz","cbz","cbr"}),
    "docs":    ("Documentos",         {"pdf","doc","docx","txt","epub"}),
    "project": ("Archivos proyecto",  {"psd","clip","xcf","kra","procreate","sai","sai2","ai","ora","mdp"}),
}


def _encode_profile_filter(group_ids: list[str], custom: str) -> str:
    """Serializa selección de filtro de perfil a JSON para guardar en BD."""
    import json
    return json.dumps({"groups": group_ids, "custom": custom.strip()})


def _decode_profile_filter(stored: str) -> tuple[set[str], set[str]]:
    """
    Deserializa un ext_filter guardado.
    Retorna (group_ids_set, ext_filter_set).
    - Si stored empieza con '{' → formato JSON nuevo.
    - Si no → formato legacy (extensiones separadas por coma).
    """
    import json
    from ..gui.bridge import _parse_ext_filter

    if not stored:
        return set(), set()

    if stored.startswith("{"):
        try:
            data      = json.loads(stored)
            group_ids = set(data.get("groups", []))
            ext_set: set[str] = set()
            for gid in group_ids:
                if gid in EXT_GROUPS:
                    _, exts = EXT_GROUPS[gid]
                    ext_set.update("." + e for e in exts)
            ext_set.update(_parse_ext_filter(data.get("custom", "")))
            return group_ids, ext_set
        except Exception:
            pass

    # Legacy: extensiones separadas por coma
    return set(), _parse_ext_filter(stored)


# ── Portapapeles del sistema ────────────────────────────────────────────────

def _read_clipboard() -> str:
    """Lee texto del portapapeles. Devuelve '' si falla.

    Backends por OS:
      - Windows: PowerShell Get-Clipboard
      - Wayland: wl-paste
      - X11:     xclip / xsel
    """
    import subprocess
    import sys

    if sys.platform == "win32":
        candidates = (
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
        )
    else:
        candidates = (
            ["wl-paste", "--no-newline"],
            ["wl-paste"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        )

    for cmd in candidates:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                # PowerShell agrega CRLF final; rstrip cubre \n y \r.
                return r.stdout.rstrip("\r\n")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return ""


class ClipInput(Input):
    """Input con paste del portapapeles del sistema.

    Cubre dos vías:
    - on_paste : el terminal convirtió Ctrl+V en bracketed paste → usa event.text
    - action_paste: Textual disparó el action interno del Input → usa wl-paste
    - App binding ctrl+v llama _insert() directamente como fallback extra.
    """

    def _insert(self, text: str) -> None:
        if not text:
            return
        pos          = self.cursor_position
        self.value   = self.value[:pos] + text + self.value[pos:]
        # mover cursor al final del texto insertado
        self.cursor_position = pos + len(text)

    def on_paste(self, event) -> None:
        """Terminal envió bracketed paste — usar el texto del evento directamente."""
        event.prevent_default()
        event.stop()
        self._insert(event.text)

    def action_paste(self) -> None:
        """Ctrl+V procesado por Textual — leer del portapapeles del sistema."""
        self._insert(_read_clipboard())


# ── Helpers de similitud de nombres ─────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Normaliza un nombre para comparación: lowercase, solo alfanumérico."""
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _name_similarity(a: str, b: str) -> float:
    """Ratio de similitud (0–1) entre dos nombres, ignorando capitalización y símbolos."""
    from difflib import SequenceMatcher
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _url_overlap(urls_a: list[dict], urls_b: list[dict]) -> str:
    """
    Detecta si dos listas de profile_urls comparten el mismo artista
    en el mismo sitio (site + artist_id coinciden).

    Retorna una descripción del primer match encontrado, o "" si no hay.
    """
    for ua in urls_a:
        if not ua.get("artist_id") or not ua.get("site"):
            continue
        for ub in urls_b:
            if (ub.get("site") == ua["site"]
                    and ub.get("artist_id") == ua["artist_id"]):
                return f"{ua['site']}/{ua['artist_id']}"
    return ""


# ── Helpers de formato ──────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _fmt_speed(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"


# ── WorkerRow ───────────────────────────────────────────────────────────────

class WorkerRow(Container):
    """Fila de un worker en el panel de descargas."""

    def __init__(self, slot_id: int, **kwargs):
        super().__init__(**kwargs)
        self._slot_id = slot_id
        self._start_time = 0.0
        self._last_ui = 0.0   # throttle de UI a 4 Hz

    def compose(self) -> ComposeResult:
        yield Label(f"W{self._slot_id + 1}", classes="wid")
        yield Label("—", classes="wstatus", id=f"wstatus-{self._slot_id}")
        yield Label("", classes="wfile", id=f"wfile-{self._slot_id}")
        yield ProgressBar(total=100, show_eta=False, show_percentage=False,
                          classes="wprog", id=f"wprog-{self._slot_id}")
        yield Label("", classes="wspeed", id=f"wspeed-{self._slot_id}")

    def start(self, filename: str) -> None:
        self._start_time = time.monotonic()
        self._last_ui = 0.0
        self.query_one(f"#wstatus-{self._slot_id}", Label).update("↓")
        self.query_one(f"#wfile-{self._slot_id}", Label).update(filename[:40])
        bar = self.query_one(f"#wprog-{self._slot_id}", ProgressBar)
        bar.update(total=100, progress=0)
        self.query_one(f"#wspeed-{self._slot_id}", Label).update("")

    def progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._last_ui < 0.25:   # throttle 4 Hz
            return
        self._last_ui = now
        elapsed = now - self._start_time
        bar = self.query_one(f"#wprog-{self._slot_id}", ProgressBar)
        if total > 0:
            bar.update(total=100, progress=int(done * 100 / total))
        else:
            bar.update(total=None)   # indeterminado
        if elapsed > 0.1:
            speed = done / elapsed
            self.query_one(f"#wspeed-{self._slot_id}", Label).update(_fmt_speed(speed))

    def done(self, filename: str, icon: str = "✓") -> None:
        self.query_one(f"#wstatus-{self._slot_id}", Label).update(icon)
        self.query_one(f"#wfile-{self._slot_id}", Label).update(filename[:40])
        bar = self.query_one(f"#wprog-{self._slot_id}", ProgressBar)
        bar.update(total=100, progress=100)
        self.query_one(f"#wspeed-{self._slot_id}", Label).update("")

    def idle(self) -> None:
        self.query_one(f"#wstatus-{self._slot_id}", Label).update("—")
        self.query_one(f"#wfile-{self._slot_id}", Label).update("")
        bar = self.query_one(f"#wprog-{self._slot_id}", ProgressBar)
        bar.update(total=100, progress=0)
        self.query_one(f"#wspeed-{self._slot_id}", Label).update("")


# ── Menú contextual de Input ────────────────────────────────────────────────

class InputContextMenu(ModalScreen[str | None]):
    """Menú contextual para campos de texto (clic derecho)."""

    BINDINGS = [("escape", "dismiss(None)", "Cerrar")]

    def compose(self) -> ComposeResult:
        with Container(id="ctx-menu"):
            yield Button("📋  Pegar",            id="ctx-paste")
            yield Button("☰   Seleccionar todo", id="ctx-select-all")
            yield Button("✕   Limpiar campo",    id="ctx-clear")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id)


# ── Modal: nueva URL ────────────────────────────────────────────────────────

class AddUrlModal(ModalScreen[str | None]):
    """Modal para agregar una URL a un perfil."""

    BINDINGS = [("escape", "dismiss(None)", "Cancelar")]

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Label("Agregar URL de fuente", classes="cherry-accent")
            yield Rule()
            yield Label("URL del artista:")
            yield Input(placeholder="https://kemono.cr/patreon/user/...", id="url-input")
            yield Label("", id="url-status")
            with Horizontal(id="modal-buttons"):
                yield Button("Cancelar", variant="default", id="btn-cancel")
                yield Button("Agregar", variant="primary", id="btn-confirm", classes="-primary")

    def on_input_changed(self, event: Input.Changed) -> None:
        from ..templates._registry import find_template
        url = event.value.strip()
        lbl = self.query_one("#url-status", Label)
        if not url:
            lbl.update("")
            return
        cls = find_template(url)
        if cls:
            if cls.provides_file_hashes:
                lbl.update(f"[green]✓ Template: {cls.name}[/]")
            else:
                lbl.update(
                    f"[green]✓ Template: {cls.name}[/]  "
                    f"[yellow]⚠ Este sitio no expone hashes — el primer scan "
                    f"descargará todo para deduplicar por hash local.[/]"
                )
        else:
            lbl.update("[red]✗ No hay template para este sitio[/]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            from ..templates._registry import find_template
            url = self.query_one("#url-input", Input).value.strip()
            if not url:
                return
            if not find_template(url):
                self.query_one("#url-status", Label).update(
                    "[red]✗ No hay template para este sitio — URL no agregada[/]"
                )
                return
            self.dismiss(url)
        else:
            self.dismiss(None)


# ── Modal: nuevo perfil ─────────────────────────────────────────────────────

class NewProfileModal(ModalScreen[dict | None]):
    """Modal wizard para crear un nuevo perfil de artista."""

    BINDINGS = [("escape", "dismiss(None)", "Cancelar")]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._artist_info = None   # cache del último get_artist_info exitoso
        self._site        = ""

    def compose(self) -> ComposeResult:
        cfg = load_config()
        with Container(id="modal-card"):
            yield Label("🍒 Nuevo perfil", classes="cherry-accent")
            yield Rule()

            # ── URL ──────────────────────────────────────────────────────
            yield Label("URL principal:", classes="modal-field-label")
            with Horizontal(classes="modal-input-row"):
                yield Input(
                    placeholder="https://kemono.cr/patreon/user/...",
                    id="inp-url",
                )
                yield Button("⟳ Resolver", id="btn-resolve", classes="btn-small")
            yield Label("", id="lbl-url-status", classes="modal-status")

            # ── Nombre ───────────────────────────────────────────────────
            yield Label("Nombre del artista:", classes="modal-field-label")
            with Horizontal(classes="modal-input-row"):
                yield Input(placeholder="Nombre visible", id="inp-name")
                yield Button("← API", id="btn-fetch-name", classes="btn-small")

            # ── Carpeta ──────────────────────────────────────────────────
            yield Label("Carpeta de destino:", classes="modal-field-label")
            with Horizontal(classes="modal-input-row"):
                yield Input(
                    placeholder=str(cfg.download_path / "sitio" / "artista"),
                    id="inp-folder",
                )
                yield Button("Auto", id="btn-auto-folder", classes="btn-small")

            # ── Opciones ─────────────────────────────────────────────────
            yield Rule()
            with Horizontal(classes="modal-options-row"):
                yield Label("Workers:", classes="modal-opt-label")
                yield Input("3", id="inp-workers", classes="modal-opt-input")
                yield Label("Filtro ext:", classes="modal-opt-label")
                yield Input(
                    "",
                    id="inp-ext-filter",
                    placeholder="jpg,png,mp4  (vacío = todos)",
                    classes="modal-opt-filter",
                )

            # ── Botones ──────────────────────────────────────────────────
            with Horizontal(id="modal-buttons"):
                yield Button("Cancelar",           id="btn-cancel")
                yield Button("✓ Crear",            id="btn-create",    classes="-primary")
                yield Button("▶ Crear y descargar", id="btn-create-dl", classes="-primary")

    # ── Eventos ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-resolve":     self.run_worker(self._resolve_url(),  exclusive=True, group="resolve")
            case "btn-fetch-name":  self.run_worker(self._fetch_name(),   exclusive=True, group="resolve")
            case "btn-auto-folder": self._auto_folder()
            case "btn-create":      self._submit(download=False)
            case "btn-create-dl":   self._submit(download=True)
            case "btn-cancel":      self.dismiss(None)

    # ── Workers ──────────────────────────────────────────────────────────────

    async def _resolve_url(self) -> None:
        from ..auth.patreon import NeedsManualAuth
        from ..auth.pixiv import NeedsPixivAuth
        from ..engine import DownloadEngine
        from ..templates._registry import get_template

        url = self.query_one("#inp-url", Input).value.strip()
        if not url:
            return
        lbl = self.query_one("#lbl-url-status", Label)
        lbl.update("[yellow]Resolviendo…[/]")
        try:
            async with DownloadEngine(load_config(), workers=1) as engine:
                tmpl = get_template(url, engine)
                if not tmpl:
                    lbl.update("[red]✗ Sin template para esta URL[/]")
                    return
                self._artist_info = await tmpl.get_artist_info(url)
                self._site = tmpl.name
                lbl.update(
                    f"[green]✓ {tmpl.name.upper()} · "
                    f"{self._artist_info.name} · "
                    f"ID: {self._artist_info.artist_id}[/]"
                )
            name_inp = self.query_one("#inp-name", Input)
            if not name_inp.value.strip():
                name_inp.value = self._artist_info.name
            self._auto_folder()

            # Chequeo de duplicados por similitud de nombre (Levenshtein)
            resolved_name = self._artist_info.name
            profiles = await list_profiles(INDEX_DB)
            for p in profiles:
                if _name_similarity(resolved_name, p["display_name"]) >= 0.80:
                    self.app.notify(
                        f'⚠ Nombre similar al perfil existente: "{p["display_name"]}"\n'
                        "Revisa si ya existe este artista antes de continuar.",
                        severity="warning",
                        timeout=8,
                    )
                    break
        except NeedsManualAuth:
            lbl.update("[yellow]⚠ Se requiere autenticación con Patreon[/]")
            ok = await self.app.push_screen_wait(PatreonAuthModal())
            if ok:
                await self._resolve_url()
        except NeedsPixivAuth:
            lbl.update("[yellow]⚠ Se requiere autenticación con Pixiv[/]")
            ok = await self.app.push_screen_wait(PixivAuthModal())
            if ok:
                await self._resolve_url()
        except Exception as exc:
            lbl.update(f"[red]✗ Error: {exc}[/]")

    async def _fetch_name(self) -> None:
        if not self._artist_info:
            await self._resolve_url()
        if self._artist_info:
            self.query_one("#inp-name", Input).value = self._artist_info.name
            self._auto_folder()

    def _auto_folder(self) -> None:
        if not self._artist_info:
            return
        cfg    = load_config()
        name   = self.query_one("#inp-name", Input).value.strip() or self._artist_info.name
        folder = cfg.download_path / safe_dirname(name)
        self.query_one("#inp-folder", Input).value = str(folder)

    def _submit(self, download: bool) -> None:
        name   = self.query_one("#inp-name",   Input).value.strip()
        url    = self.query_one("#inp-url",    Input).value.strip()
        folder = self.query_one("#inp-folder", Input).value.strip()
        try:
            workers = int(self.query_one("#inp-workers", Input).value or "3")
        except ValueError:
            workers = 3
        ext_filter = self.query_one("#inp-ext-filter", Input).value.strip()
        if not name or not url or not folder:
            self.app.notify("Completa nombre, URL y carpeta", severity="error")
            return
        self.dismiss({
            "name":       name,
            "url":        url,
            "folder":     folder,
            "site":       self._site,
            "workers":    workers,
            "ext_filter": ext_filter,
            "download":   download,
        })


# ── PatreonAuthModal ────────────────────────────────────────────────────────

class PatreonAuthModal(ModalScreen):
    """
    Modal de autenticación de Patreon.

    Flujo:
      1. Botón "Abrir Patreon" → webbrowser.open() en el browser del sistema
      2. Usuario inicia sesión en su browser normal
      3. Botón "Ya inicié sesión" → browser_cookie3 lee las cookies
      4. Si encuentra session_id → guarda en session.json → dismiss(True)
      5. Si no → muestra error y permite reintentar
    """

    DEFAULT_CSS = """
    PatreonAuthModal {
        align: center middle;
    }
    PatreonAuthModal > Vertical {
        width: 62;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    PatreonAuthModal #lbl-status {
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Autenticación de Patreon[/]")
            yield Rule()
            yield Label(
                "No se detectó sesión activa de Patreon en tu navegador.\n"
            )
            yield Label("Paso 1 — Abre Patreon e inicia sesión:")
            yield Button(
                "🌐  Abrir patreon.com/login",
                id="btn-open-browser",
                variant="primary",
            )
            yield Label("")
            yield Label("Paso 2 — Vuelve aquí y confirma:")
            yield Button(
                "✓  Ya inicié sesión",
                id="btn-check",
                variant="success",
            )
            yield Label("", id="lbl-status")
            yield Rule()
            yield Button("Cancelar", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-open-browser":
                import webbrowser
                webbrowser.open("https://www.patreon.com/login")
            case "btn-check":
                self.run_worker(
                    self._try_cookies(), exclusive=True, group="auth"
                )
            case "btn-cancel":
                self.dismiss(False)

    async def _try_cookies(self) -> None:
        """Busca cookies en el browser tras el login del usuario."""
        import asyncio
        from ..auth.patreon import load_from_browser, save_patreon_cookies

        lbl = self.query_one("#lbl-status", Label)
        lbl.update("[yellow]Buscando sesión en el navegador…[/]")

        # browser_cookie3 es síncrono — ejecutar en thread
        cookies = await asyncio.to_thread(load_from_browser)

        if cookies:
            save_patreon_cookies(cookies)
            lbl.update("[green]✓ Sesión detectada correctamente[/]")
            await asyncio.sleep(0.8)
            self.dismiss(True)
        else:
            lbl.update(
                "[red]✗ No se encontró sesión. "
                "¿Completaste el login en el navegador?[/]"
            )


# ── PixivAuthModal ───────────────────────────────────────────────────────────

class PixivAuthModal(ModalScreen):
    """
    Modal de autenticación de Pixiv.

    Flujo:
      1. Botón "Abrir Pixiv" → webbrowser.open() en el browser del sistema
      2. Usuario inicia sesión en su browser normal (pixiv.net/login)
      3. Botón "Ya inicié sesión" → browser_cookie3 lee las cookies
      4. Si encuentra PHPSESSID → guarda en session.json → dismiss(True)
      5. Si no → muestra error y permite reintentar
    """

    DEFAULT_CSS = """
    PixivAuthModal {
        align: center middle;
    }
    PixivAuthModal > Vertical {
        width: 62;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    PixivAuthModal #lbl-status {
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Autenticación de Pixiv[/]")
            yield Rule()
            yield Label(
                "No se detectó sesión activa de Pixiv en tu navegador.\n"
            )
            yield Label("Paso 1 — Abre Pixiv e inicia sesión:")
            yield Button(
                "🌐  Abrir pixiv.net/login",
                id="btn-open-browser",
                variant="primary",
            )
            yield Label("")
            yield Label("Paso 2 — Vuelve aquí y confirma:")
            yield Button(
                "✓  Ya inicié sesión",
                id="btn-check",
                variant="success",
            )
            yield Label("", id="lbl-status")
            yield Rule()
            yield Button("Cancelar", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-open-browser":
                import webbrowser
                webbrowser.open("https://www.pixiv.net/login.php")
            case "btn-check":
                self.run_worker(
                    self._try_cookies(), exclusive=True, group="auth"
                )
            case "btn-cancel":
                self.dismiss(False)

    async def _try_cookies(self) -> None:
        """Busca cookies de Pixiv en el browser tras el login del usuario."""
        import asyncio
        from ..auth.pixiv import load_from_browser, save_pixiv_cookies

        lbl = self.query_one("#lbl-status", Label)
        lbl.update("[yellow]Buscando sesión en el navegador…[/]")

        # browser_cookie3 es síncrono — ejecutar en thread
        cookies = await asyncio.to_thread(load_from_browser)

        if cookies:
            save_pixiv_cookies(cookies)
            lbl.update("[green]✓ Sesión detectada correctamente[/]")
            await asyncio.sleep(0.8)
            self.dismiss(True)
        else:
            lbl.update(
                "[red]✗ No se encontró sesión. "
                "¿Completaste el login en el navegador?[/]"
            )


# ── ProfilesScreen ──────────────────────────────────────────────────────────

class ProfilesScreen(Screen):
    """Pantalla principal: lista de perfiles."""

    BINDINGS = [
        Binding("n",      "new_profile",    "Nuevo",    show=True),
        Binding("enter",  "open_profile",   "Abrir",    show=True),
        Binding("delete", "delete_profile", "Eliminar", show=True),
        Binding("s",      "settings",       "Config",   show=True),
        Binding("r",      "refresh",        "Refresh",  show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="profiles-toolbar"):
            yield Button("+ Nuevo",          id="btn-new",        classes="-primary")
            yield Button("⟳ Refresh",        id="btn-refresh")
            yield Button("⌫ Eliminar",       id="btn-delete",     classes="-danger")
            yield Button("⊗ Comparar",       id="btn-compare")
            yield Button("↑ Actualizar Todo", id="btn-scan-all")
            yield Button("⚡ Batch",          id="btn-batch")
            yield Button("⟳ Chequear Todo",  id="btn-check-all")
            yield Button("⟲ Reindexar",      id="btn-reindex")
            yield Button("⚙ Config",         id="btn-settings")
        yield Label("  PERFILES", classes="section-label")
        yield DataTable(id="profiles-table", cursor_type="row")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-new":       self.action_new_profile()
            case "btn-refresh":   self.action_refresh()
            case "btn-delete":    self.action_delete_profile()
            case "btn-compare":   self.action_compare_profiles()
            case "btn-scan-all":  self.action_scan_all()
            case "btn-batch":     self.action_batch_download()
            case "btn-check-all": self.action_check_all()
            case "btn-reindex":   self.action_reindex()
            case "btn-settings":  self.action_settings()

    def on_mount(self) -> None:
        tbl = self.query_one("#profiles-table", DataTable)
        tbl.add_column("#",           width=4)
        tbl.add_column("Nombre",      width=25)
        tbl.add_column("Sitio",       width=10)
        tbl.add_column("Archivos",    width=10)
        tbl.add_column("Estado",      width=12)
        tbl.add_column("Última sync", width=14)
        tbl.add_column("Carpeta",     width=36)
        self.run_worker(self._startup_load(), exclusive=True)

    async def _startup_load(self) -> None:
        """Carga inicial con auto-reindex si el índice está vacío.

        Escenario "recién cambié de OS": index.db por-máquina aún no existe pero
        la partición compartida ya tiene carpetas auto-describibles → se
        reconstruye el índice automáticamente (idempotente, no destructivo).
        """
        try:
            profiles = await list_profiles(INDEX_DB)
            if not profiles:
                base = load_config().download_path
                if base.is_dir() and any(
                    (d / "catalog.db").exists()
                    for d in base.iterdir() if d.is_dir()
                ):
                    self.app.notify("Índice vacío — reconstruyendo desde las carpetas…")
                    stats = await reindex_from_folders(INDEX_DB, base)
                    if stats["profiles"]:
                        self.app.notify(
                            f"Reindex: {stats['profiles']} perfiles restaurados"
                        )
        except Exception:
            pass
        await self._load_profiles()

    async def _load_profiles(self) -> None:
        tbl = self.query_one("#profiles-table", DataTable)
        tbl.clear()
        try:
            profiles = await list_profiles(INDEX_DB)
            for p in profiles:
                folder = Path(p["folder_path"])
                stats  = await get_stats(folder) if folder.exists() else {"total": 0}
                last   = (p.get("last_checked") or "Nunca")[:10]

                # Indicador de estado basado en pending_queue
                # Si el perfil tiene filtro configurado, el conteo refleja
                # solo los archivos pendientes que coinciden con ese filtro.
                if not folder.exists():
                    estado = "[dim]?[/]"
                else:
                    _stored_ext = p.get("ext_filter", "")
                    _, _eff_ext = _decode_profile_filter(_stored_ext)
                    n_pending = await pending_count(folder, ext_filter=_eff_ext or None)
                    if n_pending > 0:
                        estado = f"[yellow]⏳ {n_pending}[/]"
                    elif p.get("last_checked"):
                        estado = "[green]✓[/]"
                    else:
                        estado = "[dim]○ Sin sync[/]"

                tbl.add_row(
                    str(p["id"]),
                    p["display_name"],
                    p["primary_site"].upper(),
                    str(stats["total"]),
                    estado,
                    last,
                    str(folder),
                    key=str(p["id"]),
                )
        except Exception as exc:
            self.app.notify(f"Error al cargar perfiles: {exc}", severity="error")

    # ── Acciones ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.run_worker(self._load_profiles(), exclusive=True)

    def action_reindex(self) -> None:
        self.run_worker(self._do_reindex(), exclusive=True)

    async def _do_reindex(self) -> None:
        """Reconstruye index.db desde las carpetas de la biblioteca."""
        base = load_config().download_path
        if not base.is_dir():
            self.app.notify(f"No existe download_dir: {base}", severity="error")
            return
        self.app.notify("Reindexando desde las carpetas…")
        try:
            stats = await reindex_from_folders(INDEX_DB, base)
        except Exception as exc:
            self.app.notify(f"Error al reindexar: {exc}", severity="error")
            return
        msg = (f"Reindex: {stats['profiles']} perfiles, "
               f"{stats['urls']} URLs, {stats['no_meta']} sin metadata")
        self.app.notify(msg)
        await self._load_profiles()

    def action_new_profile(self) -> None:
        self.app.push_screen(NewProfileModal(), self._on_new_profile)

    def _on_new_profile(self, result: dict | None) -> None:
        if result:
            self.run_worker(self._create_profile(result), exclusive=False)

    async def _create_profile(self, data: dict) -> None:
        from ..index import add_profile_url, update_profile_ext_filter
        from ..templates._registry import find_template
        try:
            # Determinar site si el modal no lo resolvió vía API
            site = data.get("site") or ""
            if not site:
                cls  = find_template(data["url"])
                site = cls.name if cls else "unknown"

            profile_id = await create_profile(
                db_path=INDEX_DB,
                display_name=data["name"],
                folder_path=data["folder"],
                primary_site=site,
            )
            await add_profile_url(
                db_path=INDEX_DB,
                profile_id=profile_id,
                url=data["url"],
                site=site,
            )
            if data.get("ext_filter"):
                await update_profile_ext_filter(INDEX_DB, profile_id, data["ext_filter"])

            self.app.notify(f"Perfil '{data['name']}' creado", severity="information")
            await self._load_profiles()

            if data.get("download"):
                self.app.push_screen(ArtistScreen(profile_id))
        except Exception as exc:
            self.app.notify(f"Error: {exc}", severity="error")

    def action_open_profile(self) -> None:
        tbl = self.query_one("#profiles-table", DataTable)
        if tbl.cursor_row is None:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        profile_id = int(row[0])
        self.app.push_screen(ArtistScreen(profile_id))

    def action_delete_profile(self) -> None:
        tbl = self.query_one("#profiles-table", DataTable)
        if tbl.cursor_row is None:
            return
        row = tbl.get_row_at(tbl.cursor_row)
        profile_id = int(row[0])
        self.run_worker(self._delete_profile(profile_id), exclusive=False)

    async def _delete_profile(self, profile_id: int) -> None:
        try:
            await delete_profile(INDEX_DB, profile_id)
            self.app.notify("Perfil eliminado")
            await self._load_profiles()
        except Exception as exc:
            self.app.notify(f"Error al eliminar: {exc}", severity="error")

    # ── Buscar duplicados ──────────────────────────────────────────────────────

    def action_compare_profiles(self) -> None:
        """Abre la pantalla de detección global de perfiles duplicados."""
        self.app.push_screen(DuplicateScreen())

    # ── Actualizar Todo (batch scan, sin descargar) ────────────────────────────

    def action_scan_all(self) -> None:
        """Escanea todos los perfiles en busca de novedades — solo llena la cola."""
        self.run_worker(self._do_scan_all(), exclusive=True, group="scan-all")

    async def _do_scan_all(self) -> None:
        """
        Escanea todos los perfiles en busca de archivos nuevos usando workers
        concurrentes por dominio (max 1 por site, max 3 en total).

        - Sin last_synced → scan completo desde el inicio.
        - Con last_synced → scan incremental desde esa fecha.
        - Solo rellena pending_queue. No descarga nada.
        - Al finalizar refresca la columna Estado de la tabla.
        """
        import json as _json
        from pathlib import Path as _Path
        from ..catalog import (
            init_catalog, url_exists, hash_exists,
            pending_url_exists, add_pending, pending_count,
        )
        from ..engine import DownloadEngine
        from ..index import list_profiles
        from ..templates._registry import find_template, get_template
        from ..templates.base import parse_date_utc
        from ..auth.patreon import NeedsManualAuth
        from ..auth.pixiv import NeedsPixivAuth

        config = load_config()
        _slim = await list_profiles(INDEX_DB)
        if not _slim:
            self.app.notify("No hay perfiles registrados", severity="warning")
            return
        profiles = []
        for _p in _slim:
            _full = await get_profile(INDEX_DB, _p["id"])
            if _full:
                profiles.append(_full)

        # Agrupar (profile, pu) por dominio — cada dominio tiene su propio worker
        domain_items: dict[str, list[tuple]] = {}
        for profile in profiles:
            for pu in profile.get("urls", []):
                if not pu.get("enabled") or not pu.get("url"):
                    continue
                domain = pu.get("site") or "unknown"
                domain_items.setdefault(domain, []).append((profile, pu))

        if not domain_items:
            self.app.notify("No hay URLs habilitadas", severity="warning")
            return

        domains = list(domain_items.keys())
        self.app.notify(
            f"Escaneando {len(domains)} dominio(s) en paralelo: "
            + ", ".join(domains),
            severity="information",
        )

        # Contadores compartidos (asyncio es single-thread — sin race conditions)
        total_new_ref   = [0]
        skipped_auth_ref = [0]

        # Semáforo global: máximo 3 workers concurrentes
        global_sem = asyncio.Semaphore(3)

        async def scan_domain(domain: str, items: list[tuple]) -> None:
            """Escanea todas las URLs de un dominio en secuencia."""
            async with global_sem:
                async with DownloadEngine(config, workers=1) as engine:
                    for profile, pu in items:
                        profile_name = profile.get("display_name", "?")
                        folder = _Path(profile["folder_path"])
                        folder.mkdir(parents=True, exist_ok=True)
                        await init_catalog(folder)

                        template = get_template(pu["url"], engine)
                        if not template:
                            continue

                        # Resolver info del artista
                        try:
                            artist_info = await template.get_artist_info(pu["url"])
                        except NeedsManualAuth:
                            self.app.notify(
                                f"⚠ [{domain}] {profile_name}: requiere auth Patreon",
                                severity="warning",
                            )
                            skipped_auth_ref[0] += 1
                            continue
                        except NeedsPixivAuth:
                            self.app.notify(
                                f"⚠ [{domain}] {profile_name}: requiere auth Pixiv",
                                severity="warning",
                            )
                            skipped_auth_ref[0] += 1
                            continue
                        except Exception as exc:
                            self.app.notify(
                                f"✗ [{domain}] {profile_name}: {exc}",
                                severity="error",
                            )
                            continue

                        pu_id: int | None = pu.get("id")

                        # Si ya hay pendientes de sesión anterior, no re-escanear
                        existing = await pending_count(folder, pu_id)
                        if existing > 0:
                            total_new_ref[0] += existing
                            continue

                        # Sin last_synced → scan completo; con → incremental
                        url_since = None
                        if pu.get("last_synced"):
                            url_since = parse_date_utc(pu["last_synced"])

                        seen: set[str] = set()
                        try:
                            async for fi in template.iter_files(
                                artist_info, since=url_since
                            ):
                                key = fi.dedup_key
                                if key in seen:
                                    continue
                                seen.add(key)
                                if await url_exists(folder, key):
                                    continue
                                if fi.remote_hash and await hash_exists(
                                    folder, fi.remote_hash
                                ):
                                    continue
                                if not await pending_url_exists(folder, key):
                                    await add_pending(
                                        folder,
                                        url_source=key,
                                        download_url=fi.url,
                                        filename_hint=fi.filename,
                                        post_id=fi.post_id,
                                        post_published=fi.date_published,
                                        remote_hash=fi.remote_hash,
                                        extra_headers=(
                                            _json.dumps(fi.extra_headers)
                                            if fi.extra_headers else None
                                        ),
                                        profile_url_id=pu_id,
                                    )
                                    total_new_ref[0] += 1
                        except Exception as exc:
                            self.app.notify(
                                f"✗ [{domain}] {profile_name} (scan): {exc}",
                                severity="error",
                            )

        # Lanzar un worker por dominio, todos en paralelo (acotados por global_sem)
        await asyncio.gather(
            *[scan_domain(d, items) for d, items in domain_items.items()],
            return_exceptions=True,
        )

        # Refrescar tabla con nuevos estados de pending_queue
        await self._load_profiles()
        msg = (
            f"Scan completo — {len(domains)} dominio(s), "
            f"{total_new_ref[0]} archivo(s) nuevo(s) en cola"
        )
        if skipped_auth_ref[0]:
            msg += f" · {skipped_auth_ref[0]} saltado(s) por auth"
        self.app.notify(msg, severity="information")

    # ── Batch download ─────────────────────────────────────────────────────────

    def action_batch_download(self) -> None:
        """Abre la pantalla de descarga por lotes."""
        self.app.push_screen(BatchScreen())

    # ── Chequear todo ──────────────────────────────────────────────────────────

    def action_check_all(self) -> None:
        self.run_worker(self._do_check_all(), exclusive=True, group="check-all")

    async def _do_check_all(self) -> None:
        """
        Recarga la tabla de perfiles completa, recalculando el estado de
        pending_queue para cada perfil. No descarga nada.
        """
        try:
            profiles = await list_profiles(INDEX_DB)
            self.app.notify(
                f"Chequeando {len(profiles)} perfil(es)…", severity="information"
            )
            await self._load_profiles()
            self.app.notify("Estado actualizado", severity="information")
        except Exception as exc:
            self.app.notify(f"Error al chequear: {exc}", severity="error")

    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row = self.query_one("#profiles-table", DataTable).get_row_at(event.cursor_row)
        profile_id = int(row[0])
        self.app.push_screen(ArtistScreen(profile_id))


# ── CompactConfirmModal ─────────────────────────────────────────────────────

class CompactConfirmModal(ModalScreen):
    """Modal de doble confirmación antes de compactar numeración."""

    DEFAULT_CSS = """
    CompactConfirmModal > Vertical {
        width: 60;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }
    CompactConfirmModal #compact-warning {
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }
    CompactConfirmModal #compact-info {
        margin-bottom: 1;
    }
    CompactConfirmModal Horizontal {
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, total: int, to_rename: int) -> None:
        super().__init__()
        self._total     = total
        self._to_rename = to_rename

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("⊟ Compactar numeración", id="compact-warning")
            yield Label(
                f"Se renombrarán [bold yellow]{self._to_rename}[/] archivos "
                f"de [bold]{self._total}[/] en disco.\n"
                "Esta acción modifica nombres en disco y no se puede deshacer.",
                id="compact-info",
                markup=True,
            )
            with Horizontal():
                yield Button(
                    "Cancelar", id="btn-compact-cancel", variant="default"
                )
                yield Button(
                    "Confirmar", id="btn-compact-ok", variant="warning"
                )

    def on_mount(self) -> None:
        self.query_one("#btn-compact-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-compact-ok")


# ── DuplicateScreen ──────────────────────────────────────────────────────────

def _dup_keep_remove(pair: dict) -> tuple[int, int, str, str]:
    """
    Dado un par, retorna (keep_id, remove_id, keep_name, remove_name).
    El perfil más antiguo (created_at menor, o id menor) absorbe al más nuevo.
    """
    from datetime import datetime

    def _dt(s: str | None) -> datetime:
        if not s:
            return datetime.max
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return datetime.max

    dt_a = _dt(pair.get("created_at_a"))
    dt_b = _dt(pair.get("created_at_b"))
    if dt_a <= dt_b:
        return pair["id_a"], pair["id_b"], pair["name_a"], pair["name_b"]
    return pair["id_b"], pair["id_a"], pair["name_b"], pair["name_a"]


class HashScanWarningModal(ModalScreen):
    """Advertencia antes de iniciar la comparación intensiva por hashes."""

    DEFAULT_CSS = """
    HashScanWarningModal > Vertical {
        width: 68;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }
    HashScanWarningModal Label { margin-bottom: 1; }
    HashScanWarningModal Horizontal { height: 3; align: center middle; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("↺ Comparación por hashes", markup=True)
            yield Label(
                "Esta fase compara los hashes SHA-256 ya almacenados en tus\n"
                "bases de datos — [bold]no escanea archivos en disco[/bold].\n\n"
                "Se ejecuta como una consulta SQL directa (INNER JOIN).\n"
                "Con catálogos de >50,000 archivos puede tardar varios minutos.\n\n"
                "Puedes seguir viendo los resultados de la Fase 1\n"
                "mientras el scan corre en background.",
                markup=True,
            )
            with Horizontal():
                yield Button("Cancelar",    id="btn-hsw-cancel", variant="default")
                yield Button("Iniciar scan", id="btn-hsw-ok",    variant="warning")

    def on_mount(self) -> None:
        self.query_one("#btn-hsw-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-hsw-ok")


class OrphanActionModal(ModalScreen):
    """
    Pregunta qué hacer con los archivos huérfanos tras una fusión.
    Retorna "delete" | "rename" | "ignore".
    """

    DEFAULT_CSS = """
    OrphanActionModal > Vertical {
        width: 68;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    OrphanActionModal Label { margin-bottom: 1; }
    OrphanActionModal Horizontal { height: 3; align: center middle; }
    """

    def __init__(self, folder: str, n_orphans: int) -> None:
        super().__init__()
        self._folder   = folder
        self._n_orphans = n_orphans

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Archivos huérfanos", markup=True)
            yield Label(
                f"[bold]{self._n_orphans}[/bold] archivo(s) en\n"
                f"[dim]{self._folder}[/dim]\n"
                "ya existen en el perfil destino (mismo hash).\n\n"
                "¿Qué deseas hacer con ellos?",
                markup=True,
            )
            with Horizontal():
                yield Button("Borrar",           id="btn-oa-delete",  variant="error")
                yield Button("Renombrar carpeta", id="btn-oa-rename",  variant="warning")
                yield Button("Ignorar",           id="btn-oa-ignore",  variant="default")

    def on_mount(self) -> None:
        self.query_one("#btn-oa-ignore", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "btn-oa-delete": "delete",
            "btn-oa-rename": "rename",
            "btn-oa-ignore": "ignore",
        }
        self.dismiss(mapping.get(event.button.id, "ignore"))


class DuplicateScreen(Screen):
    """
    Pantalla de detección y fusión de perfiles duplicados.

    Fase 1 (automática al montar): compara URLs y nombres de todos los pares.
      - URL match o nombre >= 0.80 → sección "Revisión / Auto"
    Fase 2 (manual, bajo demanda): SQL ATTACH INNER JOIN en catalog.db.
      - coverage >= 0.51 → pasa a "Fusión automática"
      - coverage 0.10–0.50 → pasa a "Revisión manual" con porcentaje
    """

    BINDINGS = [("escape", "go_back", "Volver")]

    # ── constantes de umbral ───────────────────────────────────────────────────
    HASH_AUTO   = 0.51   # coverage >= HASH_AUTO → fusión automática
    HASH_MIN    = 0.10   # coverage < HASH_MIN   → ruido, ignorar
    NAME_PROB   = 0.80   # nombre similar ≥ PROB → PROBABLE
    NAME_POSS   = 0.60   # nombre similar ≥ POSS → POSSIBLE

    def __init__(self) -> None:
        super().__init__()
        # Pares confirmados para fusión automática
        self._auto:   list[dict] = []
        # Pares para revisión manual  {…, "checked": bool}
        self._review: list[dict] = []
        # Set de exclusiones cargadas de index.db
        self._exclusions: set[tuple[int, int]] = set()
        # Pares pendientes de scan de hashes (los que no fueron DEFINITE en fase 1)
        self._phase2_candidates: list[dict] = []
        self._phase2_running = False
        # Perfiles pendientes de compactar después de fusiones
        self._pending_compact: list[tuple[int, str]] = []

    def compose(self) -> ComposeResult:
        yield Header("⊗ Buscar perfiles duplicados")
        with Vertical(id="dup-main"):
            yield Label("", id="dup-status")
            yield ProgressBar(id="dup-progress", show_eta=False, total=100)

            # ── Sección automática ─────────────────────────────────────────
            yield Label(
                "[bold green]FUSIÓN AUTOMÁTICA[/]  "
                "[dim](URL match o ≥51% hashes en común)[/]",
                id="dup-auto-title", markup=True,
            )
            yield DataTable(
                id="dup-auto-table",
                cursor_type="row",
                zebra_stripes=True,
            )

            # ── Sección revisión ───────────────────────────────────────────
            yield Label(
                "[bold yellow]REVISIÓN MANUAL[/]  "
                "[dim](nombre similar o hash 10–50%)[/]",
                id="dup-review-title", markup=True,
            )
            yield DataTable(
                id="dup-review-table",
                cursor_type="row",
                zebra_stripes=True,
            )

            # ── Botones de acción ──────────────────────────────────────────
            with Horizontal(id="dup-actions"):
                yield Button(
                    "↺ Comparar por hashes",
                    id="btn-dup-hash", variant="default",
                )
                yield Button(
                    "✓ Ejecutar fusiones auto",
                    id="btn-dup-exec-auto", variant="success", disabled=True,
                )
                yield Button(
                    "⟳ Fusionar seleccionados",
                    id="btn-dup-exec-manual", variant="warning", disabled=True,
                )
                yield Button(
                    "✕ Marcar como distintos",
                    id="btn-dup-exclude", variant="default", disabled=True,
                )
                yield Button("← Volver", id="btn-dup-back")

        yield Footer()

    async def on_mount(self) -> None:
        # Configurar columnas de las tablas
        auto_tbl = self.query_one("#dup-auto-table", DataTable)
        auto_tbl.add_columns("✓", "Mantener", "←", "Eliminar", "Razón")

        rev_tbl = self.query_one("#dup-review-table", DataTable)
        rev_tbl.add_columns("☐", "Perfil A", "↔", "Perfil B", "Similitud")

        # Cargar exclusiones y lanzar fase 1
        self._exclusions = await get_exclusions(INDEX_DB)
        self.run_worker(self._scan_phase1(), exclusive=False, name="dup-phase1")

    # ── Helpers UI ────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#dup-status", Label).update(msg)
        except Exception:
            pass

    def _set_progress(self, done: int, total: int) -> None:
        try:
            bar = self.query_one("#dup-progress", ProgressBar)
            bar.total   = max(total, 1)
            bar.progress = done
        except Exception:
            pass

    def _refresh_buttons(self) -> None:
        try:
            self.query_one("#btn-dup-exec-auto",   Button).disabled = len(self._auto)   == 0
            self.query_one("#btn-dup-exec-manual", Button).disabled = not any(
                p.get("checked") for p in self._review
            )
            self.query_one("#btn-dup-exclude",     Button).disabled = not any(
                p.get("checked") for p in self._review
            )
        except Exception:
            pass

    def _excluded(self, id_a: int, id_b: int) -> bool:
        lo, hi = min(id_a, id_b), max(id_a, id_b)
        return (lo, hi) in self._exclusions

    def _add_auto_row(self, pair: dict) -> None:
        """Agrega un par a la tabla de fusión automática."""
        keep_id, remove_id, keep_name, remove_name = _dup_keep_remove(pair)
        tbl = self.query_one("#dup-auto-table", DataTable)
        tbl.add_row(
            "✓",
            keep_name,
            "←",
            remove_name,
            pair.get("reason", ""),
            key=f"auto_{keep_id}_{remove_id}",
        )

    def _add_review_row(self, pair: dict) -> None:
        """Agrega un par a la tabla de revisión manual."""
        tbl = self.query_one("#dup-review-table", DataTable)
        tbl.add_row(
            "☐",
            pair["name_a"],
            "↔",
            pair["name_b"],
            pair.get("reason", ""),
            key=f"rev_{pair['id_a']}_{pair['id_b']}",
        )

    # ── Fase 1: URL + nombre ──────────────────────────────────────────────────

    async def _scan_phase1(self) -> None:
        self._set_status("[bold]Fase 1:[/] comparando URLs y nombres…")
        try:
            profiles = await list_profiles(INDEX_DB)
            # Cargar URLs de cada perfil (necesitamos artist_id y site)
            full: list[dict] = []
            for p in profiles:
                fp = await get_profile(INDEX_DB, p["id"])
                if fp:
                    full.append(fp)

            n = len(full)
            total_pairs = n * (n - 1) // 2
            done = 0

            for i in range(n):
                for j in range(i + 1, n):
                    a, b = full[i], full[j]

                    if self._excluded(a["id"], b["id"]):
                        done += 1
                        continue

                    pair_base = {
                        "id_a": a["id"], "name_a": a["display_name"],
                        "folder_a": a["folder_path"], "created_at_a": a.get("created_at"),
                        "id_b": b["id"], "name_b": b["display_name"],
                        "folder_b": b["folder_path"], "created_at_b": b.get("created_at"),
                    }

                    # ── Nivel 1: URL match ─────────────────────────────────
                    url_match = _url_overlap(a["urls"], b["urls"])
                    if url_match:
                        pair = {**pair_base,
                                "tier":   "url_match",
                                "reason": f"URL: {url_match}"}
                        self._auto.append(pair)
                        self._add_auto_row(pair)
                        self._refresh_buttons()
                        done += 1
                        self._set_progress(done, total_pairs)
                        continue

                    # ── Nivel 2: similitud de nombre ───────────────────────
                    sim = _name_similarity(a["display_name"], b["display_name"])
                    if sim >= self.NAME_PROB:
                        pair = {**pair_base,
                                "tier":    "name_similar",
                                "reason":  f"nombre {sim*100:.0f}% similar",
                                "checked": False}
                        self._review.append(pair)
                        self._phase2_candidates.append(pair)
                        self._add_review_row(pair)
                        self._refresh_buttons()
                    elif sim >= self.NAME_POSS:
                        pair = {**pair_base,
                                "tier":    "name_possible",
                                "reason":  f"nombre {sim*100:.0f}% similar",
                                "checked": False}
                        self._review.append(pair)
                        self._phase2_candidates.append(pair)
                        self._add_review_row(pair)
                        self._refresh_buttons()
                    else:
                        # Sin match de URL ni nombre → candidato solo para hash
                        self._phase2_candidates.append({
                            **pair_base,
                            "tier": "unknown", "reason": "",
                        })

                    done += 1
                    self._set_progress(done, total_pairs)

            n_auto   = len(self._auto)
            n_review = len(self._review)
            self._set_status(
                f"[bold]Fase 1 completa[/] — "
                f"{n_auto} para fusión automática · "
                f"{n_review} para revisión · "
                f"{total_pairs} pares analizados"
            )
            self._set_progress(total_pairs, total_pairs)

        except Exception as exc:
            import traceback as _tb
            self._set_status(f"[red]Error en fase 1: {exc}[/]")
            self.log(_tb.format_exc())

    # ── Fase 2: hashes via ATTACH ─────────────────────────────────────────────

    async def _scan_phase2(self) -> None:
        """
        Para cada par aún sin resolución definitiva, ejecuta compare_by_hash_join.
        Actualiza las tablas en tiempo real conforme llegan los resultados.
        """
        self._phase2_running = True
        try:
            # Solo pares que NO son ya URL match (esos ya están en auto)
            candidates = [
                p for p in self._phase2_candidates
                if p.get("tier") != "url_match"
                and not self._excluded(p["id_a"], p["id_b"])
            ]
            total = len(candidates)
            if total == 0:
                self._set_status("[dim]No hay pares candidatos para scan de hashes.[/]")
                return

            self._set_status(
                f"[bold]Fase 2:[/] comparando hashes ({total} par(es))…"
            )

            for done, pair in enumerate(candidates):
                folder_a = Path(pair["folder_a"])
                folder_b = Path(pair["folder_b"])
                try:
                    result = await compare_by_hash_join(folder_a, folder_b)
                except Exception as exc:
                    self.log(f"Hash join error ({pair['name_a']} ↔ {pair['name_b']}): {exc}")
                    self._set_progress(done + 1, total)
                    continue

                coverage = result.get("coverage", 0.0)
                total_a  = result.get("total_a", 0)
                total_b  = result.get("total_b", 0)

                if coverage < self.HASH_MIN:
                    # Sin relación → ignorar
                    pass
                elif coverage >= self.HASH_AUTO:
                    # Fusión automática
                    new_pair = {
                        **pair,
                        "tier":   "hash_definite",
                        "reason": f"{coverage*100:.1f}% hashes en común "
                                  f"({result['matches']}/{min(total_a,total_b)})",
                    }
                    self._auto.append(new_pair)
                    self._add_auto_row(new_pair)
                    # Si estaba en revisión, removerlo
                    self._remove_review_pair(pair["id_a"], pair["id_b"])
                    self._refresh_buttons()
                else:
                    # Solo mostrar si no está ya en revisión (evitar duplicar)
                    existing = self._find_review_pair(pair["id_a"], pair["id_b"])
                    new_reason = (
                        f"{coverage*100:.1f}% hashes en común "
                        f"({result['matches']}/{min(total_a,total_b)})"
                    )
                    if existing:
                        existing["reason"] = new_reason
                        # Actualizar celda de razón en la tabla
                        self._update_review_reason(pair["id_a"], pair["id_b"], new_reason)
                    else:
                        new_pair = {
                            **pair,
                            "tier":    "hash_probable",
                            "reason":  new_reason,
                            "checked": False,
                        }
                        self._review.append(new_pair)
                        self._add_review_row(new_pair)
                        self._refresh_buttons()

                self._set_progress(done + 1, total)

            n_auto   = len(self._auto)
            n_review = len(self._review)
            self._set_status(
                f"[bold]Fase 2 completa[/] — "
                f"{n_auto} para fusión automática · "
                f"{n_review} para revisión"
            )
        except Exception as exc:
            import traceback as _tb
            self._set_status(f"[red]Error en fase 2: {exc}[/]")
            self.log(_tb.format_exc())
        finally:
            self._phase2_running = False

    def _find_review_pair(self, id_a: int, id_b: int) -> dict | None:
        for p in self._review:
            if {p["id_a"], p["id_b"]} == {id_a, id_b}:
                return p
        return None

    def _remove_review_pair(self, id_a: int, id_b: int) -> None:
        self._review = [
            p for p in self._review
            if {p["id_a"], p["id_b"]} != {id_a, id_b}
        ]
        # Intentar quitar la fila de la tabla
        key = f"rev_{min(id_a,id_b)}_{max(id_a,id_b)}"
        try:
            tbl = self.query_one("#dup-review-table", DataTable)
            # DataTable no tiene remove_row por key directo; marcamos la fila
            # con tachado como proxy visual (la fila migró a auto)
            tbl.update_cell(key, "☐", "[dim]→auto[/]", update_width=False)
        except Exception:
            pass

    def _update_review_reason(self, id_a: int, id_b: int, reason: str) -> None:
        key = f"rev_{id_a}_{id_b}"
        try:
            tbl = self.query_one("#dup-review-table", DataTable)
            tbl.update_cell(key, "Similitud", reason, update_width=False)
        except Exception:
            pass

    # ── Toggle checkbox en tabla de revisión ──────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "dup-review-table":
            return
        row_key = str(event.row_key.value) if event.row_key else None
        if not row_key:
            return
        # Buscar el par por row_key  "rev_{id_a}_{id_b}"
        parts = row_key.removeprefix("rev_").split("_")
        if len(parts) != 2:
            return
        try:
            id_a, id_b = int(parts[0]), int(parts[1])
        except ValueError:
            return

        pair = self._find_review_pair(id_a, id_b)
        if not pair:
            return
        pair["checked"] = not pair.get("checked", False)
        mark = "☑" if pair["checked"] else "☐"
        try:
            tbl = self.query_one("#dup-review-table", DataTable)
            tbl.update_cell(row_key, "☐", mark, update_width=False)
        except Exception:
            pass
        self._refresh_buttons()

    # ── Botones ───────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-dup-back":
            self.action_go_back()
        elif bid == "btn-dup-hash":
            if not self._phase2_running:
                self.app.push_screen(
                    HashScanWarningModal(),
                    lambda ok: self.run_worker(
                        self._scan_phase2(), exclusive=False, name="dup-phase2"
                    ) if ok else None,
                )
        elif bid == "btn-dup-exec-auto":
            if self._auto:
                self.run_worker(
                    self._execute_merges(list(self._auto)), exclusive=False
                )
        elif bid == "btn-dup-exec-manual":
            selected = [p for p in self._review if p.get("checked")]
            if selected:
                self.run_worker(
                    self._execute_merges(selected), exclusive=False
                )
        elif bid == "btn-dup-exclude":
            self.run_worker(self._exclude_selected(), exclusive=False)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    # ── Excluir pares seleccionados ───────────────────────────────────────────

    async def _exclude_selected(self) -> None:
        selected = [p for p in self._review if p.get("checked")]
        for pair in selected:
            await add_exclusion(INDEX_DB, pair["id_a"], pair["id_b"])
            lo, hi = min(pair["id_a"], pair["id_b"]), max(pair["id_a"], pair["id_b"])
            self._exclusions.add((lo, hi))
        self._review = [p for p in self._review if not p.get("checked")]
        self.app.notify(
            f"{len(selected)} par(es) marcados como distintos — no aparecerán de nuevo",
            severity="information",
        )
        self._refresh_buttons()

    # ── Ejecutar fusiones ─────────────────────────────────────────────────────

    async def _execute_merges(self, pairs: list[dict]) -> None:
        """
        Para cada par: migra archivos únicos, fusiona el índice,
        gestiona huérfanos y ofrece compactar.
        """
        merged_count  = 0
        total_moved   = 0
        total_orphans = 0
        errors: list[str] = []
        folders_to_compact: list[tuple[int, str]] = []

        for pair in pairs:
            keep_id, remove_id, keep_name, remove_name = _dup_keep_remove(pair)
            keep_folder   = Path(pair["folder_a"] if pair["id_a"] == keep_id else pair["folder_b"])
            remove_folder = Path(pair["folder_b"] if pair["id_a"] == keep_id else pair["folder_a"])

            self._set_status(
                f"Fusionando: [bold]{remove_name}[/] → [bold]{keep_name}[/]…"
            )

            # ── Migrar archivos únicos ────────────────────────────────────
            try:
                result = await migrate_unique_files(remove_folder, keep_folder)
                total_moved   += result["moved"]
                total_orphans += result["orphaned"]

                if result["errors"]:
                    errors.extend(result["errors"][:5])

                # ── Gestionar huérfanos ───────────────────────────────────
                if result["orphaned"] > 0:
                    action = await self.app.push_screen_wait(
                        OrphanActionModal(str(remove_folder), result["orphaned"])
                    )
                    await asyncio.to_thread(
                        _handle_orphans,
                        remove_folder,
                        result["orphaned_paths"],
                        action,
                    )
            except Exception as exc:
                errors.append(f"Migración {remove_name}: {exc}")

            # ── Fusionar índice ───────────────────────────────────────────
            try:
                await merge_profiles(INDEX_DB, keep_id, remove_id)
                merged_count += 1
                if result["moved"] > 0:
                    folders_to_compact.append((keep_id, str(keep_folder)))
            except Exception as exc:
                errors.append(f"Índice {remove_name}: {exc}")

        # ── Resumen ───────────────────────────────────────────────────────
        msg = (
            f"[bold green]✓ {merged_count} fusión(es) completada(s)[/]\n"
            f"  {total_moved} archivo(s) migrados · "
            f"{total_orphans} huérfanos gestionados"
        )
        if errors:
            msg += f"\n[red]{len(errors)} error(es): {errors[0]}…[/]"
        self._set_status(msg)

        # Limpiar pares ejecutados de las listas internas
        merged_ids = {(p["id_a"], p["id_b"]) for p in pairs}
        self._auto   = [p for p in self._auto   if (p["id_a"],p["id_b"]) not in merged_ids]
        self._review = [p for p in self._review if (p["id_a"],p["id_b"]) not in merged_ids]
        self._refresh_buttons()

        # ── Ofrecer compactar ─────────────────────────────────────────────
        if folders_to_compact:
            self.app.push_screen(
                CompactAfterMergeModal(folders_to_compact),
                lambda ok: self.run_worker(
                    _compact_folders(folders_to_compact), exclusive=False
                ) if ok else None,
            )


def _handle_orphans(
    folder: Path,
    orphaned_paths: list[Path],
    action: str,
) -> None:
    """Ejecutado en thread — borra o renombra los archivos/carpeta huérfanos."""
    import shutil

    if action == "delete":
        for p in orphaned_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        # Borrar carpeta si quedó vacía (solo tiene catalog.db)
        try:
            remaining = [f for f in folder.iterdir()
                         if f.name != "catalog.db" and not f.name.endswith(".db")]
            if not remaining:
                shutil.rmtree(folder, ignore_errors=True)
        except Exception:
            pass

    elif action == "rename":
        new_name = folder.parent / f"orphan_{folder.name}"
        try:
            folder.rename(new_name)
        except Exception:
            pass  # Si falla (ya existe, permisos), se deja como está


async def _compact_folders(folders: list[tuple[int, str]]) -> None:
    """Compacta la numeración de las carpetas que recibieron archivos migrados."""
    from ..catalog import apply_compaction, get_numbered_files, plan_compaction

    for _, folder_str in folders:
        folder = Path(folder_str)
        try:
            files = await get_numbered_files(folder)
            plan  = plan_compaction(files)
            if plan:
                new_total = files[-1][0] if files else 0
                await apply_compaction(folder, plan, new_total)
        except Exception:
            pass


class CompactAfterMergeModal(ModalScreen):
    """Ofrece compactar los perfiles que recibieron archivos tras fusionar."""

    DEFAULT_CSS = """
    CompactAfterMergeModal > Vertical {
        width: 65;
        height: auto;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    CompactAfterMergeModal Label { margin-bottom: 1; }
    CompactAfterMergeModal Horizontal { height: 3; align: center middle; }
    """

    def __init__(self, folders: list[tuple[int, str]]) -> None:
        super().__init__()
        self._folders = folders

    def compose(self) -> ComposeResult:
        names = ", ".join(Path(f).name for _, f in self._folders[:3])
        if len(self._folders) > 3:
            names += f" y {len(self._folders)-3} más"
        with Vertical():
            yield Label("¿Compactar numeración?", markup=True)
            yield Label(
                f"Los archivos migrados se agregaron al final de la numeración.\n"
                f"Perfiles afectados: [bold]{names}[/bold]\n\n"
                "Compactar elimina huecos en la secuencia de números.",
                markup=True,
            )
            with Horizontal():
                yield Button("Ahora no", id="btn-cam-no",  variant="default")
                yield Button("Compactar", id="btn-cam-ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#btn-cam-ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-cam-ok")


# ── ArtistScreen ────────────────────────────────────────────────────────────

class ArtistScreen(Screen):
    """Detalle de un perfil con controles de descarga."""

    BINDINGS = [
        Binding("escape", "go_back",          "Volver",    show=True),
        Binding("d",      "start_download",   "Descargar", show=True),
        Binding("c",      "cancel_download",  "Cancelar",  show=True),
        Binding("v",      "verify",           "Verificar", show=True),
    ]

    def __init__(self, profile_id: int, **kwargs):
        super().__init__(**kwargs)
        self._profile_id   = profile_id
        self._profile: dict | None = None
        self._worker_rows: list[WorkerRow] = []
        self._is_busy      = False
        self._pending_exit = False   # pop_screen diferido al terminar workers
        # Progreso total de la sesión de descarga activa.
        # _batch_total  — archivos en la cola al iniciar (scan nuevo + resume)
        # _batch_offset — archivos completados en sesiones anteriores del mismo batch
        self._batch_total:  int = 0
        self._batch_offset: int = 0

    # ── Compose ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        # Cabecera del perfil
        with Horizontal(id="profile-header"):
            yield Label("Cargando…", id="profile-name", classes="cherry-accent")
            yield Label("", id="profile-meta", classes="dim")

        # Tabla de fuentes
        yield Label("  FUENTES", classes="section-label")
        yield DataTable(id="sources-table", cursor_type="row")

        # Botones de fuentes
        with Horizontal(id="source-buttons"):
            yield Button("+ URL",      id="btn-add-url")
            yield Button("- Eliminar", id="btn-del-url", classes="-danger")

        # Controles
        with Horizontal(id="controls-row"):
            yield Label("Workers:")
            yield Input("3", id="workers-input", placeholder="3")
            yield Label("Pre-scan:")
            yield Input("", id="prescan-input", placeholder="Carpeta de archivos existentes")

        # Filtro de tipos de archivo por perfil
        yield Label("  TIPOS A DESCARGAR  (vacío = todos)", classes="section-label")
        with Horizontal(id="artist-filter-groups"):
            for group_id, (label, _exts) in EXT_GROUPS.items():
                yield Checkbox(label, value=False, id=f"artist-chk-{group_id}")
        with Horizontal(id="artist-filter-custom-row"):
            yield Label("Ext. extra:", classes="batch-cfg-label")
            yield Input(
                "", id="artist-ext-custom",
                placeholder="psd,clip  (separadas por coma)",
                classes="batch-cfg-ext",
            )

        # Acciones
        with Horizontal(id="actions-row"):
            yield Button("⬡ Pre-scan",   id="btn-prescan")
            yield Button("⊘ Deduplicar", id="btn-dedup")
            yield Button("⊟ Compactar",  id="btn-compact")
            yield Button("⟳ Verificar",  id="btn-verify")
            yield Button("↑ Actualizar", id="btn-update")
            yield Button("▶ Descargar",  id="btn-download", classes="-primary")
            yield Button("↺ Rescan",     id="btn-rescan")
            yield Button("✕ Cancelar",   id="btn-cancel",   classes="-danger")

        # Panel de workers
        yield Label("  WORKERS", classes="section-label")
        yield Container(id="workers-panel")

        # Encabezado de actividad con semáforo y contadores inline
        with Horizontal(id="status-bar"):
            yield Label("  ACTIVIDAD", classes="section-label")
            yield Label("● Listo", id="semaphore", classes="status-idle")
            yield Static("", id="counters-label")
        yield RichLog(id="activity-log", highlight=True, markup=True)

    def on_mount(self) -> None:
        # Inicializar tabla de fuentes
        tbl = self.query_one("#sources-table", DataTable)
        tbl.add_column("Sitio",       width=10)
        tbl.add_column("URL / ID",    width=50)
        tbl.add_column("Archivos",    width=10)
        tbl.add_column("Última sync", width=14)
        tbl.add_column("Activo",      width=8)

        # Deshabilitar cancelar al inicio
        self.query_one("#btn-cancel", Button).disabled = True

        # Cargar perfil
        self.run_worker(self._load_profile(), exclusive=True, group="load")

    # ── Carga de perfil ───────────────────────────────────────────────────

    async def _load_profile(self) -> None:
        try:
            profile = await get_profile(INDEX_DB, self._profile_id)
            if not profile:
                self.app.notify(f"Perfil #{self._profile_id} no encontrado", severity="error")
                return
            self._profile = profile

            # Cabecera
            self.query_one("#profile-name", Label).update(
                f"🍒 {profile['display_name']}"
            )
            folder = Path(profile["folder_path"])
            stats  = await get_stats(folder) if folder.exists() else {"total": 0, "total_size": 0}
            last   = (profile.get("last_checked") or "Nunca")[:10]
            self.query_one("#profile-meta", Label).update(
                f"  {profile['primary_site'].upper()}  ·  "
                f"{stats['total']:,} archivos  ·  "
                f"{_fmt_size(stats['total_size'])}  ·  última sync: {last}"
            )

            # Restaurar filtro guardado (formato JSON nuevo o legacy)
            _stored_ext = profile.get("ext_filter", "")
            if _stored_ext:
                _group_ids, _ext_set = _decode_profile_filter(_stored_ext)
                # Marcar grupos
                for _gid in _group_ids:
                    try:
                        self.query_one(f"#artist-chk-{_gid}", Checkbox).value = True
                    except Exception:
                        pass
                # Legacy: si no hay group_ids pero sí ext_set, dejar custom
                if not _group_ids and _ext_set:
                    _legacy_txt = ",".join(e.lstrip(".") for e in sorted(_ext_set))
                    try:
                        self.query_one("#artist-ext-custom", Input).value = _legacy_txt
                    except Exception:
                        pass

            # Tabla de fuentes
            self._populate_sources(profile["urls"])

            # Panel de workers (usa valor del input)
            try:
                workers = int(self.query_one("#workers-input", Input).value or "3")
            except ValueError:
                workers = 3
            self._init_worker_panel(workers)

        except Exception as exc:
            self.app.notify(f"Error al cargar perfil: {exc}", severity="error")

    def _populate_sources(self, urls: list[dict]) -> None:
        tbl = self.query_one("#sources-table", DataTable)
        tbl.clear()
        for u in urls:
            display = u["url"] or f"(migrado — ID: {u['artist_id'] or '?'})"
            tbl.add_row(
                u["site"].upper(),
                display[:50],
                str(u["file_count"] or 0),
                (u["last_synced"] or "Nunca")[:10],
                "✓" if u["enabled"] else "✗",
                key=str(u["id"]),
            )

    def _init_worker_panel(self, n: int) -> None:
        panel = self.query_one("#workers-panel", Container)
        # Remover filas existentes
        for wr in self._worker_rows:
            wr.remove()
        self._worker_rows.clear()
        # Crear nuevas
        for i in range(n):
            row = WorkerRow(i)
            panel.mount(row)
            self._worker_rows.append(row)

    # ── Eventos de botones ────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-download":
            self.action_start_download()
        elif btn_id == "btn-update":
            self.action_start_update()
        elif btn_id == "btn-rescan":
            self.action_start_rescan()
        elif btn_id == "btn-cancel":
            self.action_cancel_download()
        elif btn_id == "btn-verify":
            self.action_verify()
        elif btn_id == "btn-add-url":
            self._on_add_url()
        elif btn_id == "btn-del-url":
            self._on_del_url()
        elif btn_id == "btn-dedup":
            self._start_dedup()
        elif btn_id == "btn-compact":
            self._confirm_compact()
        elif btn_id == "btn-prescan":
            self._start_prescan()

    def _on_add_url(self) -> None:
        self.app.push_screen(AddUrlModal(), self._on_url_added)

    def _on_url_added(self, url: str | None) -> None:
        if url:
            self.run_worker(self._add_url_async(url), exclusive=False)

    async def _add_url_async(self, url: str) -> None:
        if not self._profile:
            return
        try:
            from ..templates._registry import find_template
            cls  = find_template(url)
            site = cls.name if cls else "unknown"
            await add_profile_url(
                db_path=INDEX_DB,
                profile_id=self._profile["id"],
                url=url,
                site=site,
            )
            await self._load_profile()
            self.app.notify("URL agregada")
        except Exception as exc:
            self.app.notify(f"Error: {exc}", severity="error")

    def _on_del_url(self) -> None:
        tbl = self.query_one("#sources-table", DataTable)
        if tbl.cursor_row is None:
            return
        self.run_worker(self._del_url_async(tbl.cursor_row), exclusive=False)

    async def _del_url_async(self, row_idx: int) -> None:
        tbl = self.query_one("#sources-table", DataTable)
        keys = list(tbl.rows.keys())
        if row_idx >= len(keys):
            return
        url_id = int(keys[row_idx].value)
        try:
            # Capturar profile_id antes del DELETE para refrescar la metadata.
            async with aiosqlite.connect(INDEX_DB) as db:
                async with db.execute(
                    "SELECT profile_id FROM profile_urls WHERE id = ?", (url_id,)
                ) as cur:
                    prow = await cur.fetchone()
                await db.execute("DELETE FROM profile_urls WHERE id = ?", (url_id,))
                await db.commit()
            if prow:
                await sync_profile_meta(INDEX_DB, prow[0])
            await self._load_profile()
            self.app.notify("URL eliminada")
        except Exception as exc:
            self.app.notify(f"Error: {exc}", severity="error")

    # ── Acciones de teclado ───────────────────────────────────────────────

    def action_go_back(self) -> None:
        if self._is_busy:
            # Cancelar workers y diferir el pop hasta que terminen.
            # _set_busy(False) detectará _pending_exit y hará el pop.
            self._pending_exit = True
            self.action_cancel_download()
        else:
            self.app.pop_screen()

    def action_start_download(self) -> None:
        if self._is_busy or not self._profile:
            return
        self._run_download()

    def action_start_update(self) -> None:
        if self._is_busy or not self._profile:
            return
        self._run_download(update_only=True)

    def action_start_rescan(self) -> None:
        """Escanea desde el inicio y puebla la cola — no descarga nada."""
        if self._is_busy or not self._profile:
            return
        self._run_download(force_full=True, scan_only=True)

    def action_cancel_download(self) -> None:
        self.workers.cancel_group(self, "download")

    def action_verify(self) -> None:
        if self._is_busy or not self._profile:
            return
        self._run_verify()

    # ── Helpers de UI ─────────────────────────────────────────────────────

    def _log(self, text: str) -> None:
        self.query_one("#activity-log", RichLog).write(text)

    def _set_semaphore(self, state: str) -> None:
        _STATES = {
            "idle":      ("●", "status-idle",    "Listo"),
            "running":   ("●", "status-running", "Corriendo…"),
            "done":      ("●", "status-done",    "Completado"),
            "error":     ("●", "status-error",   "Error"),
            "cancelled": ("●", "status-cancel",  "Cancelado"),
        }
        icon, cls, tip = _STATES.get(state, _STATES["idle"])
        sem = self.query_one("#semaphore", Label)
        sem.update(f"{icon} {tip}")
        sem.set_classes(cls)

    def _update_counters(self, dl: int, sk: int, err: int, def_: int) -> None:
        # Progreso de descarga: "X de N" si hay total conocido, solo "X" si no
        total = self._batch_total
        if total > 0:
            done = self._batch_offset + dl
            dl_str = f"↓ {done} / {total}"
        else:
            dl_str = f"↓ {dl}"
        self.query_one("#counters-label", Static).update(
            f"{dl_str}  skip {sk}  ✗ {err}  ⏭ {def_}"
        )

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        for btn_id in (
            "btn-download", "btn-update", "btn-rescan", "btn-verify", "btn-prescan",
            "btn-dedup", "btn-compact",
            "btn-add-url", "btn-del-url",
        ):
            self.query_one(f"#{btn_id}", Button).disabled = busy
        self.query_one("#btn-cancel", Button).disabled = not busy
        if busy:
            self._set_semaphore("running")
        elif self._pending_exit:
            # El usuario pidió volver mientras los workers corrían.
            # Ahora que terminaron, es seguro hacer el pop.
            self._pending_exit = False
            self.app.pop_screen()

    # ── Guardado de ext_filter ────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "workers-input" and self._profile:
            try:
                n = int(event.value)
                if 1 <= n <= 20:
                    self._init_worker_panel(n)
            except ValueError:
                pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "artist-ext-custom" and self._profile:
            self._save_artist_filter()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("artist-chk-") and self._profile:
            self._save_artist_filter()

    def _save_artist_filter(self) -> None:
        """Serializa la selección de checkboxes + custom y guarda en la BD."""
        if not self._profile:
            return
        selected_groups: list[str] = []
        for _gid in EXT_GROUPS:
            try:
                if self.query_one(f"#artist-chk-{_gid}", Checkbox).value:
                    selected_groups.append(_gid)
            except Exception:
                pass
        try:
            custom_val = self.query_one("#artist-ext-custom", Input).value.strip()
        except Exception:
            custom_val = ""
        encoded = _encode_profile_filter(selected_groups, custom_val)
        self.run_worker(
            update_profile_ext_filter(INDEX_DB, self._profile["id"], encoded),
            exclusive=False,
        )

    # ── Download ──────────────────────────────────────────────────────────

    @work(exclusive=True, group="download")
    async def _run_download(
        self, update_only: bool = False, force_full: bool = False,
        scan_only: bool = False,
    ) -> None:
        self._set_busy(True)
        self.query_one("#activity-log", RichLog).clear()
        try:
            await self._do_download(
                update_only=update_only, force_full=force_full, scan_only=scan_only
            )
        except asyncio.CancelledError:
            self._log("[yellow]Descarga cancelada por el usuario.[/]")
            self._set_semaphore("cancelled")
        except Exception as exc:
            self._log(f"[red]✗ Error: {exc}[/]")
            self._set_semaphore("error")
        finally:
            self._set_busy(False)

    async def _do_download(
        self, update_only: bool = False, force_full: bool = False,
        scan_only: bool = False,
    ) -> None:
        from datetime import datetime

        from ..engine import DownloadEngine, ErrorKind
        from ..gui.bridge import (
            _build_local_hash_map,
            _parse_ext_filter,
            _passes_ext_filter,
            build_filename,
        )
        from ..index import (
            get_or_create_artist,
            get_or_create_site,
            update_profile_url_sync,
        )
        from ..auth.patreon import NeedsManualAuth
        from ..auth.pixiv import NeedsPixivAuth
        from ..templates._registry import find_template, get_template
        from ..templates.base import parse_date_utc

        profile = self._profile
        if not profile:
            return

        config  = load_config()
        try:
            workers = int(self.query_one("#workers-input", Input).value or "3")
        except ValueError:
            workers = 3
        # Leer ext_filter desde los checkboxes de grupos + campo custom
        ext_filter: set[str] = set()
        for _gid, (_lbl, _exts) in EXT_GROUPS.items():
            try:
                if self.query_one(f"#artist-chk-{_gid}", Checkbox).value:
                    ext_filter.update("." + e for e in _exts)
            except Exception:
                pass
        try:
            _custom_val = self.query_one("#artist-ext-custom", Input).value.strip()
            if _custom_val:
                ext_filter.update(_parse_ext_filter(_custom_val))
        except Exception:
            pass

        # Respetar max_workers del template más restrictivo en el perfil
        for pu in profile.get("urls", []):
            if pu.get("enabled") and pu.get("url"):
                cls = find_template(pu["url"])
                if cls and getattr(cls, "max_workers", None):
                    workers = min(workers, cls.max_workers)

        # Reinicializar panel de workers con el número correcto
        self._init_worker_panel(workers)

        downloaded_ref     = [0]
        skipped_ref        = [0]
        errors_ref         = [0]
        deferred_count_ref = [0]
        folder = Path(profile["folder_path"])
        deferred: list[tuple] = []

        async with DownloadEngine(config, workers=workers) as engine:
            for pu in profile["urls"]:
                if not pu["enabled"] or not pu["url"]:
                    continue

                template = get_template(pu["url"], engine)
                if not template:
                    self._log(f"[red]✗ Sin template para: {pu['url']}[/]")
                    continue

                try:
                    artist_info = await template.get_artist_info(pu["url"])
                except NeedsManualAuth:
                    self._log("[yellow]⚠ Patreon requiere autenticación[/]")
                    ok = await self.app.push_screen_wait(PatreonAuthModal())
                    if not ok:
                        self._log("[red]✗ Autenticación cancelada[/]")
                        continue
                    try:
                        artist_info = await template.get_artist_info(
                            pu["url"]
                        )
                    except Exception as exc:
                        self._log(f"[red]✗ Error tras auth: {exc}[/]")
                        continue
                except NeedsPixivAuth:
                    self._log("[yellow]⚠ Pixiv requiere autenticación[/]")
                    ok = await self.app.push_screen_wait(PixivAuthModal())
                    if not ok:
                        self._log("[red]✗ Autenticación cancelada[/]")
                        continue
                    try:
                        artist_info = await template.get_artist_info(
                            pu["url"]
                        )
                    except Exception as exc:
                        self._log(f"[red]✗ Error tras auth Pixiv: {exc}[/]")
                        continue
                self._log(f"[bold]▶ {artist_info.name} ({pu['site']})[/]")

                folder.mkdir(parents=True, exist_ok=True)
                await init_catalog(folder)
                await init_index(INDEX_DB)
                site_id = await get_or_create_site(INDEX_DB, artist_info.site)
                await get_or_create_artist(
                    db_path=INDEX_DB,
                    site_id=site_id,
                    artist_id=artist_info.artist_id,
                    name=artist_info.name,
                    folder_path=folder,
                )
                await update_profile_url_sync(
                    INDEX_DB, pu["id"], artist_id=artist_info.artist_id
                )

                # Calcular fecha de corte.
                # Si last_synced existe Y no es un Rescan forzado, se usa como
                # frontera en ambos modos (Descargar + Actualizar). Esto evita
                # re-escanear cientos de páginas de API para perfiles que ya
                # tienen una sincronización previa. "↺ Rescan" usa force_full=True
                # para ignorar la frontera y escanear desde el inicio.
                url_since: datetime | None = None
                if not force_full and pu.get("last_synced"):
                    url_since = parse_date_utc(pu["last_synced"])
                    if url_since:
                        label = "↑" if update_only else "⟳"
                        self._log(
                            f"  [dim]{label} Sync desde {pu['last_synced'][:16]}[/]"
                        )
                elif force_full:
                    self._log("  [dim]↺ Rescan completo desde el inicio…[/]")

                dl_before    = downloaded_ref[0]
                local_hashes = await _build_local_hash_map(folder)

                # ── Fase 1: scan o retomar pendientes ─────────────────────
                import json as _json
                import zlib as _zlib
                pu_id: int | None = pu.get("id")

                # Limpiar entradas que ya están en el catálogo (descargadas
                # en sesiones anteriores pero no removidas de la cola).
                _cleaned = await clean_pending_catalog_overlap(folder, pu_id)
                if _cleaned:
                    self._log(f"  [dim]⊘ {_cleaned} entrada(s) ya descargadas eliminadas de la cola[/]")

                existing_pending = await pending_count(folder, pu_id)

                # Detectar si el filtro cambió desde el último scan.
                _filter_key  = f"scan_filter_{pu_id}"
                _filter_sig  = _zlib.crc32(
                    ",".join(sorted(ext_filter)).encode()
                ) & 0x7FFFFFFF
                _stored_sig  = await get_meta_int(folder, _filter_key)
                _filter_changed = (_stored_sig != _filter_sig)

                # Contar cuántos pendientes coinciden con el filtro actual.
                # Si hay pendientes pero ninguno coincide → también hay que escanear
                # (puede haber archivos del tipo nuevo que nunca se añadieron a la cola).
                _matching_pending = await pending_count(
                    folder, pu_id, ext_filter=ext_filter or None
                )

                new_this_scan = 0

                if existing_pending > 0 and not _filter_changed and _matching_pending > 0:
                    self._log(
                        f"  [cyan]↺ Retomando — "
                        f"{_matching_pending} archivo(s) pendiente(s)[/]"
                    )
                else:
                    # Scan de la API: poblar pending_queue antes de descargar.
                    # Cuando el filtro cambió, se ignora url_since aunque exista:
                    # los nuevos tipos de archivo pueden estar en posts anteriores
                    # que ya pasaron la frontera de last_synced.
                    if _filter_changed:
                        _scan_since = None  # scan completo al cambiar filtro
                        _label = "Filtro cambiado — escaneo completo para nuevos tipos…"
                        self._log(f"  [dim]{_label}[/]")
                    else:
                        _scan_since = url_since
                        self._log(f"  [dim]Escaneando posts…[/]")
                    seen_scan: set[str] = set()
                    _scan_files_seen = 0
                    try:
                        async for fi in template.iter_files(
                            artist_info, since=_scan_since
                        ):
                            _scan_files_seen += 1
                            key = fi.dedup_key
                            if key in seen_scan:
                                continue
                            seen_scan.add(key)
                            # Saltar si ya está en el catálogo de archivos
                            if await url_exists(folder, key):
                                continue
                            if fi.remote_hash and await hash_exists(
                                folder, fi.remote_hash
                            ):
                                continue
                            # Agregar a cola persistente si no está ya
                            # (el filtro se aplica en el producer, no aquí,
                            #  para que la cola refleje TODO lo pendiente)
                            if not await pending_url_exists(folder, key):
                                await add_pending(
                                    folder,
                                    url_source=key,
                                    download_url=fi.url,
                                    filename_hint=fi.filename,
                                    post_id=fi.post_id,
                                    post_published=fi.date_published,
                                    remote_hash=fi.remote_hash,
                                    extra_headers=(
                                        _json.dumps(fi.extra_headers)
                                        if fi.extra_headers else None
                                    ),
                                    profile_url_id=pu_id,
                                )
                                new_this_scan += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as _scan_exc:
                        import traceback as _tb2
                        self._log(
                            f"\n[bold red]✗ Error en escaneo "
                            f"({type(_scan_exc).__name__}): {_scan_exc}[/]"
                        )
                        self._log(
                            f"  [dim]{_tb2.format_exc().splitlines()[-1]}[/]"
                        )
                        self._log(
                            "[yellow]⚠ Scan parcial — usa Sync para continuar.[/]"
                        )

                    # Guardar fingerprint solo si la API devolvió archivos.
                    # Si devolvió 0 puede ser un bloqueo transitorio (DDoS-Guard,
                    # sesión expirada…) — no marcar como "escaneado" para que el
                    # siguiente intento vuelva a escanear completo.
                    if _scan_files_seen > 0:
                        await set_meta_int(folder, _filter_key, _filter_sig)
                    self._log(f"  [dim]Scan: {new_this_scan} nuevo(s) en cola[/]")

                    # Enfriamiento post-scan: si el scan fue agresivo (muchas
                    # páginas de API), esperar antes de iniciar las descargas
                    # para que las protecciones del servidor se reseteen.
                    _threshold = getattr(template, "cooldown_threshold", 200)
                    _cooldown  = getattr(template, "cooldown_seconds", 10.0)
                    if new_this_scan >= _threshold:
                        self._log(
                            f"  [yellow]⏸ Enfriamiento {_cooldown:.0f}s "
                            f"antes de iniciar descargas…[/]"
                        )
                        await asyncio.sleep(_cooldown)

                # Modo scan-only: no descargar — solo dejar la cola lista
                if scan_only:
                    count = await pending_count(folder, pu_id)
                    if count:
                        self._log(f"  [cyan]⏳ {count} archivo(s) en cola[/]")
                    else:
                        self._log("  [green]✓ Ya está al día[/]")
                    continue

                # ── Fase 2: descargar desde la cola persistida ────────────
                pending_list = await get_pending_files(folder, pu_id)
                if not pending_list:
                    self._log("  [green]✓ Sin archivos nuevos[/]")
                    self._batch_total  = 0
                    self._batch_offset = 0
                    await update_profile_url_sync(
                        INDEX_DB, pu["id"],
                        file_count=pu.get("file_count") or 0,
                    )
                    continue

                # Calcular progreso total "X / N" para el contador de UI.
                # Batch nuevo → guardar total en meta para que futuros resumes
                # sepan cuántos archivos había originalmente.
                # Resume → leer el total guardado y calcular el offset.
                _batch_key = f"pending_batch_{pu_id}"
                if existing_pending == 0:
                    _batch_total  = len(pending_list)
                    _batch_offset = 0
                    await set_meta_int(folder, _batch_key, _batch_total)
                else:
                    _stored = await get_meta_int(folder, _batch_key)
                    if _stored and _stored >= len(pending_list):
                        _batch_total  = _stored
                        _batch_offset = _stored - len(pending_list)
                    else:
                        _batch_total  = len(pending_list)
                        _batch_offset = 0

                self._batch_total  = _batch_total
                self._batch_offset = _batch_offset

                _resume_note = (
                    f"  — retomando desde [cyan]{_batch_offset} / {_batch_total}[/]"
                    if _batch_offset > 0 else ""
                )
                self._log(
                    f"  Descargando [bold]{len(pending_list)}[/] archivo(s)"
                    f"{_resume_note}"
                )

                file_queue: asyncio.Queue = asyncio.Queue(maxsize=workers * 3)

                async def producer() -> None:
                    """Alimenta la cola de workers desde la lista de pendientes.
                    Aplica ext_filter aquí: los archivos que no coinciden se
                    dejan en pending_queue para sesiones futuras con otro filtro."""
                    from ..templates.base import FileInfo as _FI
                    try:
                        for _pf in pending_list:
                            # Filtro de extensiones: saltar sin eliminar de la cola
                            if ext_filter and not _passes_ext_filter(
                                _pf.get("filename_hint") or "", ext_filter, not ext_filter
                            ):
                                continue
                            _extra: dict = {}
                            if _pf.get("extra_headers"):
                                try:
                                    _extra = _json.loads(_pf["extra_headers"])
                                except Exception:
                                    pass
                            _fi = _FI(
                                url=_pf["download_url"],
                                url_source=_pf["url_source"],
                                filename=_pf["filename_hint"],
                                artist_id=artist_info.artist_id,
                                artist_name=artist_info.name,
                                post_id=_pf.get("post_id") or "",
                                date_published=_pf.get("post_published") or "",
                                remote_hash=_pf.get("remote_hash") or "",
                                extra_headers=_extra,
                            )
                            await asyncio.wait_for(
                                file_queue.put(_fi), timeout=120.0
                            )
                    except asyncio.CancelledError:
                        raise
                    finally:
                        for _ in range(workers):
                            try:
                                await file_queue.put(None)
                            except RuntimeError:
                                break

                in_progress_hashes: set[str] = set()

                async def worker_task(slot_id: int) -> None:
                    while True:
                        fi = await file_queue.get()
                        if fi is None:
                            if slot_id < len(self._worker_rows):
                                self._worker_rows[slot_id].idle()
                            break

                        if fi.remote_hash and fi.remote_hash in in_progress_hashes:
                            skipped_ref[0] += 1
                            self._log(f"  [dim]— {fi.filename[:60]}  [hash en progreso][/]")
                            self._update_counters(
                                downloaded_ref[0], skipped_ref[0],
                                errors_ref[0], deferred_count_ref[0],
                            )
                            continue

                        if fi.remote_hash:
                            in_progress_hashes.add(fi.remote_hash)

                        try:
                            if await url_exists(folder, fi.dedup_key):
                                skipped_ref[0] += 1
                                self._log(f"  [dim]— {fi.filename[:60]}  [URL en catálogo][/]")
                                await remove_pending(folder, fi.dedup_key)
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            if fi.remote_hash and await hash_exists(folder, fi.remote_hash):
                                skipped_ref[0] += 1
                                self._log(f"  [dim]— {fi.filename[:60]}  [hash en catálogo][/]")
                                await remove_pending(folder, fi.dedup_key)
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            counter    = await next_counter(folder)
                            final_name = build_filename(artist_info.name, counter, fi.filename)

                            if slot_id < len(self._worker_rows):
                                self._worker_rows[slot_id].start(fi.filename)

                            def make_cb(s: int) -> Callable[[int, int], None]:
                                def cb(done: int, total: int) -> None:
                                    if s < len(self._worker_rows):
                                        self._worker_rows[s].progress(done, total)
                                return cb

                            # El engine maneja su propio timeout total (570s) y
                            # retorna DownloadResult(TIMEOUT) de forma limpia.
                            # asyncio.wait_for se mantiene en 660s solo como
                            # safety net de último recurso — en Python 3.12
                            # wait_for puede propagar CancelledError en lugar de
                            # TimeoutError cuando httpx re-crea excepciones al
                            # hacer cleanup de streams HTTP/2.
                            try:
                                result = await asyncio.wait_for(
                                    engine.download(
                                        url=fi.url,
                                        dest_dir=folder,
                                        filename=final_name,
                                        on_progress=make_cb(slot_id),
                                        extra_headers=fi.extra_headers or None,
                                        total_timeout=300.0,
                                    ),
                                    timeout=7200.0,
                                )
                            except asyncio.TimeoutError:
                                if slot_id < len(self._worker_rows):
                                    self._worker_rows[slot_id].done(fi.filename, "⏸")
                                self._log(f"  [yellow]⏸ {fi.filename[:55]}  [timeout total — diferido][/]")
                                deferred.append((fi, artist_info, folder))
                                deferred_count_ref[0] += 1
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            if not result.ok:
                                if result.error_kind in ErrorKind.DEFERRABLE:
                                    deferred.append((fi, artist_info, folder))
                                    if slot_id < len(self._worker_rows):
                                        self._worker_rows[slot_id].done(fi.filename, "⏸")
                                    self._log(
                                        f"  [yellow]⏸ {fi.filename[:55]}  [{result.error_kind}][/]"
                                    )
                                else:
                                    errors_ref[0] += 1
                                    if slot_id < len(self._worker_rows):
                                        self._worker_rows[slot_id].done(fi.filename, "✗")
                                    self._log(f"  [red]✗ {fi.filename[:45]}:  {result.error}[/]")
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            if result.file_hash is None:
                                errors_ref[0] += 1
                                self._log(f"  [red]✗ {fi.filename[:50]}: bug interno — hash nulo[/]")
                                if result.dest and result.dest.exists():
                                    result.dest.unlink()
                                continue

                            # Dedup post-descarga: mismo contenido ya catalogado
                            # (ej. archivo de Patreon que ya existía vía Kemono)
                            if await hash_exists(folder, result.file_hash):
                                skipped_ref[0] += 1
                                if result.dest and result.dest.exists():
                                    result.dest.unlink()
                                if slot_id < len(self._worker_rows):
                                    self._worker_rows[slot_id].idle()
                                self._log(
                                    f"  [dim]≡ {fi.filename[:60]}  [duplicado — hash ya catalogado][/]"
                                )
                                await remove_pending(folder, fi.dedup_key)
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            # Catalogar
                            if result.file_hash in local_hashes:
                                old_path = local_hashes[result.file_hash]
                                new_path = folder / final_name
                                try:
                                    old_path.replace(new_path)
                                    local_hashes[result.file_hash] = new_path
                                    if result.dest and result.dest.exists():
                                        result.dest.unlink()
                                    renamed = True
                                except OSError:
                                    try:
                                        old_path.unlink()
                                    except OSError:
                                        pass
                                    local_hashes[result.file_hash] = result.dest
                                    renamed = False
                                await add_file(
                                    artist_dir=folder,
                                    file_hash=result.file_hash,
                                    filename=final_name,
                                    url_source=fi.dedup_key,
                                    file_size=result.file_size,
                                    counter=counter,
                                )
                                await remove_pending(folder, fi.dedup_key)
                                downloaded_ref[0] += 1
                                if slot_id < len(self._worker_rows):
                                    self._worker_rows[slot_id].done(
                                        final_name, "↷" if renamed else "✓"
                                    )
                                icon = "↷" if renamed else "✓"
                                self._log(
                                    f"  [green]{icon} {fi.filename[:40]}  →  {final_name}[/]"
                                    + ("  [renombrado]" if renamed else "")
                                )
                            else:
                                await add_file(
                                    artist_dir=folder,
                                    file_hash=result.file_hash,
                                    filename=final_name,
                                    url_source=fi.dedup_key,
                                    file_size=result.file_size,
                                    counter=counter,
                                )
                                await remove_pending(folder, fi.dedup_key)
                                local_hashes[result.file_hash] = result.dest
                                downloaded_ref[0] += 1
                                if slot_id < len(self._worker_rows):
                                    self._worker_rows[slot_id].done(final_name, "✓")
                                self._log(
                                    f"  [green]✓ {fi.filename[:40]}  →  {final_name}[/]"
                                )

                            self._update_counters(
                                downloaded_ref[0], skipped_ref[0],
                                errors_ref[0], deferred_count_ref[0],
                            )

                        except asyncio.CancelledError:
                            raise
                        except Exception as _exc:
                            import traceback as _tb
                            errors_ref[0] += 1
                            if slot_id < len(self._worker_rows):
                                self._worker_rows[slot_id].done(fi.filename, "✗")
                            self._log(
                                f"  [red]✗ {fi.filename[:45]}  [{type(_exc).__name__}: {_exc}][/]"
                            )
                            self._log(f"    [dim]{_tb.format_exc().splitlines()[-1]}[/]")
                            self._update_counters(
                                downloaded_ref[0], skipped_ref[0],
                                errors_ref[0], deferred_count_ref[0],
                            )
                        finally:
                            if fi.remote_hash:
                                in_progress_hashes.discard(fi.remote_hash)

                _all_tasks = [
                    asyncio.create_task(producer(), name="producer"),
                    *[
                        asyncio.create_task(worker_task(i), name=f"worker-{i}")
                        for i in range(workers)
                    ],
                ]
                try:
                    _results = await asyncio.gather(
                        *_all_tasks, return_exceptions=True
                    )
                except asyncio.CancelledError:
                    for t in _all_tasks:
                        t.cancel()
                    await asyncio.gather(*_all_tasks, return_exceptions=True)
                    raise

                # El primer resultado es el producer. Si murió con excepción
                # (error de red, timeout de paginación, etc.) la reportamos
                # explícitamente — de lo contrario el proceso parece "completo"
                # pero faltan archivos sin mostrar ningún error.
                _producer_exc = _results[0]
                if isinstance(_producer_exc, Exception):
                    import traceback as _tb
                    self._log(
                        f"\n[bold red]✗ Error en paginación "
                        f"({type(_producer_exc).__name__}): "
                        f"{_producer_exc}[/]"
                    )
                    self._log(
                        "[yellow]⚠ La descarga quedó incompleta. "
                        "Usa Sync de nuevo para continuar.[/]"
                    )

                # Actualizar conteo de fuente
                source_dl = downloaded_ref[0] - dl_before
                new_count = (pu["file_count"] or 0) + source_dl
                await update_profile_url_sync(INDEX_DB, pu["id"], file_count=new_count)

            # Cola diferida — reintentar archivos que agotaron el timeout inicial.
            # Usa el primer worker slot como indicador visual y aplica el mismo
            # timeout de 600s para evitar que el TUI se congele indefinidamente.
            if deferred:
                self._log(f"\n[yellow]⏭ Cola diferida: {len(deferred)} archivo(s)…[/]")
                _slot0 = self._worker_rows[0] if self._worker_rows else None
                import hashlib as _hl
                for file_info, a_info, dest_folder in deferred:
                    if await url_exists(dest_folder, file_info.dedup_key):
                        skipped_ref[0] += 1
                        continue
                    if _slot0:
                        _slot0.start(file_info.filename)
                    # Nombre temporal único — el contador se asigna solo si la
                    # descarga tiene éxito, evitando huecos en la numeración.
                    _ext  = Path(file_info.filename).suffix
                    _tmp_name = (
                        "_dl_" + _hl.md5(file_info.url.encode()).hexdigest()[:12] + _ext
                    )
                    try:
                        result = await asyncio.wait_for(
                            engine.download(
                                url=file_info.url,
                                dest_dir=dest_folder,
                                filename=_tmp_name,
                                extra_headers=file_info.extra_headers or None,
                                total_timeout=300.0,
                            ),
                            timeout=7200.0,
                        )
                    except asyncio.TimeoutError:
                        if _slot0:
                            _slot0.done(file_info.filename, "⏭")
                        deferred_count_ref[0] += 1
                        self._log(
                            f"  [yellow]⏭ {file_info.filename[:55]}  "
                            f"[timeout — pendiente próx. sync][/]"
                        )
                        self._update_counters(
                            downloaded_ref[0], skipped_ref[0],
                            errors_ref[0], deferred_count_ref[0],
                        )
                        continue
                    if result.ok and result.file_hash:
                        # Asignar contador post-éxito y renombrar el archivo temporal
                        counter    = await next_counter(dest_folder)
                        final_name = build_filename(a_info.name, counter, file_info.filename)
                        if result.dest and result.dest.exists():
                            try:
                                result.dest.replace(dest_folder / final_name)
                            except OSError:
                                final_name = _tmp_name   # fallback
                        await add_file(
                            artist_dir=dest_folder,
                            file_hash=result.file_hash,
                            filename=final_name,
                            url_source=file_info.dedup_key,
                            file_size=result.file_size,
                            counter=counter,
                        )
                        await remove_pending(dest_folder, file_info.dedup_key)
                        downloaded_ref[0] += 1
                        if _slot0:
                            _slot0.done(final_name, "✓")
                        self._log(f"  [green]✓ {final_name} (reintento)[/]")
                    else:
                        # Limpiar el archivo temporal si quedó en disco
                        if result.dest and result.dest.exists():
                            result.dest.unlink(missing_ok=True)
                        if _slot0:
                            _slot0.done(file_info.filename, "⏭")
                        deferred_count_ref[0] += 1
                        self._log(f"  [yellow]⏭ {file_info.filename[:55]}  — pendiente próx. sync[/]")
                    self._update_counters(
                        downloaded_ref[0], skipped_ref[0],
                        errors_ref[0], deferred_count_ref[0],
                    )
                if _slot0:
                    _slot0.idle()

        # Resumen final
        await update_profile_last_checked(INDEX_DB, profile["id"])
        if scan_only:
            total_q = await pending_count(Path(profile["folder_path"]))
            if total_q:
                self._log(f"\n[bold cyan]Escaneo completo — ⏳ {total_q} archivo(s) en cola[/]")
                self._set_semaphore("cancelled")   # amarillo: hay pendientes
            else:
                self._log("\n[bold green]Escaneo completo — ✓ Todo al día[/]")
                self._set_semaphore("done")
            await self._load_profile()
            return

        dl  = downloaded_ref[0]
        sk  = skipped_ref[0]
        err = errors_ref[0]
        def_ = deferred_count_ref[0]
        summary = f"Completado — ↓ {dl} nuevos  skip {sk}"
        if err:
            summary += f"  ✗ {err} errores"
        if def_:
            summary += f"  ⏭ {def_} para próxima sync"
        self._log(f"\n[bold green]{summary}[/]")
        if err or def_:
            self._set_semaphore("cancelled")   # amarillo: pendientes/errores
        else:
            self._set_semaphore("done")        # azul: todo completado
        await self._load_profile()

    # ── Verificar ─────────────────────────────────────────────────────────

    @work(exclusive=True, group="download")
    async def _run_verify(self) -> None:
        self._set_busy(True)
        self.query_one("#activity-log", RichLog).clear()
        try:
            await self._do_verify()
        except asyncio.CancelledError:
            self._log("[yellow]Verificación cancelada.[/]")
            self._set_semaphore("cancelled")
        except Exception as exc:
            self._log(f"[red]✗ Error: {exc}[/]")
            self._set_semaphore("error")
        finally:
            self._set_busy(False)

    async def _do_verify(self) -> None:
        from ..engine import DownloadEngine
        from ..templates._registry import get_template

        profile = self._profile
        if not profile:
            return
        config = load_config()
        folder = Path(profile["folder_path"])
        await init_catalog(folder)
        total_new = 0

        from ..auth.patreon import NeedsManualAuth
        from ..auth.pixiv   import NeedsPixivAuth

        async with DownloadEngine(config) as engine:
            for pu in profile["urls"]:
                if not pu["enabled"] or not pu["url"]:
                    continue
                template = get_template(pu["url"], engine)
                if not template:
                    self._log(f"[red]Sin template para {pu['url']}[/]")
                    continue
                try:
                    artist_info = await template.get_artist_info(pu["url"])
                except NeedsManualAuth:
                    self._log("[yellow]⚠ Patreon requiere autenticación[/]")
                    ok = await self.app.push_screen_wait(PatreonAuthModal())
                    if not ok:
                        self._log("[red]✗ Autenticación cancelada — fuente omitida[/]")
                        continue
                    try:
                        artist_info = await template.get_artist_info(pu["url"])
                    except Exception as exc:
                        self._log(f"[red]✗ Error tras auth: {exc}[/]")
                        continue
                except NeedsPixivAuth:
                    self._log("[yellow]⚠ Pixiv requiere autenticación[/]")
                    ok = await self.app.push_screen_wait(PixivAuthModal())
                    if not ok:
                        self._log("[red]✗ Autenticación cancelada — fuente omitida[/]")
                        continue
                    try:
                        artist_info = await template.get_artist_info(pu["url"])
                    except Exception as exc:
                        self._log(f"[red]✗ Error tras auth Pixiv: {exc}[/]")
                        continue
                self._log(f"[bold]⟳ {artist_info.name} ({pu['site']})…[/]")
                count_new = 0
                async for file_info in template.iter_files(artist_info):
                    if file_info.remote_hash and await hash_exists(folder, file_info.remote_hash):
                        continue
                    if await url_exists(folder, file_info.dedup_key):
                        continue
                    count_new += 1
                    self._update_counters(total_new + count_new, 0, 0, 0)
                self._log(f"  → [cyan]{count_new}[/] archivos nuevos")
                total_new += count_new

        await update_profile_last_checked(INDEX_DB, profile["id"])
        msg = (
            f"[bold green]Verificación completa — {total_new} archivos nuevos[/]"
            if total_new else
            "[bold green]Todo al día — sin archivos nuevos[/]"
        )
        self._log(f"\n{msg}")
        self._set_semaphore("done")
        await self._load_profile()

    # ── Deduplicar ────────────────────────────────────────────────────────

    @work(exclusive=True, group="download")
    async def _start_dedup(self) -> None:
        self._set_busy(True)
        self.query_one("#activity-log", RichLog).clear()
        try:
            await self._do_dedup()
        except asyncio.CancelledError:
            self._log("[yellow]Deduplicación cancelada.[/]")
            self._set_semaphore("cancelled")
        except Exception as exc:
            self._log(f"[red]✗ Error: {exc}[/]")
            self._set_semaphore("error")
        finally:
            self._set_busy(False)

    async def _do_dedup(self) -> None:
        from ..catalog import get_all_files
        from ..hasher import sha256_file

        profile = self._profile
        if not profile:
            return
        folder = Path(profile["folder_path"])
        if not folder.exists():
            self._log("[red]Carpeta del artista no encontrada.[/]")
            return

        catalog_rows = await get_all_files(folder)
        catalog_map: dict[str, str] = {r["hash"]: r["filename"] for r in catalog_rows}
        self._log(f"Catálogo: [cyan]{len(catalog_map)}[/] entradas")

        all_files = [p for p in folder.iterdir() if p.is_file() and p.name != "catalog.db"]
        self._log(f"Archivos en disco: [cyan]{len(all_files)}[/]")

        def _scan() -> dict[str, list[Path]]:
            groups: dict[str, list[Path]] = {}
            for p in all_files:
                try:
                    h = sha256_file(p)
                    groups.setdefault(h, []).append(p)
                except OSError:
                    pass
            return groups

        groups = await asyncio.to_thread(_scan)
        removed = 0
        freed   = 0
        for file_hash, paths in groups.items():
            if len(paths) < 2:
                continue
            canonical = catalog_map.get(file_hash)
            keep = next((p for p in paths if p.name == canonical), paths[0]) if canonical else paths[0]
            for p in paths:
                if p == keep:
                    continue
                try:
                    size = p.stat().st_size
                    p.unlink()
                    freed += size
                    removed += 1
                    self._log(f"  [red]✗[/] {p.name}  →  mantiene [green]{keep.name}[/]")
                except OSError as exc:
                    self._log(f"  [yellow]⚠ No se pudo borrar {p.name}: {exc}[/]")

        if removed:
            self._log(f"\n[bold green]Deduplicación completa — {removed} duplicado(s), {_fmt_size(freed)} liberados[/]")
        else:
            self._log("[bold green]Sin duplicados — la colección está limpia[/]")
        self._set_semaphore("done")
        await self._load_profile()

    # ── Compactar ─────────────────────────────────────────────────────────

    def _confirm_compact(self) -> None:
        """Abre el modal de doble confirmación antes de compactar."""
        profile = self._profile
        if not profile:
            return
        folder = Path(profile["folder_path"])

        async def _push() -> None:
            from ..catalog import get_numbered_files, plan_compaction
            files = await get_numbered_files(folder)
            plan  = plan_compaction(files)
            if not plan:
                self.app.notify(
                    "Numeración ya es continua — nada que compactar.",
                    severity="information",
                )
                return
            self.app.push_screen(
                CompactConfirmModal(len(files), len(plan)),
                callback=lambda ok: ok and self._start_compact(),
            )

        self.run_worker(_push(), exclusive=False)

    @work(exclusive=True, group="download")
    async def _start_compact(self) -> None:
        self._set_busy(True)
        self.query_one("#activity-log", RichLog).clear()
        try:
            await self._do_compact()
        except asyncio.CancelledError:
            self._log("[yellow]Compactación cancelada.[/]")
            self._set_semaphore("cancelled")
        except Exception as exc:
            self._log(f"[red]✗ Error: {exc}[/]")
            self._set_semaphore("error")
        finally:
            self._set_busy(False)

    async def _do_compact(self) -> None:
        from ..catalog import (
            get_numbered_files, plan_compaction, apply_compaction,
        )

        profile = self._profile
        if not profile:
            return
        folder = Path(profile["folder_path"])
        if not folder.exists():
            self._log("[red]Carpeta del artista no encontrada.[/]")
            return

        files = await get_numbered_files(folder)
        plan  = plan_compaction(files)
        if not plan:
            self._log("[bold green]Numeración ya es continua — nada que hacer[/]")
            self._set_semaphore("done")
            return

        self._log(
            f"Compactando: [cyan]{len(files)}[/] archivos, "
            f"[yellow]{len(plan)}[/] a renombrar…"
        )

        await apply_compaction(folder, plan, len(files))

        self._log(
            f"\n[bold green]Compactación completa — "
            f"{len(plan)} archivos renombrados[/]"
        )
        self._set_semaphore("done")
        await self._load_profile()

    # ── Pre-scan ──────────────────────────────────────────────────────────

    @work(exclusive=True, group="download")
    async def _start_prescan(self) -> None:
        prescan_str = self.query_one("#prescan-input", Input).value.strip()
        if not prescan_str:
            self.app.notify("Indica una carpeta en Pre-scan", severity="warning")
            return
        prescan_path = Path(prescan_str)
        if not prescan_path.is_dir():
            self.app.notify(f"Carpeta no encontrada: {prescan_path}", severity="error")
            return
        self._set_busy(True)
        self.query_one("#activity-log", RichLog).clear()
        try:
            await self._do_prescan(prescan_path)
        except asyncio.CancelledError:
            self._log("[yellow]Pre-scan cancelado.[/]")
            self._set_semaphore("cancelled")
        except Exception as exc:
            self._log(f"[red]✗ Error en pre-scan: {exc}[/]")
            self._set_semaphore("error")
        finally:
            self._set_busy(False)

    async def _do_prescan(self, prescan_path: Path) -> None:
        from ..engine import DownloadEngine
        from ..organizer import organize
        from ..templates._registry import get_template

        profile = self._profile
        if not profile:
            return
        folder = Path(profile["folder_path"])
        main_url = next(
            (pu for pu in profile["urls"] if pu["enabled"] and pu.get("artist_id")), None
        )
        if not main_url:
            url_entry = next(
                (pu for pu in profile["urls"] if pu["enabled"] and pu.get("url")), None
            )
            if not url_entry:
                self._log("[yellow]⚠ Pre-scan omitido — sin URL activa[/]")
                return
            self._log("  Pre-scan: resolviendo artista desde API…")
            async with DownloadEngine(load_config()) as engine:
                tmpl = get_template(url_entry["url"], engine)
                if not tmpl:
                    self._log("[yellow]⚠ Pre-scan omitido — sin template[/]")
                    return
                artist_info = await tmpl.get_artist_info(url_entry["url"])
            artist_id = artist_info.artist_id
            site      = url_entry["site"]
        else:
            artist_id = main_url["artist_id"]
            site      = main_url["site"]

        def on_progress(processed: int, total: int, filename: str) -> None:
            self._log(f"  Pre-scan [{processed}/{total}]: {filename[:40]}")

        folder.mkdir(parents=True, exist_ok=True)
        await init_catalog(folder)
        scan_result, _ = await organize(
            source_dir=prescan_path,
            artist_name=profile["display_name"],
            artist_id=artist_id,
            site=site,
            dest_root=load_config().download_path,
            progress_cb=on_progress,
        )
        self._log(f"  [green]Pre-scan: {scan_result.summary()}[/]")
        self._set_semaphore("done")


# ── ProfileFilterModal ───────────────────────────────────────────────────────

class ProfileFilterModal(ModalScreen):
    """
    Modal para configurar el filtro de tipos de archivo de un perfil específico.
    Se usa en el flujo de batch con 'usar configuración de cada perfil'.
    Devuelve el JSON encoded del filtro seleccionado, o None si se cancela.
    """

    def __init__(self, profile_name: str, current_filter: str = "", **kwargs):
        super().__init__(**kwargs)
        self._profile_name   = profile_name
        self._current_filter = current_filter

    def compose(self) -> ComposeResult:
        with Vertical(id="pfm-container"):
            yield Label(
                f"Configurar filtro para: [bold]{self._profile_name}[/]",
                id="pfm-title", markup=True,
            )
            yield Label("Tipos de archivo a descargar (vacío = todos):", classes="batch-filter-title")
            with Horizontal(id="pfm-groups"):
                for group_id, (label, _exts) in EXT_GROUPS.items():
                    yield Checkbox(label, value=False, id=f"pfm-chk-{group_id}")
            with Horizontal(id="pfm-custom-row"):
                yield Label("Ext. extra:", classes="batch-cfg-label")
                yield Input(
                    "", id="pfm-custom",
                    placeholder="psd,clip  (separadas por coma)",
                    classes="batch-cfg-ext",
                )
            with Horizontal(id="pfm-buttons"):
                yield Button("Aceptar", id="pfm-ok",     variant="success")
                yield Button("Omitir",  id="pfm-skip",   variant="default")
                yield Button("Cancelar", id="pfm-cancel", variant="error")

    def on_mount(self) -> None:
        # Restaurar selección actual del perfil
        if self._current_filter:
            _group_ids, _ = _decode_profile_filter(self._current_filter)
            for _gid in _group_ids:
                try:
                    self.query_one(f"#pfm-chk-{_gid}", Checkbox).value = True
                except Exception:
                    pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "pfm-ok":
                selected: list[str] = []
                for _gid in EXT_GROUPS:
                    try:
                        if self.query_one(f"#pfm-chk-{_gid}", Checkbox).value:
                            selected.append(_gid)
                    except Exception:
                        pass
                try:
                    custom = self.query_one("#pfm-custom", Input).value.strip()
                except Exception:
                    custom = ""
                self.dismiss(_encode_profile_filter(selected, custom))
            case "pfm-skip":
                # Omitir sin cambiar configuración → None significa "no configurado"
                self.dismiss(None)
            case "pfm-cancel":
                self.dismiss(False)  # False = cancelar todo el batch


# ── BatchScreen ─────────────────────────────────────────────────────────────

class BatchScreen(Screen):
    """
    Descarga por lotes — itera todos los perfiles hasta completarlos.

    Flujo:
      1. Para cada perfil: si pending_queue vacía → escanear API.
      2. Si después del scan sigue vacía → perfil al día, continuar.
      3. Descargar desde pending_queue, archivo por archivo.
         Si hay MAX_CONSECUTIVE errores seguidos → abandonar perfil (queda
         en "incompletos"), continuar con el siguiente.
      4. Al terminar la lista: reintentar los incompletos (nueva iteración).
      5. Parar cuando no queden incompletos O el usuario detenga el proceso.
    """

    BINDINGS = [Binding("escape", "go_back", "Volver", show=True)]

    _stop_requested: bool = False
    _skip_current:   bool = False
    _current_download_task: asyncio.Task | None = None

    # Errores consecutivos antes de abandonar un perfil y pasar al siguiente
    MAX_CONSECUTIVE: int = 5
    # Red de seguridad de último recurso — no interfiere con descargas grandes activas.
    HARD_TIMEOUT: float = 7200.0
    # Presupuesto de reintentos del engine (solo se descuenta entre intentos fallidos,
    # NUNCA durante una descarga activa con bytes fluyendo).
    # Con max_retries=1 el engine hace UN intento; si la conexión falla o hay stall
    # (45s sin datos), retorna error y el archivo queda en pending_queue para la
    # siguiente iteración del batch.
    # Si el archivo está descargando activamente, corre hasta completarse sin importar
    # cuánto tarde — el contador mostrará "activo" en lugar de "abandona en 0s".
    ENGINE_TIMEOUT: float = 300.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="batch-main"):
            # ── Sección de configuración (visible antes de iniciar) ──────────
            with Vertical(id="batch-config"):
                with Horizontal(id="batch-cfg-top"):
                    yield Label("Workers:", classes="batch-cfg-label")
                    yield Input("3", id="inp-batch-workers", classes="batch-cfg-input")
                    yield Button("▶ Iniciar", id="btn-batch-start", variant="success")

                with Horizontal(id="batch-filter-mode"):
                    yield Checkbox(
                        "Usar configuración de cada perfil",
                        value=False, id="chk-use-profile-filter",
                    )

                yield Label("Tipos de archivo a descargar (vacío = todos):", classes="batch-filter-title", id="batch-filter-title")
                with Horizontal(id="batch-filter-groups"):
                    for group_id, (label, _exts) in EXT_GROUPS.items():
                        yield Checkbox(label, value=False, id=f"chk-{group_id}")

                with Horizontal(id="batch-filter-custom"):
                    yield Label("Extensiones custom:", classes="batch-cfg-label")
                    yield Input(
                        "", id="inp-batch-custom",
                        placeholder="psd,clip,mp4  (separadas por coma)",
                        classes="batch-cfg-ext",
                    )

            yield Static("",  id="batch-stats")
            yield Static("",  id="batch-current")
            yield ProgressBar(id="batch-bar", total=100, show_eta=False)
            yield RichLog(
                id="batch-log", highlight=True, markup=True,
                wrap=True, max_lines=2000,
            )
        with Horizontal(id="batch-footer"):
            yield Button("⏹ Detener", id="btn-batch-stop", classes="-danger", disabled=True)
            yield Button("⏭ Saltar",  id="btn-batch-skip", disabled=True)
            yield Button("← Volver",  id="btn-batch-back")

    def on_mount(self) -> None:
        self._stop_requested = False
        # Pre-cargar workers desde config global
        try:
            cfg = load_config()
            self.query_one("#inp-batch-workers", Input).value = str(cfg.workers)
        except Exception:
            pass

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Toggle visibilidad de checkboxes de grupo cuando se activa 'usar perfil'."""
        if event.checkbox.id == "chk-use-profile-filter":
            use_profile = event.value
            for widget_id in ("#batch-filter-title", "#batch-filter-groups", "#batch-filter-custom"):
                try:
                    self.query_one(widget_id).display = not use_profile
                except Exception:
                    pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-batch-start":
                self._start_batch()
            case "btn-batch-stop":
                self._stop_requested = True
                self.workers.cancel_group(self, "batch")
                btn = self.query_one("#btn-batch-stop", Button)
                btn.disabled = True
                btn.label    = "Deteniendo…"
                # Saltar también cancela la descarga actual
                if self._current_download_task and not self._current_download_task.done():
                    self._current_download_task.cancel()
            case "btn-batch-skip":
                if self._current_download_task and not self._current_download_task.done():
                    self._skip_current = True
                    self._current_download_task.cancel()
                    try:
                        self.query_one("#btn-batch-skip", Button).disabled = True
                    except Exception:
                        pass
            case "btn-batch-back":
                self.app.pop_screen()

    def _start_batch(self) -> None:
        """Lee la config, oculta el panel de inicio y lanza el worker."""
        from ..gui.bridge import _parse_ext_filter

        try:
            batch_workers = max(1, int(
                self.query_one("#inp-batch-workers", Input).value or "3"
            ))
        except (ValueError, Exception):
            batch_workers = 3

        # ¿Modo "usar configuración de cada perfil"?
        use_profile_filter = False
        try:
            use_profile_filter = self.query_one("#chk-use-profile-filter", Checkbox).value
        except Exception:
            pass

        ext_filter   = set()
        exclude_mode = False

        if not use_profile_filter:
            # Recopilar extensiones de los grupos marcados
            for group_id, (_label, exts) in EXT_GROUPS.items():
                try:
                    if self.query_one(f"#chk-{group_id}", Checkbox).value:
                        ext_filter.update("." + ext for ext in exts)
                except Exception:
                    pass
            # Extensiones custom
            try:
                raw_custom = self.query_one("#inp-batch-custom", Input).value.strip()
            except Exception:
                raw_custom = ""
            ext_filter.update(_parse_ext_filter(raw_custom))

        # Ocultar config, activar botones Stop y Skip
        try:
            self.query_one("#batch-config").display = False
        except Exception:
            pass
        for btn_id in ("#btn-batch-stop", "#btn-batch-skip"):
            try:
                self.query_one(btn_id, Button).disabled = False
            except Exception:
                pass

        self._stop_requested = False

        if use_profile_filter:
            # Configurar perfiles sin filtro antes de arrancar
            self.run_worker(
                self._configure_and_start_batch(batch_workers),
                exclusive=True, group="batch",
                exit_on_error=False,
            )
        else:
            self.run_worker(
                self._do_batch(batch_workers, ext_filter, exclude_mode),
                exclusive=True, group="batch",
                exit_on_error=False,
            )

    def action_go_back(self) -> None:
        self.app.pop_screen()

    async def _configure_and_start_batch(self, batch_workers: int) -> None:
        """
        Pre-vuelo del modo 'usar configuración de cada perfil':
        1. Carga todos los perfiles.
        2. Para los que no tienen ext_filter configurado → muestra ProfileFilterModal.
        3. Guarda la configuración y arranca _do_batch con use_profile_filter=True.
        """
        _slim    = await list_profiles(INDEX_DB)
        profiles = []
        for _p in _slim:
            _full = await get_profile(INDEX_DB, _p["id"])
            if _full:
                profiles.append(_full)

        unconfigured = [
            p for p in profiles
            if not p.get("ext_filter")
            and any(u.get("enabled") and u.get("url") for u in p.get("urls", []))
        ]

        if unconfigured:
            self._log(
                f"[yellow]⚠ {len(unconfigured)} perfil(es) sin configuración de filtro.[/]\n"
                f"[dim]Configura cada uno antes de iniciar el batch.[/]"
            )

        for profile in unconfigured:
            if self._stop_requested:
                return
            result = await self.app.push_screen_wait(
                ProfileFilterModal(
                    profile_name   = profile["display_name"],
                    current_filter = profile.get("ext_filter", ""),
                )
            )
            if result is False:
                # Usuario canceló todo el batch
                self._log("[red]Batch cancelado por el usuario.[/]")
                try:
                    self.query_one("#btn-batch-stop", Button).disabled = True
                    self.query_one("#btn-batch-skip", Button).disabled = True
                    self.query_one("#batch-config").display = True
                except Exception:
                    pass
                return
            if result is None:
                # Omitir este perfil → no guardar nada, continuará con filtro vacío
                self._log(f"  [dim]⊘ {profile['display_name']} — omitido (descargará todo)[/]")
                continue
            # Guardar configuración
            await update_profile_ext_filter(INDEX_DB, profile["id"], result)
            self._log(f"  [green]✓ {profile['display_name']} — filtro configurado[/]")

        self._log("\n[bold]Iniciando batch con configuración de perfiles…[/]\n")
        await self._do_batch(batch_workers, use_profile_filter=True)

    # ── UI helpers ───────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        self.query_one("#batch-log", RichLog).write(msg)

    def _set_stats(
        self, iteration: int, done: int, pending_n: int, total: int
    ) -> None:
        self.query_one("#batch-stats", Static).update(
            f"[bold]Iteración {iteration}[/]  |  "
            f"[green]✓ {done} completados[/]  |  "
            f"[yellow]⏳ {pending_n} pendientes[/]  |  "
            f"[dim]{total} perfiles en total[/]"
        )

    def _set_current(self, name: str, step: str = "") -> None:
        suffix = f"  [dim]— {step}[/]" if step else ""
        self.query_one("#batch-current", Static).update(
            f"[bold cyan]▶[/] [bold]{name}[/]{suffix}"
        )

    def _set_progress(self, done: int, total: int) -> None:
        bar = self.query_one("#batch-bar", ProgressBar)
        bar.update(total=max(total, 1), progress=done)

    # ── Escaneo de una URL ───────────────────────────────────────────────────

    async def _scan_url(
        self, pu: dict, folder: Path, engine,
    ) -> int:
        """
        Escanea una URL de perfil y puebla pending_queue con TODOS los archivos
        pendientes (sin filtrar por extensión — el filtro se aplica en _download_url).
        Si ya hay pendientes (sesión interrumpida), los retoma sin re-escanear.
        Retorna el número de archivos en cola (nuevos o existentes).
        """
        import json as _json
        from ..auth.patreon        import NeedsManualAuth
        from ..auth.pixiv          import NeedsPixivAuth
        from ..templates._registry import get_template
        from ..templates.base      import parse_date_utc

        pu_id = pu.get("id")

        # Retomar sesión anterior sin re-escanear
        existing = await pending_count(folder, pu_id)
        if existing > 0:
            return existing

        template = get_template(pu["url"], engine)
        if not template:
            self._log(f"  [red]✗ Sin template para {pu['url'][:60]}[/]")
            return 0

        try:
            artist_info = await template.get_artist_info(pu["url"])
        except (NeedsManualAuth, NeedsPixivAuth):
            site = pu.get("site", "?")
            self._log(f"  [yellow]⚠ Auth requerida — {site} (omitido en batch)[/]")
            return 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"  [red]✗ Error resolviendo {pu['url'][:50]}: {exc}[/]")
            return 0

        url_since = None
        if pu.get("last_synced"):
            url_since = parse_date_utc(pu["last_synced"])

        count = 0
        seen: set[str] = set()
        try:
            async for fi in template.iter_files(artist_info, since=url_since):
                key = fi.dedup_key
                if key in seen:
                    continue
                seen.add(key)
                if await url_exists(folder, key):
                    continue
                if fi.remote_hash and await hash_exists(folder, fi.remote_hash):
                    continue
                # Sin filtro de extensiones: la cola siempre refleja TODO lo pendiente.
                # El filtro se aplica en _download_url al momento de descargar.
                if not await pending_url_exists(folder, key):
                    await add_pending(
                        folder,
                        url_source=key,
                        download_url=fi.url,
                        filename_hint=fi.filename,
                        post_id=fi.post_id,
                        post_published=fi.date_published,
                        remote_hash=fi.remote_hash,
                        extra_headers=(
                            _json.dumps(fi.extra_headers) if fi.extra_headers else None
                        ),
                        profile_url_id=pu_id,
                    )
                    count += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(
                f"  [red]✗ Error en scan ({type(exc).__name__}): {exc}[/]"
            )
        return count

    # ── Descarga de una URL ──────────────────────────────────────────────────

    async def _download_url(
        self,
        artist_name: str,
        pu: dict,
        folder: Path,
        engine,
        local_hashes: dict,
        progress_offset: int,
        progress_total: int,
        batch_workers: int = 1,  # reservado para uso futuro; descarga es secuencial
        ext_filter: set | None = None,
        exclude_mode: bool = False,
    ) -> tuple[int, int, int, int]:
        """
        Descarga archivos pendientes de una URL de perfil, uno a la vez.
        La ejecución secuencial garantiza que consecutive_errors acumule
        correctamente: solo se resetea cuando un archivo se descarga con éxito.
        Los skips (dedup, filtro) no interrumpen la racha de errores.
        Abandona el perfil si hay MAX_CONSECUTIVE errores seguidos.

        Returns: (downloaded, skipped, errors, remaining_pending)
        """
        import hashlib as _hl
        import json as _json
        from ..gui.bridge import build_filename, _passes_ext_filter

        pu_id        = pu.get("id")
        pending_list = await get_pending_files(folder, pu_id)
        if not pending_list:
            return 0, 0, 0, 0

        dl = sk = err = 0
        consecutive_errors = 0
        done_count = progress_offset

        for row in pending_list:
            if self._stop_requested:
                break

            url_source    = row["url_source"]
            dl_url        = row["download_url"]
            filename_hint = row.get("filename_hint") or "file.bin"
            remote_hash   = row.get("remote_hash")
            extra_headers: dict = {}
            if row.get("extra_headers"):
                try:
                    extra_headers = _json.loads(row["extra_headers"])
                except Exception:
                    pass

            # Filtro de extensiones: saltar sin eliminar de pending_queue.
            # El archivo queda en cola para futuras sesiones con otro filtro.
            if ext_filter and not _passes_ext_filter(
                filename_hint, ext_filter, exclude_mode
            ):
                continue

            # Dedup rápido antes de descargar (no resetea consecutive_errors)
            if await url_exists(folder, url_source):
                await remove_pending(folder, url_source)
                sk += 1
                done_count += 1
                self._set_progress(done_count, progress_total)
                continue
            if remote_hash and await hash_exists(folder, remote_hash):
                await remove_pending(folder, url_source)
                sk += 1
                done_count += 1
                self._set_progress(done_count, progress_total)
                continue

            # Nombre temporal único — contador asignado solo en éxito
            _ext      = Path(filename_hint).suffix
            _tmp_name = "_dl_" + _hl.md5(dl_url.encode()).hexdigest()[:12] + _ext

            self._log(f"  [dim]⬇ {filename_hint[:55]}…[/]")

            # ── Timer: actualiza la UI cada segundo con elapsed/restante ──────
            _t0 = time.monotonic()

            async def _run_timer(_name: str = artist_name, _fn: str = filename_hint) -> None:
                while True:
                    elapsed   = time.monotonic() - _t0
                    remaining = self.ENGINE_TIMEOUT - elapsed
                    if remaining > 0:
                        suffix = f"[dim][{elapsed:.0f}s · abandona en {remaining:.0f}s][/]"
                    else:
                        # Descarga activa con bytes fluyendo — ENGINE_TIMEOUT no la mata.
                        # El usuario puede usar ⏭ Saltar si la ve colgada.
                        suffix = f"[dim][{elapsed:.0f}s · [bold yellow]activo[/] — usa ⏭ para saltar][/]"
                    self._set_current(_name, f"⬇ {_fn[:30]}  {suffix}")
                    await asyncio.sleep(1.0)

            _timer_task = asyncio.create_task(_run_timer())

            def _on_status(msg: str, _name: str = artist_name, _fn: str = filename_hint) -> None:
                elapsed   = time.monotonic() - _t0
                remaining = self.ENGINE_TIMEOUT - elapsed
                time_info = (
                    f"{remaining:.0f}s restantes" if remaining > 0 else "activo"
                )
                self._set_current(
                    _name,
                    f"{msg}  [dim]{_fn[:25]}  [{elapsed:.0f}s · {time_info}][/]",
                )

            # ── Descarga como task cancelable (permite skip) ──────────────────
            self._skip_current = False
            self._current_download_task = asyncio.ensure_future(
                engine.download(
                    url=dl_url,
                    dest_dir=folder,
                    filename=_tmp_name,
                    extra_headers=extra_headers or None,
                    total_timeout=self.ENGINE_TIMEOUT,
                    on_status=_on_status,
                    max_retries=1,   # falla rápido; el batch reintenta en siguiente iteración
                )
            )
            # Habilitar botón saltar
            try:
                self.query_one("#btn-batch-skip", Button).disabled = False
            except Exception:
                pass

            _was_skipped = False
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(self._current_download_task),
                    timeout=self.HARD_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # wait_for expiró: cancelar el task subyacente manualmente
                self._current_download_task.cancel()
                try:
                    await self._current_download_task
                except Exception:
                    pass
                _timer_task.cancel()
                consecutive_errors += 1
                err += 1
                elapsed = time.monotonic() - _t0
                self._set_current(artist_name, "")
                self._log(
                    f"  [yellow]⏸ {filename_hint[:45]}"
                    f"  [sin respuesta {elapsed:.0f}s — queda pendiente"
                    f" ({consecutive_errors}/{self.MAX_CONSECUTIVE})][/]"
                )
                if consecutive_errors >= self.MAX_CONSECUTIVE:
                    self._log(
                        f"  [red]✗ {self.MAX_CONSECUTIVE} timeouts consecutivos"
                        f" — servidor caído, pasando al siguiente perfil[/]"
                    )
                    break
                continue
            except asyncio.CancelledError:
                # Puede ser skip del usuario o stop del batch
                self._current_download_task.cancel()
                try:
                    await self._current_download_task
                except Exception:
                    pass
                _timer_task.cancel()
                if self._skip_current:
                    _was_skipped = True
                    self._skip_current = False
                else:
                    raise  # stop real del batch
            except Exception as exc:
                _timer_task.cancel()
                consecutive_errors += 1
                err += 1
                elapsed = time.monotonic() - _t0
                self._set_current(artist_name, "")
                self._log(
                    f"  [red]✗ {filename_hint[:40]}"
                    f"  [{type(exc).__name__}: {exc}]"
                    f"  — queda pendiente ({consecutive_errors}/{self.MAX_CONSECUTIVE})[/]"
                )
                if consecutive_errors >= self.MAX_CONSECUTIVE:
                    self._log(
                        f"  [red]✗ {self.MAX_CONSECUTIVE} errores consecutivos"
                        f" — pasando al siguiente perfil[/]"
                    )
                    break
                continue
            finally:
                _timer_task.cancel()
                self._current_download_task = None
                # Deshabilitar skip hasta la siguiente descarga
                try:
                    self.query_one("#btn-batch-skip", Button).disabled = True
                except Exception:
                    pass
                self._set_current(artist_name, "")

            if _was_skipped:
                self._log(f"  [yellow]⏭ {filename_hint[:55]}  [saltado por usuario][/]")
                sk += 1
                done_count += 1
                self._set_progress(done_count, progress_total)
                continue

            if not result.ok:
                consecutive_errors += 1
                err += 1
                self._log(
                    f"  [red]✗ {filename_hint[:45]}: {result.error}"
                    f"  ({consecutive_errors}/{self.MAX_CONSECUTIVE})[/]"
                )
                if consecutive_errors >= self.MAX_CONSECUTIVE:
                    self._log(
                        f"  [red]✗ {self.MAX_CONSECUTIVE} errores"
                        f" — pasando al siguiente perfil[/]"
                    )
                    break
                continue

            if not result.file_hash:
                consecutive_errors += 1
                err += 1
                if result.dest and result.dest.exists():
                    result.dest.unlink(missing_ok=True)
                self._log(
                    f"  [red]✗ {filename_hint[:50]}: hash nulo"
                    f"  ({consecutive_errors}/{self.MAX_CONSECUTIVE})[/]"
                )
                if consecutive_errors >= self.MAX_CONSECUTIVE:
                    self._log(
                        f"  [red]✗ {self.MAX_CONSECUTIVE} errores consecutivos"
                        f" — pasando al siguiente perfil[/]"
                    )
                    break
                continue

            # Dedup post-descarga (no resetea consecutive_errors)
            if await hash_exists(folder, result.file_hash):
                if result.dest and result.dest.exists():
                    result.dest.unlink()
                await remove_pending(folder, url_source)
                sk += 1
                done_count += 1
                self._set_progress(done_count, progress_total)
                self._log(f"  [dim]≡ {filename_hint[:55]}  [duplicado][/]")
                continue

            # Post-éxito: asignar contador y renombrar temporal
            counter    = await next_counter(folder)
            final_name = build_filename(artist_name, counter, filename_hint)

            if result.file_hash in local_hashes:
                old_path = local_hashes[result.file_hash]
                try:
                    old_path.replace(folder / final_name)
                    local_hashes[result.file_hash] = folder / final_name
                    if result.dest and result.dest.exists():
                        result.dest.unlink()
                except OSError:
                    try:
                        result.dest.replace(folder / final_name)
                    except OSError:
                        final_name = _tmp_name
                    local_hashes[result.file_hash] = folder / final_name
            else:
                try:
                    result.dest.replace(folder / final_name)
                    local_hashes[result.file_hash] = folder / final_name
                except OSError:
                    final_name = _tmp_name
                    local_hashes[result.file_hash] = result.dest

            await add_file(
                artist_dir=folder,
                file_hash=result.file_hash,
                filename=final_name,
                url_source=url_source,
                file_size=result.file_size,
                counter=counter,
            )
            await remove_pending(folder, url_source)
            dl += 1
            done_count += 1
            consecutive_errors = 0  # solo se resetea en descarga exitosa
            self._set_progress(done_count, progress_total)
            self._log(f"  [green]✓ {final_name}[/]")

        remaining = await pending_count(folder, pu_id)
        return dl, sk, err, remaining

    # ── Loop principal ───────────────────────────────────────────────────────

    async def _do_batch(
        self,
        batch_workers: int = 1,
        ext_filter: set | None = None,
        exclude_mode: bool = True,
        use_profile_filter: bool = False,
    ) -> None:
        """
        Loop principal del batch:
          - Itera todos los perfiles, escanea y descarga cada uno.
          - Los incompletos se reintentan en la siguiente iteración.
          - Para cuando no quedan incompletos o el usuario detiene el proceso.
        batch_workers      — descargas concurrentes por perfil.
        ext_filter         — set de extensiones global (excluir o incluir).
        use_profile_filter — si True, usa la configuración guardada de cada perfil.
        """
        from ..engine     import DownloadEngine
        from ..gui.bridge import _build_local_hash_map
        from ..index      import update_profile_url_sync, update_profile_last_checked

        config = load_config()
        _slim  = await list_profiles(INDEX_DB)
        # list_profiles no incluye urls — cargar cada perfil completo
        profiles: list[dict] = []
        for _p in _slim:
            _full = await get_profile(INDEX_DB, _p["id"])
            if _full:
                profiles.append(_full)

        enabled = [
            p for p in profiles
            if any(u.get("enabled") and u.get("url") for u in p.get("urls", []))
        ]

        if not enabled:
            self._log("[yellow]No hay perfiles con URLs habilitadas.[/]")
            try:
                self.query_one("#btn-batch-stop", Button).disabled = True
            except Exception:
                pass
            return

        self._log(
            f"[bold]Batch iniciado — {len(enabled)} perfil(es)[/]\n"
            f"[dim]Umbral de abandono: {self.MAX_CONSECUTIVE} errores consecutivos "
            f"por fuente[/]\n"
        )

        to_process   = list(enabled)
        complete_ids: set[int] = set()
        iteration    = 0

        try:
            while to_process:
                iteration    += 1
                still_incomplete: list = []

                self._log(
                    f"\n[bold cyan]{'─' * 52}[/]\n"
                    f"[bold cyan]  Iteración {iteration}"
                    f" — {len(to_process)} perfil(es)[/]\n"
                    f"[bold cyan]{'─' * 52}[/]"
                )
                self._set_stats(
                    iteration, len(complete_ids), len(to_process), len(enabled)
                )

                async with DownloadEngine(config, workers=batch_workers) as engine:
                    for idx, profile in enumerate(to_process):
                        if self._stop_requested:
                            break

                        name   = profile["display_name"]
                        folder = Path(profile["folder_path"])
                        folder.mkdir(parents=True, exist_ok=True)
                        await init_catalog(folder)

                        pos = f"{idx + 1}/{len(to_process)}"
                        self._log(f"\n[bold]▶ {name}[/]  [{pos}]")
                        self._set_current(name, f"perfil {pos}")
                        self._set_stats(
                            iteration,
                            len(complete_ids),
                            len(still_incomplete),
                            len(enabled),
                        )

                        # Determinar ext_filter efectivo para este perfil
                        if use_profile_filter:
                            _stored = profile.get("ext_filter", "")
                            _gids, eff_ext_filter = _decode_profile_filter(_stored)
                            eff_exclude_mode = False
                            if eff_ext_filter:
                                _gnames = [EXT_GROUPS[g][0] for g in _gids if g in EXT_GROUPS]
                                self._log(
                                    f"  [dim]filtro: {', '.join(_gnames) or 'custom'}[/]"
                                )
                        else:
                            eff_ext_filter   = ext_filter or set()
                            eff_exclude_mode = exclude_mode

                        # ── Fase 1: scan si pending_queue vacía ────────────
                        total_pending = 0
                        for pu in profile.get("urls", []):
                            if pu.get("enabled") and pu.get("url"):
                                total_pending += await pending_count(
                                    folder, pu.get("id")
                                )

                        if total_pending == 0:
                            self._set_current(name, "escaneando…")
                            for pu in profile.get("urls", []):
                                if not pu.get("enabled") or not pu.get("url"):
                                    continue
                                if self._stop_requested:
                                    break
                                new_n = await self._scan_url(pu, folder, engine)
                                if new_n > 0:
                                    site = pu.get("site", "?")
                                    self._log(
                                        f"  [dim]↳ {site}: {new_n} nuevo(s)[/]"
                                    )
                                total_pending += new_n

                        if total_pending == 0:
                            self._log("  [green]✓ Ya está al día[/]")
                            complete_ids.add(profile["id"])
                            continue

                        self._log(f"  [cyan]⏳ {total_pending} pendiente(s)[/]")
                        self._set_progress(0, total_pending)
                        self._set_current(
                            name, f"descargando {total_pending} archivo(s)…"
                        )

                        # ── Fase 2: descargar desde pending_queue ───────────
                        dl_total = sk_total = err_total = remaining_total = 0
                        local_hashes     = await _build_local_hash_map(folder)
                        progress_offset  = 0
                        profile_incomplete = False

                        for pu in profile.get("urls", []):
                            if not pu.get("enabled") or not pu.get("url"):
                                continue
                            if self._stop_requested:
                                profile_incomplete = True
                                break

                            dl, sk, err, remaining = await self._download_url(
                                name, pu, folder, engine, local_hashes,
                                progress_offset, total_pending,
                                batch_workers=batch_workers,
                                ext_filter=eff_ext_filter,
                                exclude_mode=eff_exclude_mode,
                            )
                            dl_total        += dl
                            sk_total        += sk
                            err_total       += err
                            remaining_total += remaining
                            progress_offset += dl + sk

                            if remaining > 0:
                                profile_incomplete = True

                            await update_profile_url_sync(
                                INDEX_DB, pu["id"],
                                file_count=(pu.get("file_count") or 0) + dl,
                            )

                        await update_profile_last_checked(INDEX_DB, profile["id"])

                        if profile_incomplete or remaining_total > 0:
                            self._log(
                                f"  [yellow]⚠ Incompleto — "
                                f"✓ {dl_total} · ↷ {sk_total} · ✗ {err_total}"
                                f" · ⏳ {remaining_total} restante(s)[/]"
                            )
                            still_incomplete.append(profile)
                        else:
                            self._log(
                                f"  [green]✓ Completo — "
                                f"{dl_total} descargados · {sk_total} saltados[/]"
                            )
                            complete_ids.add(profile["id"])

                to_process = still_incomplete

                if self._stop_requested:
                    break

        except asyncio.CancelledError:
            self._log("\n[bold yellow]⚠ Batch detenido por el usuario.[/]")
        except Exception as exc:
            import traceback as _tb
            self._log(f"\n[bold red]✗ Error inesperado: {exc}[/]")
            self._log(f"[dim]{_tb.format_exc()}[/]")

        # ── Resumen final ────────────────────────────────────────────────────
        try:
            self.query_one("#btn-batch-stop", Button).disabled = True
            self.query_one("#btn-batch-skip", Button).disabled = True
            self._set_current("Batch finalizado")
            if not self._stop_requested:
                self._log(
                    f"\n[bold green]✓ Batch completo — "
                    f"{len(complete_ids)} / {len(enabled)} perfiles "
                    f"en {iteration} iteración(es)[/]"
                )
            else:
                self._log(
                    f"\n[bold yellow]Detenido en iteración {iteration} — "
                    f"{len(complete_ids)} completos · "
                    f"{len(to_process)} pendientes[/]"
                )
        except Exception:
            pass


# ── SettingsScreen ──────────────────────────────────────────────────────────

class SettingsScreen(Screen):
    """Configuración global de cherry-dl."""

    BINDINGS = [
        Binding("escape", "go_back", "Volver", show=True),
        Binding("s",      "save",    "Guardar", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Label("  CONFIGURACIÓN GLOBAL", classes="section-label")
        yield Rule()

        cfg = load_config()
        with Container(id="settings-grid"):
            # Columna izquierda
            with Vertical(classes="setting-row"):
                yield Label("Carpeta de descargas:")
                yield Input(str(cfg.download_path), id="cfg-download-dir")

            with Vertical(classes="setting-row"):
                yield Label("Workers por defecto:")
                yield Input(str(cfg.workers), id="cfg-workers")

            with Vertical(classes="setting-row"):
                yield Label("Timeout conexión (s):")
                yield Input(str(cfg.timeout), id="cfg-timeout")

            with Vertical(classes="setting-row"):
                yield Label("Stall timeout (s):")
                yield Input(str(cfg.network.stall_timeout), id="cfg-stall")

            # Columna derecha
            with Vertical(classes="setting-row"):
                yield Label("Delay mínimo entre requests (s):")
                yield Input(str(cfg.network.delay_min), id="cfg-delay-min")

            with Vertical(classes="setting-row"):
                yield Label("Delay máximo entre requests (s):")
                yield Input(str(cfg.network.delay_max), id="cfg-delay-max")

            with Vertical(classes="setting-row"):
                yield Label("Reintentos API:")
                yield Input(str(cfg.network.retries_api), id="cfg-retries-api")

            with Vertical(classes="setting-row"):
                yield Label("Reintentos archivo:")
                yield Input(str(cfg.network.retries_file), id="cfg-retries-file")

        with Horizontal(id="actions-row"):
            yield Button("← Volver",  id="btn-back")
            yield Button("💾 Guardar", id="btn-save", classes="-primary")

        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self.action_save()
        elif event.button.id == "btn-back":
            self.action_go_back()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_save(self) -> None:
        try:
            cfg = load_config()
            cfg.download_dir        = self.query_one("#cfg-download-dir", Input).value.strip()
            cfg.workers             = int(self.query_one("#cfg-workers",       Input).value)
            cfg.timeout             = int(self.query_one("#cfg-timeout",       Input).value)
            cfg.network.stall_timeout = int(self.query_one("#cfg-stall",       Input).value)
            cfg.network.delay_min   = float(self.query_one("#cfg-delay-min",   Input).value)
            cfg.network.delay_max   = float(self.query_one("#cfg-delay-max",   Input).value)
            cfg.network.retries_api = int(self.query_one("#cfg-retries-api",   Input).value)
            cfg.network.retries_file = int(self.query_one("#cfg-retries-file", Input).value)
            save_config(cfg)
            self.app.notify("Configuración guardada", severity="information")
        except Exception as exc:
            self.app.notify(f"Error al guardar: {exc}", severity="error")


# ── App principal ───────────────────────────────────────────────────────────

class CherryApp(App):
    """Cherry-DL TUI."""

    CSS_PATH  = str(Path(__file__).parent / "theme.tcss")
    TITLE     = "cherry-dl"
    SUB_TITLE = "descargador de colecciones"

    BINDINGS = [
        Binding("ctrl+c", "quit",            "Salir",  show=True),
        Binding("q",      "quit",            "Salir",  show=False),
        Binding("ctrl+v", "paste_clipboard", "Pegar",  show=False),
    ]

    async def on_mount(self) -> None:
        await init_index(INDEX_DB)
        await self.push_screen(ProfilesScreen())

    # ── Portapapeles: Ctrl+V ──────────────────────────────────────────────

    def action_paste_clipboard(self) -> None:
        """Ctrl+V llega al App — pegar en el Input enfocado."""
        self._paste_into_focused(_read_clipboard())

    def _paste_into_focused(self, text: str) -> None:
        if not text:
            self.notify("Portapapeles vacío", severity="warning")
            return
        focused = self.screen.focused
        if not isinstance(focused, Input):
            return
        pos    = focused.cursor_position
        new_v  = focused.value[:pos] + text + focused.value[pos:]
        focused.value           = new_v
        focused.cursor_position = pos + len(text)

    # ── Menú contextual: clic derecho en cualquier Input ──────────────────

    def on_mouse_up(self, event) -> None:
        if getattr(event, "button", 0) != 3:
            return
        focused = self.screen.focused
        if not isinstance(focused, Input):
            return
        self._ctx_target = focused
        self.push_screen(InputContextMenu(), self._on_ctx_action)

    def _on_ctx_action(self, action: str | None) -> None:
        target = getattr(self, "_ctx_target", None)
        if not action or not isinstance(target, Input):
            return
        match action:
            case "ctx-paste":
                self._paste_into_focused(_read_clipboard())
            case "ctx-select-all":
                target.action_select_all()
            case "ctx-clear":
                target.value = ""
                target.cursor_position = 0


def run() -> None:
    """Punto de entrada de la TUI."""
    CherryApp().run()


if __name__ == "__main__":
    run()
