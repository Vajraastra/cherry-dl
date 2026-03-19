"""
Interfaz gráfica de cherry-dl — Dear PyGui.

Layout:
  ┌──────────────────────────────────────────────┐
  │  [Descargas]  [Colecciones]  [Configuración] │
  ├──────────────────────────────────────────────┤
  │  URL: [_________________________] [Añadir]   │
  │  ┌─ Cola ──────────────────────────────────┐ │
  │  │ Artista │ Sitio │ Progreso │ Estado │ # │ │
  │  └─────────────────────────────────────────┘ │
  │  ┌─ Log ───────────────────────────────────┐ │
  │  │ 09:15 ✓ Yoruichi.psd descargado         │ │
  │  └─────────────────────────────────────────┘ │
  └──────────────────────────────────────────────┘

Threading:
  - Hilo principal:   DPG render loop
  - Hilo daemon:      asyncio event loop (bridge.py)
  - Comunicación:     queue.Queue[ProgressUpdate]
"""

from __future__ import annotations

import queue
import uuid
from datetime import datetime
from pathlib import Path

import dearpygui.dearpygui as dpg

from ..config import INDEX_DB, UserConfig, ensure_dirs, load_config, save_config
from .bridge import (
    AsyncBridge, ProgressUpdate,
    download_for_gui, prescan_and_download, load_collections_async,
    repair_async, update_async, _parse_ext_filter,
)
from .native_dialog import pick_directory

# ── Paleta cherry-dl ──────────────────────────────────────────────────────────

_C_BG         = [15,  15,  25]
_C_BG2        = [22,  22,  36]
_C_BORDER     = [40,  40,  60]
_C_CHERRY     = [200, 40,  60]
_C_CHERRY_HV  = [230, 65,  85]
_C_CHERRY_ACT = [160, 25,  45]
_C_GREEN      = [70,  200, 100]
_C_YELLOW     = [220, 180, 50]
_C_RED        = [220, 60,  60]
_C_TEXT       = [220, 220, 230]
_C_DIM        = [130, 130, 150]

_MAX_LOG = 120   # líneas máximas en el log antes de descartar las más viejas
_WIN_W   = 1100
_WIN_H   = 720


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _error_style(kind: str) -> tuple[str, list, str]:
    """
    Retorna (icono, color_rgb, etiqueta) según el tipo de error.
    Permite distinguir errores permanentes de transitorios de un vistazo.
    """
    match kind:
        case "not_found":
            return "~", [90, 90, 110],   "NO EXISTE"   # gris — ignorado silenciosamente
        case "auth":
            return "✗", [180, 100, 30],  "AUTH"        # naranja — requiere sesión
        case "cloudflare":
            return "⚠", [220, 160, 30],  "CLOUDFLARE"  # amarillo — esperando y reintentando
        case "rate_limit":
            return "⚠", [220, 160, 30],  "RATE LIMIT"  # amarillo — esperando y reintentando
        case "server_error":
            return "✗", [200, 80, 30],   "SERVIDOR"    # naranja-rojo — error del servidor
        case "timeout":
            return "✗", [180, 80, 180],  "TIMEOUT"     # violeta — timeout
        case "network":
            return "✗", [180, 80, 180],  "RED"         # violeta — error de conexión
        case _:
            return "✗", [220, 60, 60],   "ERROR"       # rojo — desconocido


# ══════════════════════════════════════════════════════════════════════════════
# App principal
# ══════════════════════════════════════════════════════════════════════════════

class CherryApp:

    def __init__(self) -> None:
        self.config: UserConfig = load_config()
        self.bridge = AsyncBridge()
        self._updates: queue.Queue[ProgressUpdate] = queue.Queue()

        # session_id → dict con tags de los widgets de la fila + contadores
        self._sessions: dict[str, dict] = {}
        # contador para tags únicos del log
        self._log_count = 0
        # artista pendiente de borrar (se llena al abrir el modal de confirmación)
        self._pending_delete: dict | None = None
        # acumuladores globales para la barra de progreso general
        self._global_done:  int = 0
        self._global_total: int = 0
        # contadores globales de la sesión (descargados, duplicados, renombrados, errores)
        self._stats: dict[str, int] = {
            "done": 0, "skip": 0, "renamed": 0, "errors": 0
        }
        # set de session_ids con fila activa en el workers panel
        self._active_worker_sessions: set[str] = set()

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        ensure_dirs(self.config)
        self.bridge.start()

        dpg.create_context()
        self._build_theme()
        self._build_ui()

        dpg.create_viewport(
            title="cherry-dl",
            width=_WIN_W,
            height=_WIN_H,
            min_width=800,
            min_height=500,
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

        # Cargar colecciones al arrancar
        self._refresh_collections()

        # Render loop principal
        while dpg.is_dearpygui_running():
            self._process_updates()
            dpg.render_dearpygui_frame()

        dpg.destroy_context()
        self.bridge.stop()

    # ── Construcción de UI ────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        with dpg.window(tag="main_window", no_title_bar=True, no_move=True,
                        no_resize=True):

            # Título / header
            with dpg.group(horizontal=True):
                dpg.add_text("cherry-dl", color=_C_CHERRY)
                dpg.add_text("  mass downloader", color=_C_DIM)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # Barra de progreso global
            with dpg.group(horizontal=True):
                dpg.add_text("Global:", color=_C_DIM)
                dpg.add_progress_bar(
                    tag="global_progress",
                    default_value=0.0,
                    width=-180,
                    overlay="Sin actividad",
                )
                dpg.add_text("", tag="global_label", color=_C_DIM)
            dpg.add_spacer(height=4)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            with dpg.tab_bar(tag="main_tabs"):
                with dpg.tab(label="  Descargas  "):
                    self._build_downloads_tab()
                with dpg.tab(label="  Colecciones  "):
                    self._build_collections_tab()
                with dpg.tab(label="  Configuración  "):
                    self._build_settings_tab()

        # Modal de confirmación para borrar artista (fuera del tab_bar)
        with dpg.window(tag="modal_delete", label="Confirmar borrado",
                        modal=True, show=False, no_resize=True,
                        width=440, height=220):
            dpg.add_text("", tag="modal_delete_msg", wrap=380)
            dpg.add_spacer(height=12)
            with dpg.group(horizontal=True):
                dpg.add_button(label="  Borrar  ", width=120,
                               callback=self._on_delete_confirm)
                dpg.add_spacer(width=20)
                dpg.add_button(label="  Cancelar  ", width=120,
                               callback=lambda: dpg.configure_item("modal_delete", show=False))

    # ── Tab: Descargas ────────────────────────────────────────────────────────

    def _build_downloads_tab(self) -> None:
        dpg.add_spacer(height=6)

        # ── Fila 1: URL ───────────────────────────────────────────────────────
        with dpg.group(horizontal=True):
            dpg.add_text("URL:    ", color=_C_DIM)
            dpg.add_input_text(
                tag="url_input",
                hint="https://kemono.cr/patreon/user/...",
                width=-1,
            )

        # ── Fila 2: Workers + Añadir ──────────────────────────────────────────
        with dpg.group(horizontal=True):
            dpg.add_text("Workers:", color=_C_DIM)
            dpg.add_input_int(
                tag="workers_input",
                default_value=self.config.workers,
                min_value=1, max_value=10,
                width=90,
            )
            dpg.add_spacer(width=8)
            dpg.add_button(
                label="  Añadir  ",
                callback=self._on_add,
                width=130,
            )

        # ── Fila 3: Filtro de extensiones ─────────────────────────────────────
        with dpg.group(horizontal=True):
            dpg.add_text("Filtro:  ", color=_C_DIM)
            dpg.add_checkbox(
                tag="ext_exclude_mode",
                label="Excluir",
                default_value=True,
            )
            dpg.add_spacer(width=6)
            dpg.add_input_text(
                tag="ext_filter_input",
                hint="Ej: .zip, .rar   (vacío = sin filtro)",
                width=-1,
            )

        # ── Fila 4: Carpeta pre-scan ──────────────────────────────────────────
        with dpg.group(horizontal=True):
            dpg.add_text("Carpeta:", color=_C_DIM)
            dpg.add_input_text(
                tag="prescan_folder_input",
                hint="Opcional: carpeta con archivos existentes a indexar primero",
                width=-140,
                readonly=True,
            )
            dpg.add_button(
                label="  Examinar  ",
                callback=self._on_browse_prescan,
                width=100,
            )
            dpg.add_button(
                label=" X ",
                callback=self._on_clear_prescan,
                width=32,
            )

        dpg.add_spacer(height=6)
        dpg.add_separator()
        dpg.add_spacer(height=4)
        dpg.add_text("Cola de descargas", color=_C_DIM)
        dpg.add_spacer(height=4)

        # Tabla de descargas activas
        with dpg.table(
            tag="downloads_table",
            header_row=True,
            borders_innerH=True,
            borders_outerH=True,
            borders_innerV=True,
            borders_outerV=True,
            scrollY=True,
            height=200,
            row_background=True,
        ):
            dpg.add_table_column(label="Artista",  width_fixed=True,   init_width_or_weight=160)
            dpg.add_table_column(label="Sitio",    width_fixed=True,   init_width_or_weight=80)
            dpg.add_table_column(label="Progreso", width_stretch=True)
            dpg.add_table_column(label="Estado",   width_fixed=True,   init_width_or_weight=110)
            dpg.add_table_column(label="Archivos", width_fixed=True,   init_width_or_weight=80)

        dpg.add_spacer(height=8)
        dpg.add_text("Actividad", color=_C_DIM)
        dpg.add_spacer(height=4)

        # Panel de workers activos — una fila por sesión en curso
        with dpg.child_window(tag="workers_panel", height=110, border=True,
                               autosize_x=True):
            dpg.add_text("Sin descargas activas.", color=_C_DIM,
                         tag="workers_empty")

        dpg.add_spacer(height=6)

        # Barra de estadísticas globales
        with dpg.group(horizontal=True):
            dpg.add_text("✓", color=_C_GREEN)
            dpg.add_text("0", tag="stat_done", color=_C_GREEN)
            dpg.add_text(" desc   ", color=_C_DIM)
            dpg.add_text("~", color=[80, 80, 130])
            dpg.add_text("0", tag="stat_skip", color=[80, 80, 130])
            dpg.add_text(" dupl   ", color=_C_DIM)
            dpg.add_text("↩", color=_C_YELLOW)
            dpg.add_text("0", tag="stat_renamed", color=_C_YELLOW)
            dpg.add_text(" ren   ", color=_C_DIM)
            dpg.add_text("✗", color=_C_RED)
            dpg.add_text("0", tag="stat_errors", color=_C_RED)
            dpg.add_text(" err", color=_C_DIM)

        dpg.add_spacer(height=6)

        # Mini-log: solo eventos notables (errores, completados, warnings)
        with dpg.child_window(tag="log_window", height=70, border=True,
                               autosize_x=True):
            dpg.add_text("Listo.", color=_C_DIM, tag="log_0")
            self._log_count = 1

    # ── Tab: Colecciones ──────────────────────────────────────────────────────

    def _build_collections_tab(self) -> None:
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            dpg.add_button(label="  Actualizar  ", callback=self._refresh_collections)
            dpg.add_text("", tag="collections_status", color=_C_DIM)
        dpg.add_spacer(height=8)

        with dpg.table(
            tag="collections_table",
            header_row=True,
            borders_innerH=True,
            borders_outerH=True,
            borders_innerV=True,
            borders_outerV=True,
            scrollY=True,
            height=-1,
            row_background=True,
            resizable=True,
        ):
            dpg.add_table_column(label="Sitio",    width_fixed=True,  init_width_or_weight=90)
            dpg.add_table_column(label="Artista",  width_fixed=True,  init_width_or_weight=180)
            dpg.add_table_column(label="ID",       width_fixed=True,  init_width_or_weight=120)
            dpg.add_table_column(label="Archivos", width_fixed=True,  init_width_or_weight=80)
            dpg.add_table_column(label="Tamaño",   width_fixed=True,  init_width_or_weight=90)
            dpg.add_table_column(label="Ruta",     width_stretch=True)
            dpg.add_table_column(label="",         width_fixed=True,  init_width_or_weight=230)

    # ── Tab: Configuración ────────────────────────────────────────────────────

    def _build_settings_tab(self) -> None:
        dpg.add_spacer(height=12)

        with dpg.group():
            dpg.add_text("Directorio de descargas (carpeta madre)", color=_C_DIM)
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    tag="cfg_download_dir",
                    default_value=self.config.download_dir,
                    width=-140,
                )
                dpg.add_button(
                    label="  Examinar  ",
                    callback=self._on_browse_download_dir,
                    width=120,
                )
            dpg.add_spacer(height=8)

            dpg.add_text("Workers (descargas paralelas)", color=_C_DIM)
            dpg.add_slider_int(
                tag="cfg_workers",
                default_value=self.config.workers,
                min_value=1, max_value=10,
                width=300,
            )
            dpg.add_spacer(height=8)

            dpg.add_text("Delay entre requests (segundos)", color=_C_DIM)
            with dpg.group(horizontal=True):
                dpg.add_text("Mín:", color=_C_DIM)
                dpg.add_input_float(
                    tag="cfg_delay_min",
                    default_value=self.config.network.delay_min,
                    width=100, step=0.5,
                )
                dpg.add_text("  Máx:", color=_C_DIM)
                dpg.add_input_float(
                    tag="cfg_delay_max",
                    default_value=self.config.network.delay_max,
                    width=100, step=0.5,
                )
            dpg.add_spacer(height=8)

            dpg.add_text("Timeout por request (segundos)", color=_C_DIM)
            dpg.add_input_int(
                tag="cfg_timeout",
                default_value=self.config.timeout,
                width=100,
            )
            dpg.add_spacer(height=16)

            with dpg.group(horizontal=True):
                dpg.add_button(label="  Guardar  ", callback=self._on_save_config, width=120)
                dpg.add_text("", tag="cfg_status", color=_C_GREEN)

            dpg.add_spacer(height=16)
            dpg.add_separator()
            dpg.add_spacer(height=10)

            dpg.add_text("Migrar colecciones", color=_C_DIM)
            dpg.add_text(
                "Mueve todas las carpetas de artistas a otra carpeta madre y actualiza el índice.",
                color=_C_DIM,
            )
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                dpg.add_input_text(
                    tag="cfg_migrate_dest",
                    hint="Carpeta destino de la migración…",
                    width=-220,
                    readonly=True,
                )
                dpg.add_button(
                    label="  Examinar  ",
                    callback=self._on_browse_migrate_dest,
                    width=100,
                )
                dpg.add_button(
                    label="  Migrar  ",
                    callback=self._on_migrate,
                    width=90,
                )
            dpg.add_text("", tag="cfg_migrate_status", color=_C_DIM)

    # ── Tema cherry ───────────────────────────────────────────────────────────

    def _build_theme(self) -> None:
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                # Fondos
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg,        _C_BG)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg,         _C_BG2)
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg,         _C_BG2)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg,         _C_BG2)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,  [30, 30, 48])
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,   [35, 35, 55])
                # Títulos y tabs
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg,         _C_BG2)
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,   _C_CHERRY)
                dpg.add_theme_color(dpg.mvThemeCol_Tab,             _C_BG2)
                dpg.add_theme_color(dpg.mvThemeCol_TabHovered,      _C_CHERRY_HV)
                dpg.add_theme_color(dpg.mvThemeCol_TabActive,       _C_CHERRY)
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocused,    _C_BG2)
                dpg.add_theme_color(dpg.mvThemeCol_TabUnfocusedActive, _C_CHERRY_ACT)
                # Botones
                dpg.add_theme_color(dpg.mvThemeCol_Button,          _C_CHERRY)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   _C_CHERRY_HV)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    _C_CHERRY_ACT)
                # Headers (tabla)
                dpg.add_theme_color(dpg.mvThemeCol_Header,          [_C_CHERRY[0], _C_CHERRY[1], _C_CHERRY[2], 100])
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,   [_C_CHERRY[0], _C_CHERRY[1], _C_CHERRY[2], 160])
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,    _C_CHERRY)
                # Progress bar fill color
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram,   _C_CHERRY)
                # Separadores y bordes
                dpg.add_theme_color(dpg.mvThemeCol_Separator,       _C_BORDER)
                dpg.add_theme_color(dpg.mvThemeCol_Border,          _C_BORDER)
                # Sliders
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab,      _C_CHERRY)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, _C_CHERRY_HV)
                # Checkmark
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark,       _C_CHERRY)
                # Scroll
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,        _C_CHERRY_ACT)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, _C_CHERRY)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive,  _C_CHERRY_HV)
                # Texto
                dpg.add_theme_color(dpg.mvThemeCol_Text, _C_TEXT)
                # Redondeo
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,  8)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,   6)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,   4)
                dpg.add_theme_style(dpg.mvStyleVar_TabRounding,     4)
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding,    4)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,   12, 10)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding,    8, 4)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,     8, 6)

        dpg.bind_theme(theme)

    # ── Callbacks de UI ───────────────────────────────────────────────────────

    # ── Workers panel ─────────────────────────────────────────────────────────

    def _add_worker_row(self, session_id: str, artist_name: str) -> None:
        """Añade una fila al panel de workers para la sesión recién iniciada."""
        # Ocultar el placeholder vacío si es el primer worker
        if not self._active_worker_sessions and dpg.does_item_exist("workers_empty"):
            dpg.configure_item("workers_empty", show=False)

        self._active_worker_sessions.add(session_id)
        short = artist_name[:22].ljust(23)
        with dpg.group(horizontal=True, tag=f"w_row_{session_id}",
                       parent="workers_panel"):
            dpg.add_text(short, tag=f"w_artist_{session_id}", color=_C_TEXT)
            dpg.add_text(" • ", tag=f"w_icon_{session_id}", color=_C_DIM)
            dpg.add_text("Iniciando…", tag=f"w_file_{session_id}", color=_C_DIM)

    def _update_worker_status(self, session_id: str,
                               icon: str, icon_color: list,
                               filename: str, file_color: list) -> None:
        """Actualiza el icono y nombre de archivo de una fila de worker."""
        if dpg.does_item_exist(f"w_icon_{session_id}"):
            dpg.set_value(f"w_icon_{session_id}", f" {icon} ")
            dpg.configure_item(f"w_icon_{session_id}", color=icon_color)
        if dpg.does_item_exist(f"w_file_{session_id}"):
            truncated = (filename[:62] + "…") if len(filename) > 62 else filename
            dpg.set_value(f"w_file_{session_id}", truncated)
            dpg.configure_item(f"w_file_{session_id}", color=file_color)

    def _remove_worker_row(self, session_id: str) -> None:
        """Elimina la fila del worker y restaura el placeholder si no quedan activos."""
        self._active_worker_sessions.discard(session_id)
        if dpg.does_item_exist(f"w_row_{session_id}"):
            dpg.delete_item(f"w_row_{session_id}")
        # Si no quedan workers activos, mostrar placeholder
        if not self._active_worker_sessions and dpg.does_item_exist("workers_empty"):
            dpg.configure_item("workers_empty", show=True)

    def _update_stats(self, key: str) -> None:
        """Incrementa un contador de stats y actualiza su widget."""
        self._stats[key] += 1
        tag_map = {
            "done":    "stat_done",
            "skip":    "stat_skip",
            "renamed": "stat_renamed",
            "errors":  "stat_errors",
        }
        tag = tag_map.get(key)
        if tag:
            dpg.set_value(tag, str(self._stats[key]))

    # ── Tabla de descargas ────────────────────────────────────────────────────

    def _add_session_row(self, session_id: str, label: str) -> None:
        """Crea una fila en la tabla de descargas y registra la sesión."""
        progress_tag = f"prog_{session_id}"
        status_tag   = f"stat_{session_id}"
        files_tag    = f"file_{session_id}"

        with dpg.table_row(parent="downloads_table", tag=f"row_{session_id}"):
            dpg.add_text(label, tag=f"name_{session_id}", color=_C_DIM)
            dpg.add_text("—",   tag=f"site_{session_id}", color=_C_DIM)
            dpg.add_progress_bar(tag=progress_tag, default_value=0.0,
                                 width=-1, overlay="En cola…")
            dpg.add_text("En cola", tag=status_tag, color=_C_DIM)
            dpg.add_text("0/?",     tag=files_tag,  color=_C_DIM)

        self._sessions[session_id] = {
            "progress": progress_tag,
            "status":   status_tag,
            "files":    files_tag,
            "name":     f"name_{session_id}",
            "site":     f"site_{session_id}",
            "done":     0,
            "total":    0,
        }

    def _on_add(self) -> None:
        """Inicia la descarga de una URL ingresada por el usuario."""
        url = dpg.get_value("url_input").strip()
        if not url:
            self._log("URL vacía.", color=_C_RED)
            return

        workers      = dpg.get_value("workers_input")
        ext_raw      = dpg.get_value("ext_filter_input").strip()
        exclude_mode = dpg.get_value("ext_exclude_mode")
        ext_filter   = _parse_ext_filter(ext_raw) if ext_raw else set()
        session_id   = uuid.uuid4().hex[:8]

        # Agregar fila placeholder a la tabla
        label = url[:30] + "…" if len(url) > 30 else url
        self._add_session_row(session_id, label)

        # Leer carpeta de pre-scan (opcional)
        prescan_folder = dpg.get_value("prescan_folder_input").strip()

        # Limpiar inputs
        dpg.set_value("url_input", "")
        dpg.set_value("prescan_folder_input", "")

        # Despachar al loop asyncio: con o sin pre-scan
        if ext_filter:
            mode_label = "excluir" if exclude_mode else "solo"
            self._log(
                f"  Filtro: {mode_label} {', '.join(sorted(ext_filter))}",
                color=_C_DIM,
            )

        if prescan_folder and Path(prescan_folder).is_dir():
            self.bridge.submit(
                prescan_and_download(
                    url, Path(prescan_folder), self.config, workers,
                    self._updates, session_id,
                    ext_filter=ext_filter, exclude_mode=exclude_mode,
                )
            )
            self._log(f"Añadido con pre-scan: {url}", color=_C_DIM)
            self._log(f"  Carpeta: {prescan_folder}", color=_C_DIM)
        else:
            self.bridge.submit(
                download_for_gui(
                    url, self.config, workers, self._updates, session_id,
                    ext_filter=ext_filter, exclude_mode=exclude_mode,
                )
            )
            self._log(f"Añadido: {url}", color=_C_DIM)

    def _on_browse_prescan(self) -> None:
        """Abre el selector nativo para la carpeta de pre-scan."""
        current = dpg.get_value("prescan_folder_input").strip()
        pick_directory(
            title="Seleccionar carpeta con archivos existentes",
            start_dir=current or str(Path.home()),
            callback=lambda path: dpg.set_value("prescan_folder_input", path),
        )

    def _on_clear_prescan(self) -> None:
        """Limpia el campo de carpeta de pre-scan."""
        dpg.set_value("prescan_folder_input", "")

    def _on_browse_download_dir(self) -> None:
        """Abre el selector nativo para la carpeta madre de descargas.
        Auto-guarda la ruta al seleccionarla — no requiere clic en Guardar.
        """
        current = dpg.get_value("cfg_download_dir").strip()

        def _on_picked(path: str) -> None:
            dpg.set_value("cfg_download_dir", path)
            self.config = self.config.model_copy(update={"download_dir": path})
            save_config(self.config)
            ensure_dirs(self.config)
            dpg.set_value("cfg_status", " ✓ Carpeta guardada")

        pick_directory(
            title="Seleccionar carpeta madre de descargas",
            start_dir=current or str(Path.home()),
            callback=_on_picked,
        )

    def _on_browse_migrate_dest(self) -> None:
        """Abre el selector nativo para la carpeta destino de migración."""
        pick_directory(
            title="Seleccionar carpeta destino de migración",
            start_dir=str(Path.home()),
            callback=lambda path: dpg.set_value("cfg_migrate_dest", path),
        )

    def _on_migrate(self) -> None:
        """Inicia la migración de colecciones a la carpeta destino seleccionada."""
        dest = dpg.get_value("cfg_migrate_dest").strip()
        if not dest:
            dpg.set_value("cfg_migrate_status", "⚠ Selecciona una carpeta destino primero.")
            dpg.configure_item("cfg_migrate_status", color=_C_YELLOW)
            return

        dest_path = Path(dest)
        old_path  = self.config.download_path

        if dest_path == old_path:
            dpg.set_value("cfg_migrate_status", "⚠ La carpeta destino es la misma que la actual.")
            dpg.configure_item("cfg_migrate_status", color=_C_YELLOW)
            return

        dpg.set_value("cfg_migrate_status", "Migrando…")
        dpg.configure_item("cfg_migrate_status", color=_C_DIM)

        self.bridge.submit(self._migrate_async(old_path, dest_path))

    async def _migrate_async(self, old_root: Path, new_root: Path) -> None:
        """Ejecuta la migración en el hilo async y actualiza la UI al terminar."""
        from ..config import INDEX_DB, save_config
        from ..index import init_index, migrate_all_folders

        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")

        try:
            await init_index(INDEX_DB)
            moved, errors = await migrate_all_folders(INDEX_DB, old_root, new_root)

            # Actualizar config con la nueva carpeta madre
            new_config = self.config.model_copy(update={"download_dir": str(new_root)})
            save_config(new_config)
            self.config = new_config

            msg = f"✓ Migradas {moved} colecciones → {new_root}"
            if errors:
                msg += f"  ({len(errors)} errores)"

            self._updates.put(ProgressUpdate(
                session_id="_sys", update_type="migrate_done",
                artist_name=msg, error="\n".join(errors[:5]),
            ))

        except Exception as e:
            self._updates.put(ProgressUpdate(
                session_id="_sys", update_type="migrate_done",
                artist_name="", error=str(e),
            ))

    def _on_save_config(self) -> None:
        """Guarda la configuración editada por el usuario."""
        from ..config import NetworkConfig

        self.config = self.config.model_copy(update={
            "download_dir": dpg.get_value("cfg_download_dir"),
            "workers":      dpg.get_value("cfg_workers"),
            "timeout":      dpg.get_value("cfg_timeout"),
            "network": NetworkConfig(
                delay_min=dpg.get_value("cfg_delay_min"),
                delay_max=dpg.get_value("cfg_delay_max"),
                retries_api=self.config.network.retries_api,
                retries_file=self.config.network.retries_file,
            ),
        })
        save_config(self.config)
        ensure_dirs(self.config)
        dpg.set_value("cfg_status", " ✓ Guardado")

    def _refresh_collections(self) -> None:
        """Solicita la carga de colecciones al worker async."""
        dpg.set_value("collections_status", "Cargando…")

        future = self.bridge.submit(load_collections_async(self.config))
        future.add_done_callback(self._on_collections_ready)

    def _on_collections_ready(self, future) -> None:
        """Callback desde el hilo async — encola actualización para el hilo principal."""
        try:
            data = future.result()
        except Exception as e:
            data = []
            self._updates.put(ProgressUpdate(
                session_id="_sys",
                update_type="log_error",
                error=f"Error cargando colecciones: {e}",
            ))
        self._updates.put(ProgressUpdate(
            session_id="_sys",
            update_type="collections",
            payload=data,
        ))

    # ── Procesamiento de actualizaciones (hilo principal) ─────────────────────

    def _process_updates(self) -> None:
        """Drena la cola de actualizaciones y aplica cambios a los widgets DPG."""
        try:
            while True:
                upd: ProgressUpdate = self._updates.get_nowait()
                self._apply_update(upd)
        except queue.Empty:
            pass

    def _apply_update(self, upd: ProgressUpdate) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        session = self._sessions.get(upd.session_id)

        match upd.update_type:

            case "started":
                self._add_worker_row(upd.session_id, upd.artist_name)
                if session:
                    dpg.set_value(session["name"], upd.artist_name[:28])
                    dpg.set_value(session["status"], "Descargando")
                    dpg.configure_item(session["status"], color=_C_YELLOW)
                    dpg.configure_item(session["progress"], overlay="Descargando…")


            case "prescan_start":
                self._update_worker_status(upd.session_id, "⟳", _C_DIM, "Pre-escaneando carpeta…", _C_DIM)
                if session:
                    dpg.set_value(session["status"], "Pre-scan…")
                    dpg.configure_item(session["progress"], overlay="Escaneando…")

            case "prescan_file":
                self._update_worker_status(upd.session_id, "⟳", _C_DIM,
                                           upd.filename, _C_DIM)
                if session:
                    progress = upd.files_done / max(upd.files_total, 1)
                    dpg.set_value(session["progress"], progress)
                    dpg.configure_item(session["progress"],
                                       overlay=f"Pre-scan {upd.files_done}/{upd.files_total}")
                    dpg.set_value(session["files"], f"{upd.files_done}/{upd.files_total}")

            case "prescan_done":
                moved = upd.files_done
                total = upd.files_total
                self._update_worker_status(upd.session_id, "⬇", _C_YELLOW,
                                           "Pre-scan listo, descargando…", _C_DIM)
                self._log(
                    f"[{ts}] Pre-scan listo — {moved} indexados ({total - moved} dupl)",
                    color=_C_GREEN,
                )
                if session:
                    dpg.set_value(session["status"], "Descargando")
                    dpg.configure_item(session["status"], color=_C_YELLOW)
                    dpg.set_value(session["progress"], 0.0)
                    dpg.configure_item(session["progress"], overlay="Descargando…")

            case "file_done":
                progress = upd.files_done / max(upd.files_total, 1)
                self._update_worker_status(upd.session_id, "⬇", _C_GREEN,
                                           upd.filename, _C_TEXT)
                self._update_stats("done")
                if session:
                    prev_done  = session["done"]
                    prev_total = session["total"]
                    session["done"]  = upd.files_done
                    session["total"] = upd.files_total
                    self._global_done  += upd.files_done  - prev_done
                    self._global_total += upd.files_total - prev_total
                    dpg.set_value(session["progress"], progress)
                    dpg.configure_item(session["progress"],
                                       overlay=f"{upd.files_done}/{upd.files_total}")
                    dpg.set_value(session["files"], f"{upd.files_done}/{upd.files_total}")
                self._update_global_progress()

            case "file_renamed":
                # upd.filename = nombre original, upd.error = nombre nuevo (cherry-dl)
                self._update_worker_status(upd.session_id, "↩", _C_YELLOW,
                                           f"{upd.filename} → {upd.error}", _C_YELLOW)
                self._update_stats("renamed")
                if session:
                    prev_total = session["total"]
                    session["done"]  = upd.files_done
                    session["total"] = upd.files_total
                    self._global_done  += upd.files_done - session.get("done", 0)
                    self._global_total += upd.files_total - prev_total
                    ratio = upd.files_done / max(upd.files_total, 1)
                    dpg.set_value(session["progress"], ratio)
                    dpg.configure_item(session["progress"],
                                       overlay=f"{upd.files_done}/{upd.files_total}")
                    dpg.set_value(session["files"], f"{upd.files_done}/{upd.files_total}")
                self._update_global_progress()

            case "file_skip":
                # Solo actualizar contadores — no spam al workers panel con cada skip
                self._update_stats("skip")
                if session:
                    prev_total = session["total"]
                    session["total"] = upd.files_total
                    self._global_total += upd.files_total - prev_total
                    shown_done = session["done"]
                    dpg.set_value(session["progress"],
                                  shown_done / max(upd.files_total, 1))
                    dpg.configure_item(session["progress"],
                                       overlay=f"{shown_done}/{upd.files_total}")
                    dpg.set_value(session["files"], f"{shown_done}/{upd.files_total}")
                self._update_global_progress()

            case "error":
                icon, color, label = _error_style(upd.error_kind)
                self._update_worker_status(upd.session_id, icon, color,
                                           f"[{label}] {upd.filename}", color)
                self._update_stats("errors")
                self._log(
                    f"[{ts}] {icon} [{label}] {upd.filename}: {upd.error}",
                    color=color,
                )

            case "completed":
                self._remove_worker_row(upd.session_id)
                if session:
                    prev_done = session["done"]
                    session["done"] = upd.files_done
                    self._global_done += upd.files_done - prev_done
                    dpg.set_value(session["progress"], 1.0)
                    dpg.configure_item(session["progress"], overlay="Completado")
                    dpg.set_value(session["status"], "✓ Listo")
                    dpg.configure_item(session["status"], color=_C_GREEN)
                    dpg.set_value(session["files"],
                                  f"{upd.files_done}/{upd.files_total}")
                self._log(
                    f"[{ts}] ✓ {upd.artist_name} — {upd.files_done} desc  "
                    f"{self._stats['skip']} dupl  {self._stats['errors']} err",
                    color=_C_GREEN,
                )
                self._update_global_progress()
                self._refresh_collections()

            case "fatal":
                self._remove_worker_row(upd.session_id)
                if session:
                    dpg.set_value(session["status"], "✗ Error")
                    dpg.configure_item(session["status"], color=_C_RED)
                    dpg.set_value(session["progress"], 0.0)
                    dpg.configure_item(session["progress"], overlay="Fallido")
                self._log(f"[{ts}] ✗ FATAL: {upd.error}", color=_C_RED)

            case "collections":
                self._update_collections_table(upd.payload or [])

            case "deleted":
                self._log(
                    f"[{ts}] Colección borrada: {upd.artist_name}", color=_C_YELLOW
                )
                self._refresh_collections()

            case "migrate_done":
                if upd.error and not upd.artist_name:
                    # Error fatal en la migración
                    dpg.set_value("cfg_migrate_status", f"✗ Error: {upd.error}")
                    dpg.configure_item("cfg_migrate_status", color=_C_RED)
                    self._log(f"[{ts}] ✗ Migración fallida: {upd.error}", color=_C_RED)
                else:
                    dpg.set_value("cfg_migrate_status", upd.artist_name)
                    dpg.configure_item("cfg_migrate_status", color=_C_GREEN)
                    # Actualizar el campo cfg_download_dir en la UI
                    dpg.set_value("cfg_download_dir", self.config.download_dir)
                    dpg.set_value("cfg_migrate_dest", "")
                    self._log(f"[{ts}] {upd.artist_name}", color=_C_GREEN)
                    if upd.error:
                        self._log(f"       Errores: {upd.error}", color=_C_RED)
                    self._refresh_collections()

            case "log_error":
                self._log(f"[{ts}] ✗ {upd.error}", color=_C_RED)

    # ── Tabla de colecciones ──────────────────────────────────────────────────

    def _update_collections_table(self, data: list[dict]) -> None:
        """Limpia y rellena la tabla de colecciones con datos frescos."""
        # Eliminar filas existentes
        dpg.delete_item("collections_table", children_only=True, slot=1)

        total_files = 0
        total_size  = 0

        for a in data:
            total_files += a.get("total", 0)
            total_size  += a.get("total_size", 0)
            folder_str = str(a.get("folder_path", ""))

            # Capturar 'a' en closure para el callback del botón
            artist_data = dict(a)

            with dpg.table_row(parent="collections_table"):
                dpg.add_text(a.get("site", ""))
                dpg.add_text(a.get("name", ""))
                dpg.add_text(a.get("artist_id", ""), color=_C_DIM)
                dpg.add_text(str(a.get("total", 0)))
                dpg.add_text(_fmt_size(a.get("total_size", 0)))
                dpg.add_text(folder_str, color=_C_DIM)
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="Abrir",
                        width=48,
                        callback=self._on_open_folder,
                        user_data=folder_str,
                    )
                    dpg.add_button(
                        label="Reparar",
                        width=62,
                        callback=self._on_repair,
                        user_data=artist_data,
                    )
                    dpg.add_button(
                        label="Actualizar",
                        width=74,
                        callback=self._on_update,
                        user_data=artist_data,
                    )
                    dpg.add_button(
                        label="Borrar",
                        width=56,
                        callback=self._on_delete_request,
                        user_data=artist_data,
                    )

        status = (
            f"{len(data)} artistas · {total_files:,} archivos · {_fmt_size(total_size)}"
            if data else "Sin colecciones aún."
        )
        dpg.set_value("collections_status", status)

    # ── Abrir carpeta ─────────────────────────────────────────────────────────

    def _on_open_folder(self, sender, app_data, user_data: str) -> None:
        """Abre la carpeta del artista con el gestor de archivos del OS."""
        import subprocess
        folder = Path(user_data)
        if not folder.exists():
            self._log(f"Carpeta no encontrada: {folder}", color=_C_RED)
            return
        # xdg-open → gestor predeterminado del sistema (Dolphin en KDE, Nautilus en GNOME, etc.)
        subprocess.Popen(["xdg-open", str(folder)])

    # ── Reparar colección ─────────────────────────────────────────────────────

    def _on_repair(self, sender, app_data, user_data: dict) -> None:
        """Re-descarga archivos físicamente ausentes del catálogo."""
        session_id = uuid.uuid4().hex[:8]
        name = user_data.get("name", user_data.get("artist_id", "?"))
        workers = self.config.workers

        self._add_session_row(session_id, f"[Repair] {name}")
        self._log(f"Reparando colección: {name}", color=_C_YELLOW)

        self.bridge.submit(
            repair_async(user_data, self.config, workers, self._updates, session_id)
        )

    # ── Actualizar colección ──────────────────────────────────────────────────

    def _on_update(self, sender, app_data, user_data: dict) -> None:
        """Descarga archivos nuevos desde kemono para una colección existente."""
        session_id = uuid.uuid4().hex[:8]
        name = user_data.get("name", user_data.get("artist_id", "?"))
        workers = self.config.workers

        self._add_session_row(session_id, f"[Update] {name}")
        self._log(f"Actualizando colección: {name}", color=_C_YELLOW)

        self.bridge.submit(
            update_async(user_data, self.config, workers, self._updates, session_id)
        )

    # ── Borrar colección ──────────────────────────────────────────────────────

    def _on_delete_request(self, sender, app_data, user_data: dict) -> None:
        """Abre el modal de confirmación con los datos del artista a borrar."""
        self._pending_delete = user_data
        name  = user_data.get("name", user_data.get("artist_id", "?"))
        total = user_data.get("total", 0)
        size  = _fmt_size(user_data.get("total_size", 0))
        msg = (
            f"¿Borrar permanentemente la colección de:\n\n"
            f"  Artista: {name}\n"
            f"  Sitio:   {user_data.get('site', '')}\n"
            f"  Archivos: {total} ({size})\n\n"
            f"Se eliminará la carpeta y su contenido del disco."
        )
        dpg.set_value("modal_delete_msg", msg)

        # Centrar el modal: posicionar primero, luego mostrar
        vw = dpg.get_viewport_width()
        vh = dpg.get_viewport_height()
        dpg.set_item_pos("modal_delete", [(vw - 440) // 2, (vh - 220) // 2])
        dpg.configure_item("modal_delete", show=True)

    def _on_delete_confirm(self) -> None:
        """Ejecuta el borrado tras la confirmación del usuario."""
        dpg.configure_item("modal_delete", show=False)
        if not self._pending_delete:
            return

        a = self._pending_delete
        self._pending_delete = None

        # Despachar borrado al worker async
        self.bridge.submit(self._delete_artist_async(a))

    async def _delete_artist_async(self, a: dict) -> None:
        """Borra la carpeta del artista del disco y lo elimina del índice."""
        import shutil
        from ..index import init_index

        folder = Path(a.get("folder_path", ""))
        name   = a.get("name", a.get("artist_id", "?"))
        ts     = __import__("datetime").datetime.now().strftime("%H:%M:%S")

        # Eliminar del index.db
        try:
            await init_index(INDEX_DB)
            async with __import__("aiosqlite").connect(INDEX_DB) as db:
                await db.execute(
                    """
                    DELETE FROM artists
                    WHERE artist_id = ?
                      AND site_id = (SELECT id FROM sites WHERE name = ?)
                    """,
                    (a.get("artist_id", ""), a.get("site", "")),
                )
                await db.commit()
        except Exception as e:
            self._updates.put(ProgressUpdate(
                session_id="_sys", update_type="log_error",
                error=f"Error borrando de índice: {e}",
            ))
            return

        # Eliminar carpeta del disco
        try:
            if folder.exists():
                shutil.rmtree(folder)
        except Exception as e:
            self._updates.put(ProgressUpdate(
                session_id="_sys", update_type="log_error",
                error=f"Error borrando carpeta: {e}",
            ))
            return

        self._updates.put(ProgressUpdate(
            session_id="_sys", update_type="deleted",
            artist_name=name,
        ))

    # ── Barra de progreso global ───────────────────────────────────────────────

    def _update_global_progress(self) -> None:
        """Recalcula y actualiza la barra de progreso global."""
        total = self._global_total
        done  = self._global_done

        if total == 0:
            dpg.set_value("global_progress", 0.0)
            dpg.configure_item("global_progress", overlay="Sin actividad")
            dpg.set_value("global_label", "")
            return

        ratio = min(done / total, 1.0)
        pct   = int(ratio * 100)
        dpg.set_value("global_progress", ratio)
        dpg.configure_item("global_progress", overlay=f"{pct}%  ({done}/{total})")
        dpg.set_value("global_label", f"{done} de {total} archivos")

    # ── Log de actividad ──────────────────────────────────────────────────────

    def _log(self, msg: str, color: list | None = None) -> None:
        """Agrega una línea al log de actividad y hace scroll al final."""
        color = color or _C_DIM
        tag = f"log_{self._log_count}"
        self._log_count += 1

        dpg.add_text(msg, color=color, parent="log_window", tag=tag)

        # Limpiar líneas más viejas si se excede el máximo
        oldest_idx = self._log_count - _MAX_LOG - 1
        if oldest_idx > 0:
            old_tag = f"log_{oldest_idx}"
            if dpg.does_item_exist(old_tag):
                dpg.delete_item(old_tag)

        # Auto-scroll al fondo
        dpg.set_y_scroll("log_window", dpg.get_y_scroll_max("log_window") + 999)


# ── Entry point ────────────────────────────────────────────────────────────────

def run_app() -> None:
    """Lanza la aplicación GUI."""
    CherryApp().run()
