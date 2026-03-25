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

from ..catalog import add_file, get_stats, hash_exists, init_catalog, next_counter, url_exists
from ..config import INDEX_DB, load_config, save_config
from ..index import (
    add_profile_url,
    create_profile,
    delete_profile,
    get_profile,
    init_index,
    list_profiles,
    set_profile_url_enabled,
    update_profile_ext_filter,
    update_profile_last_checked,
)


# ── Portapapeles del sistema ────────────────────────────────────────────────

def _read_clipboard() -> str:
    """Lee texto del portapapeles (Wayland / X11). Devuelve '' si falla."""
    import subprocess
    for cmd in (
        ["wl-paste", "--no-newline"],
        ["wl-paste"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if r.returncode == 0:
                return r.stdout.rstrip("\n")
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
        folder = cfg.download_path / name
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
            yield Button("+ Nuevo",    id="btn-new",     classes="-primary")
            yield Button("⟳ Refresh",  id="btn-refresh")
            yield Button("⌫ Eliminar", id="btn-delete",  classes="-danger")
            yield Button("⚙ Config",   id="btn-settings")
        yield Label("  PERFILES", classes="section-label")
        yield DataTable(id="profiles-table", cursor_type="row")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        match event.button.id:
            case "btn-new":      self.action_new_profile()
            case "btn-refresh":  self.action_refresh()
            case "btn-delete":   self.action_delete_profile()
            case "btn-settings": self.action_settings()

    def on_mount(self) -> None:
        tbl = self.query_one("#profiles-table", DataTable)
        tbl.add_column("#",           width=4)
        tbl.add_column("Nombre",      width=28)
        tbl.add_column("Sitio",       width=10)
        tbl.add_column("Archivos",    width=10)
        tbl.add_column("Última sync", width=14)
        tbl.add_column("Carpeta",     width=40)
        self.run_worker(self._load_profiles(), exclusive=True)

    async def _load_profiles(self) -> None:
        tbl = self.query_one("#profiles-table", DataTable)
        tbl.clear()
        try:
            profiles = await list_profiles(INDEX_DB)
            for p in profiles:
                folder = Path(p["folder_path"])
                stats  = await get_stats(folder) if folder.exists() else {"total": 0}
                last   = (p.get("last_checked") or "Nunca")[:10]
                tbl.add_row(
                    str(p["id"]),
                    p["display_name"],
                    p["primary_site"].upper(),
                    str(stats["total"]),
                    last,
                    str(folder),
                    key=str(p["id"]),
                )
        except Exception as exc:
            self.app.notify(f"Error al cargar perfiles: {exc}", severity="error")

    # ── Acciones ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.run_worker(self._load_profiles(), exclusive=True)

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
            yield Label("Filtro ext:")
            yield Input("", id="ext-filter-input", placeholder="jpg,png,mp4")
            yield Label("Pre-scan:")
            yield Input("", id="prescan-input", placeholder="Carpeta de archivos existentes")

        # Acciones
        with Horizontal(id="actions-row"):
            yield Button("⬡ Pre-scan",   id="btn-prescan")
            yield Button("⊘ Deduplicar", id="btn-dedup")
            yield Button("⊟ Compactar",  id="btn-compact")
            yield Button("⟳ Verificar",  id="btn-verify")
            yield Button("↑ Actualizar", id="btn-update")
            yield Button("▶ Descargar",  id="btn-download", classes="-primary")
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

            # Restaurar filtro guardado
            ext = profile.get("ext_filter", "")
            if ext:
                self.query_one("#ext-filter-input", Input).value = ext

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
            async with aiosqlite.connect(INDEX_DB) as db:
                await db.execute("DELETE FROM profile_urls WHERE id = ?", (url_id,))
                await db.commit()
            await self._load_profile()
            self.app.notify("URL eliminada")
        except Exception as exc:
            self.app.notify(f"Error: {exc}", severity="error")

    # ── Acciones de teclado ───────────────────────────────────────────────

    def action_go_back(self) -> None:
        self.action_cancel_download()
        self.app.pop_screen()

    def action_start_download(self) -> None:
        if self._is_busy or not self._profile:
            return
        self._run_download()

    def action_start_update(self) -> None:
        if self._is_busy or not self._profile:
            return
        self._run_download(update_only=True)

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
        self.query_one("#counters-label", Static).update(
            f"↓ {dl}  skip {sk}  ✗ {err}  ⏭ {def_}"
        )

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        for btn_id in (
            "btn-download", "btn-update", "btn-verify", "btn-prescan",
            "btn-dedup", "btn-compact",
            "btn-add-url", "btn-del-url",
        ):
            self.query_one(f"#{btn_id}", Button).disabled = busy
        self.query_one("#btn-cancel", Button).disabled = not busy
        if busy:
            self._set_semaphore("running")

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
        if event.input.id == "ext-filter-input" and self._profile:
            self.run_worker(
                update_profile_ext_filter(INDEX_DB, self._profile["id"], event.value),
                exclusive=False,
            )

    # ── Download ──────────────────────────────────────────────────────────

    @work(exclusive=True, group="download")
    async def _run_download(self, update_only: bool = False) -> None:
        self._set_busy(True)
        self.query_one("#activity-log", RichLog).clear()
        try:
            await self._do_download(update_only=update_only)
        except asyncio.CancelledError:
            self._log("[yellow]Descarga cancelada por el usuario.[/]")
            self._set_semaphore("cancelled")
        except Exception as exc:
            self._log(f"[red]✗ Error: {exc}[/]")
            self._set_semaphore("error")
        finally:
            self._set_busy(False)

    async def _do_download(self, update_only: bool = False) -> None:
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
        ext_filter = _parse_ext_filter(self.query_one("#ext-filter-input", Input).value)

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

                # Calcular fecha de corte para "Actualizar"
                url_since: datetime | None = None
                if update_only and pu.get("last_synced"):
                    url_since = parse_date_utc(pu["last_synced"])
                    if url_since:
                        self._log(
                            f"  [dim]↑ Actualizar desde {pu['last_synced'][:16]}[/]"
                        )

                dl_before    = downloaded_ref[0]
                local_hashes = await _build_local_hash_map(folder)
                file_queue: asyncio.Queue = asyncio.Queue(maxsize=workers * 3)
                seen_urls: set[str] = set()

                async def producer() -> None:
                    try:
                        async for fi in template.iter_files(
                            artist_info, since=url_since
                        ):
                            if not _passes_ext_filter(fi.filename, ext_filter, not ext_filter):
                                skipped_ref[0] += 1
                                self._log(f"  [dim]— {fi.filename[:60]}  [filtro ext][/]")
                                continue
                            if fi.url in seen_urls:
                                skipped_ref[0] += 1
                                continue
                            seen_urls.add(fi.url)
                            await asyncio.wait_for(file_queue.put(fi), timeout=120.0)
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
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            if fi.remote_hash and await hash_exists(folder, fi.remote_hash):
                                skipped_ref[0] += 1
                                self._log(f"  [dim]— {fi.filename[:60]}  [hash en catálogo][/]")
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

                            try:
                                result = await asyncio.wait_for(
                                    engine.download(
                                        url=fi.url,
                                        dest_dir=folder,
                                        filename=final_name,
                                        on_progress=make_cb(slot_id),
                                        extra_headers=fi.extra_headers or None,
                                    ),
                                    timeout=600.0,
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
                                    old_path.rename(new_path)
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

            # Cola diferida
            if deferred:
                self._log(f"\n[yellow]⏭ Cola diferida: {len(deferred)} archivo(s)…[/]")
                for file_info, a_info, dest_folder in deferred:
                    if await url_exists(dest_folder, file_info.dedup_key):
                        skipped_ref[0] += 1
                        continue
                    counter    = await next_counter(dest_folder)
                    final_name = build_filename(a_info.name, counter, file_info.filename)
                    result = await engine.download(
                        url=file_info.url,
                        dest_dir=dest_folder,
                        filename=final_name,
                        extra_headers=file_info.extra_headers or None,
                    )
                    if result.ok and result.file_hash:
                        await add_file(
                            artist_dir=dest_folder,
                            file_hash=result.file_hash,
                            filename=final_name,
                            url_source=file_info.dedup_key,
                            file_size=result.file_size,
                            counter=counter,
                        )
                        downloaded_ref[0] += 1
                        self._log(f"  [green]✓ {final_name} (reintento)[/]")
                    else:
                        deferred_count_ref[0] += 1
                        self._log(f"  [yellow]⏭ {file_info.filename} — pendiente próx. sync[/]")
                    self._update_counters(
                        downloaded_ref[0], skipped_ref[0],
                        errors_ref[0], deferred_count_ref[0],
                    )

        # Resumen final
        await update_profile_last_checked(INDEX_DB, profile["id"])
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

        async with DownloadEngine(config) as engine:
            for pu in profile["urls"]:
                if not pu["enabled"] or not pu["url"]:
                    continue
                template = get_template(pu["url"], engine)
                if not template:
                    self._log(f"[red]Sin template para {pu['url']}[/]")
                    continue
                artist_info = await template.get_artist_info(pu["url"])
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
