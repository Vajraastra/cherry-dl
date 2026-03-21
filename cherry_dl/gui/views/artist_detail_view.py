"""
Vista de detalle de un perfil de artista.

Secciones:
  - Header: nombre, sitio, carpeta, stats (archivos, tamaño, última sync)
  - Fuentes: tabla de profile_urls con toggle activo/inactivo + borrar
  - [+ Agregar URL] con detección de sitio automática
  - Controles: workers, filtro de extensiones, carpeta pre-scan
  - Acciones: [⟳ Verificar] [▶ Descargar / Actualizar] [✕ Cancelar]
  - Progreso: barra + log de actividad
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import aiosqlite
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...catalog import add_file, get_stats, hash_exists, init_catalog, next_counter, url_exists
from ...config import INDEX_DB, load_config
from ...index import (
    add_profile_url,
    get_profile,
    set_profile_url_enabled,
    update_profile_ext_filter,
    update_profile_last_checked,
)

# Columnas de la tabla de fuentes
_COL_SITE = 0
_COL_URL  = 1
_COL_FILES = 2
_COL_SYNC = 3
_COL_ENABLED = 4

# Dimensiones del panel de workers
_WORKER_ROW_H   = 30   # px por fila de worker
_WORKER_HDR_H   = 28   # px para la fila de cabeceras
_WORKER_MAX_VIS = 5    # máximo de workers visibles sin scroll


class ArtistDetailView(QWidget):
    """Vista de detalle de un perfil con controles de descarga."""

    def __init__(self, nav: Callable, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav
        self._profile: dict | None = None
        self._download_task: asyncio.Task | None = None
        self._setup_ui()

    # ── Construcción de UI ─────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 0)
        root.setSpacing(12)

        # ── Header ─────────────────────────────────────────────────────────
        header = QHBoxLayout()
        btn_back = QPushButton("← Volver")
        btn_back.clicked.connect(lambda: self._nav("profiles"))

        self._lbl_name = QLabel("—")
        self._lbl_name.setObjectName("lbl_title")
        self._lbl_meta = QLabel("")
        self._lbl_meta.setObjectName("lbl_subtitle")

        header.addWidget(btn_back)
        header.addSpacing(12)
        vbox = QVBoxLayout()
        vbox.setSpacing(2)
        vbox.addWidget(self._lbl_name)
        vbox.addWidget(self._lbl_meta)
        header.addLayout(vbox)
        header.addStretch()
        root.addLayout(header)

        sep1 = QFrame()
        sep1.setObjectName("separator")
        sep1.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep1)

        # ── Fuentes ────────────────────────────────────────────────────────
        lbl_sources = QLabel("FUENTES")
        lbl_sources.setObjectName("lbl_section")
        root.addWidget(lbl_sources)

        self._sources_table = QTableWidget()
        self._sources_table.setColumnCount(5)
        self._sources_table.setHorizontalHeaderLabels(
            ["Sitio", "URL / ID", "Archivos", "Última sync", "Activo"]
        )
        self._sources_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._sources_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._sources_table.setAlternatingRowColors(True)
        self._sources_table.setShowGrid(False)
        self._sources_table.verticalHeader().setVisible(False)
        self._sources_table.setMaximumHeight(150)

        hdr = self._sources_table.horizontalHeader()
        hdr.setSectionResizeMode(_COL_SITE, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_COL_URL, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(_COL_FILES, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_COL_SYNC, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_COL_ENABLED, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self._sources_table)

        # Botones de fuentes
        src_btns = QHBoxLayout()
        self._btn_add_url = QPushButton("+ Agregar URL")
        self._btn_del_url = QPushButton("Eliminar seleccionada")
        self._btn_del_url.setObjectName("btn_danger")
        src_btns.addWidget(self._btn_add_url)
        src_btns.addWidget(self._btn_del_url)
        src_btns.addStretch()
        root.addLayout(src_btns)

        # Fila de nueva URL (oculta por defecto)
        self._add_url_widget = QWidget()
        add_url_row = QHBoxLayout(self._add_url_widget)
        add_url_row.setContentsMargins(0, 0, 0, 0)
        self._new_url_input = QLineEdit()
        self._new_url_input.setPlaceholderText("URL del artista en otro servicio")
        self._btn_confirm_url = QPushButton("Agregar")
        self._btn_confirm_url.setObjectName("btn_primary")
        self._btn_cancel_url = QPushButton("Cancelar")
        add_url_row.addWidget(self._new_url_input)
        add_url_row.addWidget(self._btn_confirm_url)
        add_url_row.addWidget(self._btn_cancel_url)
        self._add_url_widget.setVisible(False)
        root.addWidget(self._add_url_widget)

        sep2 = QFrame()
        sep2.setObjectName("separator")
        sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)

        # ── Controles ──────────────────────────────────────────────────────
        controls = QHBoxLayout()
        controls.setSpacing(16)

        controls.addWidget(QLabel("Workers:"))
        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 20)
        self._workers_spin.setValue(load_config().workers)
        self._workers_spin.setFixedWidth(60)
        controls.addWidget(self._workers_spin)

        controls.addWidget(QLabel("Filtro ext:"))
        self._ext_filter = QLineEdit()
        self._ext_filter.setPlaceholderText("jpg,png,mp4  (vacío = todos)")
        self._ext_filter.setMaximumWidth(200)
        controls.addWidget(self._ext_filter)

        controls.addWidget(QLabel("Pre-scan:"))
        self._prescan_input = QLineEdit()
        self._prescan_input.setPlaceholderText("Carpeta con archivos existentes (opcional)")
        controls.addWidget(self._prescan_input)
        controls.addStretch()
        root.addLayout(controls)

        # ── Acciones ───────────────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.addStretch()
        self._btn_prescan = QPushButton("⬡ Pre-scan")
        self._btn_prescan.setToolTip(
            "Escanea la carpeta indicada en Pre-scan, calcula hashes,\n"
            "renombra y mueve los archivos a la carpeta del artista."
        )
        self._btn_dedup = QPushButton("⊘ Deduplicar")
        self._btn_dedup.setToolTip(
            "Busca archivos duplicados (mismo hash, distinto nombre) en la\n"
            "carpeta del artista y elimina las copias extra."
        )
        self._btn_check = QPushButton("⟳ Verificar actualizaciones")
        self._btn_download = QPushButton("▶ Descargar / Actualizar")
        self._btn_download.setObjectName("btn_primary")
        self._btn_cancel = QPushButton("✕ Cancelar")
        self._btn_cancel.setObjectName("btn_danger")
        self._btn_cancel.setEnabled(False)
        actions.addWidget(self._btn_prescan)
        actions.addWidget(self._btn_dedup)
        actions.addWidget(self._btn_check)
        actions.addWidget(self._btn_download)
        actions.addWidget(self._btn_cancel)
        root.addLayout(actions)

        # ── Panel de workers (scrollable, siempre visible) ─────────────────
        # Contenedor interno con los widgets de cada worker
        self._worker_panel = QWidget()
        self._worker_panel_layout = QVBoxLayout(self._worker_panel)
        self._worker_panel_layout.setContentsMargins(0, 0, 0, 0)
        self._worker_panel_layout.setSpacing(0)

        # Fila de cabeceras
        wh = QHBoxLayout()
        wh.setContentsMargins(4, 2, 4, 2)
        for txt, w in [
            ("W", 28), ("Estado", 60), ("Archivo", None),
            ("Progreso", 160), ("Vel.", 80),
        ]:
            lbl = QLabel(txt)
            lbl.setObjectName("lbl_section")
            if w:
                lbl.setFixedWidth(w)
            else:
                lbl.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Preferred,
                )
            wh.addWidget(lbl)
        wh_widget = QWidget()
        wh_widget.setLayout(wh)
        wh_widget.setFixedHeight(_WORKER_HDR_H)
        self._worker_panel_layout.addWidget(wh_widget)
        self._worker_panel_layout.addStretch()   # empuja rows hacia arriba
        self._worker_rows: list[dict] = []

        # Scroll area que contiene el panel — altura fija (max 5 workers)
        self._worker_scroll = QScrollArea()
        self._worker_scroll.setWidget(self._worker_panel)
        self._worker_scroll.setWidgetResizable(True)
        self._worker_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._worker_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._worker_scroll.setFixedHeight(
            _WORKER_HDR_H + _WORKER_MAX_VIS * _WORKER_ROW_H
        )
        root.addWidget(self._worker_scroll)

        # ── Log de actividad ───────────────────────────────────────────────
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText("El log de actividad aparecerá aquí…")
        self._log.setMinimumHeight(100)
        # Sin max height — se expande para ocupar el espacio disponible
        root.addWidget(self._log, stretch=1)

        # ── Barra de estado ────────────────────────────────────────────────
        bar = QFrame()
        bar.setObjectName("activity_bar")
        bar.setFrameShape(QFrame.Shape.NoFrame)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(4, 6, 4, 6)
        bar_layout.setSpacing(8)

        # Semáforo de estado (●)
        self._lbl_semaphore = QLabel("●")
        self._lbl_semaphore.setFixedWidth(22)
        self._lbl_semaphore.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_semaphore.setStyleSheet("color: #808080; font-size: 18px;")
        self._lbl_semaphore.setToolTip("Detenido")

        self._lbl_status = QLabel("Listo")
        self._lbl_status.setObjectName("lbl_status")
        self._lbl_progress = QLabel("")
        self._lbl_progress.setObjectName("lbl_subtitle")
        self._lbl_progress.setAlignment(Qt.AlignmentFlag.AlignRight)
        bar_layout.addWidget(self._lbl_semaphore)
        bar_layout.addWidget(self._lbl_status)
        bar_layout.addStretch()
        bar_layout.addWidget(self._lbl_progress)
        root.addWidget(bar)

        # ── Señales ────────────────────────────────────────────────────────
        self._btn_prescan.clicked.connect(self._on_prescan)
        self._btn_dedup.clicked.connect(self._on_deduplicate)
        self._btn_check.clicked.connect(self._on_check)
        self._btn_download.clicked.connect(self._on_download)
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_add_url.clicked.connect(self._on_show_add_url)
        self._btn_del_url.clicked.connect(self._on_del_url)
        self._btn_confirm_url.clicked.connect(self._on_confirm_add_url)
        self._btn_cancel_url.clicked.connect(lambda: self._add_url_widget.setVisible(False))
        self._ext_filter.editingFinished.connect(self._on_ext_filter_changed)

    # ── API pública ────────────────────────────────────────────────────────────

    def load_profile(
        self,
        profile_id: int,
        *,
        auto_download: bool = False,
        auto_check: bool = False,
        prescan_path: str | None = None,
    ) -> None:
        """Carga el perfil y actualiza la UI."""
        self._profile = None
        self._log.clear()
        self._lbl_status.setText("Cargando…")
        asyncio.ensure_future(
            self._load_async(
                profile_id,
                auto_download=auto_download,
                auto_check=auto_check,
                prescan_path=prescan_path,
            )
        )

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_deduplicate(self) -> None:
        self._download_task = asyncio.ensure_future(self._do_deduplicate())

    def _on_prescan(self) -> None:
        prescan_str = self._prescan_input.text().strip()
        if not prescan_str:
            self._lbl_status.setText("Indica una carpeta en el campo Pre-scan.")
            return
        prescan_path = Path(prescan_str)
        if not prescan_path.is_dir():
            self._lbl_status.setText(f"Carpeta no encontrada: {prescan_path}")
            return
        self._download_task = asyncio.ensure_future(
            self._run_prescan(prescan_path)
        )

    async def _run_prescan(self, prescan_path: Path) -> None:
        self._set_busy(True)
        self._log.clear()
        self._lbl_status.setText("Iniciando pre-scan…")
        try:
            await self._do_prescan(prescan_path)
            self._lbl_status.setText("Pre-scan completado.")
            self._set_status_light("done")
        except asyncio.CancelledError:
            self._lbl_status.setText("Pre-scan cancelado.")
            self._set_status_light("cancelled")
        except Exception as exc:
            self._lbl_status.setText(f"Error en pre-scan: {exc}")
            self._set_status_light("error")
        finally:
            self._download_task = None
            self._set_busy(False)

    def _on_check(self) -> None:
        self._download_task = asyncio.ensure_future(self._do_check())

    def _on_download(self) -> None:
        self._download_task = asyncio.ensure_future(self._do_download())

    def _on_cancel(self) -> None:
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()

    def _on_show_add_url(self) -> None:
        self._new_url_input.clear()
        self._add_url_widget.setVisible(True)
        self._new_url_input.setFocus()

    def _on_confirm_add_url(self) -> None:
        self._btn_confirm_url.setEnabled(False)
        task = asyncio.ensure_future(self._add_url_async())
        task.add_done_callback(lambda _: self._btn_confirm_url.setEnabled(True))

    def _on_del_url(self) -> None:
        self._btn_del_url.setEnabled(False)
        task = asyncio.ensure_future(self._del_url_async())
        task.add_done_callback(lambda _: self._btn_del_url.setEnabled(True))

    def _on_ext_filter_changed(self) -> None:
        if not self._profile:
            return
        task = asyncio.ensure_future(
            update_profile_ext_filter(INDEX_DB, self._profile["id"], self._ext_filter.text())
        )
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    # ── Helpers de UI ──────────────────────────────────────────────────────────

    def _append_log(self, text: str) -> None:
        self._log.appendPlainText(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _populate_sources(self, urls: list[dict]) -> None:
        self._sources_table.setRowCount(len(urls))
        for r, u in enumerate(urls):
            self._sources_table.setItem(r, _COL_SITE, QTableWidgetItem(u["site"]))
            display = u["url"] or f"(migrado — ID: {u['artist_id'] or '?'})"
            self._sources_table.setItem(r, _COL_URL, QTableWidgetItem(display))
            self._sources_table.setItem(r, _COL_FILES, QTableWidgetItem(str(u["file_count"])))
            synced = u["last_synced"] or "Nunca"
            self._sources_table.setItem(r, _COL_SYNC, QTableWidgetItem(synced[:10]))

            chk = QCheckBox()
            chk.setChecked(u["enabled"])
            chk.setStyleSheet("margin-left: 12px;")
            uid = u["id"]
            chk.stateChanged.connect(
                lambda state, url_id=uid: asyncio.ensure_future(
                    self._toggle_url(url_id, bool(state))
                )
            )
            self._sources_table.setCellWidget(r, _COL_ENABLED, chk)

            # Guardar url_id en columna Sitio para poder eliminarlo
            item_site = self._sources_table.item(r, _COL_SITE)
            if item_site:
                item_site.setData(Qt.ItemDataRole.UserRole, uid)

        self._sources_table.resizeRowsToContents()

    def _init_worker_slots(self, n: int) -> None:
        """Crea/recrea las filas de worker en el panel y ajusta la altura del scroll."""
        # Limpiar filas existentes (insertar antes del stretch final)
        for row_data in self._worker_rows:
            row_data["widget"].setParent(None)
        self._worker_rows.clear()

        # Insertar nuevas filas ANTES del stretch (último item del layout)
        stretch_idx = self._worker_panel_layout.count() - 1

        for i in range(n):
            row_widget = QWidget()
            row_widget.setFixedHeight(_WORKER_ROW_H)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(6)

            lbl_id = QLabel(f"W{i + 1}")
            lbl_id.setFixedWidth(28)
            lbl_id.setObjectName("lbl_subtitle")

            lbl_status = QLabel("—")
            lbl_status.setFixedWidth(60)
            lbl_status.setObjectName("lbl_status")

            lbl_file = QLabel("")
            lbl_file.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
            lbl_file.setObjectName("lbl_subtitle")

            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setFixedWidth(160)
            bar.setFixedHeight(14)
            bar.setTextVisible(False)

            lbl_speed = QLabel("")
            lbl_speed.setFixedWidth(80)
            lbl_speed.setObjectName("lbl_status")
            lbl_speed.setAlignment(Qt.AlignmentFlag.AlignRight)

            row_layout.addWidget(lbl_id)
            row_layout.addWidget(lbl_status)
            row_layout.addWidget(lbl_file)
            row_layout.addWidget(bar)
            row_layout.addWidget(lbl_speed)

            # Insertar antes del stretch para mantener rows al inicio
            self._worker_panel_layout.insertWidget(stretch_idx + i, row_widget)
            self._worker_rows.append({
                "widget": row_widget,
                "status": lbl_status,
                "file": lbl_file,
                "bar": bar,
                "speed": lbl_speed,
                "start_time": 0.0,
                "bytes_done": 0,
            })

        # Ajustar altura del scroll area: muestra hasta _WORKER_MAX_VIS rows
        visible = min(n, _WORKER_MAX_VIS)
        self._worker_scroll.setFixedHeight(
            _WORKER_HDR_H + visible * _WORKER_ROW_H
        )

    def _worker_start(self, slot: int, filename: str) -> None:
        if slot >= len(self._worker_rows):
            return
        row = self._worker_rows[slot]
        row["start_time"] = time.monotonic()
        row["bytes_done"] = 0
        row["status"].setText("↓")
        row["file"].setText(filename[:55])
        row["bar"].setRange(0, 100)
        row["bar"].setValue(0)
        row["speed"].setText("")

    def _worker_progress(self, slot: int, done: int, total: int) -> None:
        if slot >= len(self._worker_rows):
            return
        row = self._worker_rows[slot]
        row["bytes_done"] = done
        elapsed = time.monotonic() - row["start_time"]
        if elapsed > 0.1:
            speed = done / elapsed
            row["speed"].setText(_fmt_speed(speed))
        if total > 0:
            row["bar"].setRange(0, 100)
            row["bar"].setValue(int(done * 100 / total))
        else:
            row["bar"].setRange(0, 0)  # indeterminado

    def _worker_done(self, slot: int, filename: str, icon: str = "✓") -> None:
        if slot >= len(self._worker_rows):
            return
        row = self._worker_rows[slot]
        row["status"].setText(icon)
        row["file"].setText(filename[:55])
        row["bar"].setRange(0, 100)
        row["bar"].setValue(100)
        row["speed"].setText("")

    def _worker_idle(self, slot: int) -> None:
        if slot >= len(self._worker_rows):
            return
        row = self._worker_rows[slot]
        row["status"].setText("—")
        row["file"].setText("")
        row["bar"].setRange(0, 100)
        row["bar"].setValue(0)
        row["speed"].setText("")

    def _update_counters(
        self, dl: int, sk: int, err: int, def_: int
    ) -> None:
        self._lbl_progress.setText(
            f"↓ {dl}  skip {sk}  ✗ {err}  ⏭ {def_}"
        )

    def _set_status_light(self, state: str) -> None:
        """Actualiza el semáforo de estado en la barra inferior.

        Estados:
          idle      — gris   — proceso detenido / sin actividad
          running   — verde  — proceso en curso
          cancelled — amarillo — detenido por el usuario
          error     — rojo   — detenido por error
          done      — azul   — completado con éxito
        """
        _STATES = {
            "idle":      ("#808080", "Detenido"),
            "running":   ("#00cc55", "Corriendo…"),
            "cancelled": ("#ffaa00", "Cancelado por usuario"),
            "error":     ("#ff4444", "Detenido por error"),
            "done":      ("#4499ff", "Completado"),
        }
        color, tip = _STATES.get(state, _STATES["idle"])
        self._lbl_semaphore.setStyleSheet(
            f"color: {color}; font-size: 18px;"
        )
        self._lbl_semaphore.setToolTip(tip)

    def _refresh_source_row(self, url_id: int, file_count: int) -> None:
        """Actualiza archivos y última sync de una fila de la tabla de fuentes."""
        import datetime
        today = datetime.date.today().isoformat()
        for row in range(self._sources_table.rowCount()):
            item = self._sources_table.item(row, _COL_SITE)
            if item and item.data(Qt.ItemDataRole.UserRole) == url_id:
                self._sources_table.setItem(
                    row, _COL_FILES, QTableWidgetItem(str(file_count))
                )
                self._sources_table.setItem(
                    row, _COL_SYNC, QTableWidgetItem(today)
                )
                break

    def _set_busy(self, busy: bool) -> None:
        self._btn_prescan.setEnabled(not busy)
        self._btn_dedup.setEnabled(not busy)
        self._btn_check.setEnabled(not busy)
        self._btn_download.setEnabled(not busy)
        self._btn_cancel.setEnabled(busy)
        if busy:
            self._set_status_light("running")

    # ── Async ──────────────────────────────────────────────────────────────────

    async def _load_async(
        self,
        profile_id: int,
        auto_download: bool = False,
        auto_check: bool = False,
        prescan_path: str | None = None,
    ) -> None:
        try:
            profile = await get_profile(INDEX_DB, profile_id)
            if not profile:
                self._lbl_status.setText(f"Perfil #{profile_id} no encontrado.")
                return
            self._profile = profile
            self._lbl_name.setText(profile["display_name"])

            folder = Path(profile["folder_path"])
            stats = await get_stats(folder) if folder.exists() else {"total": 0, "total_size": 0}
            last = profile["last_checked"]
            last_str = last[:10] if last else "Nunca"
            self._lbl_meta.setText(
                f"{profile['primary_site'].upper()}  ·  {stats['total']:,} archivos"
                f"  ·  {_fmt_size(stats['total_size'])}  ·  última sync: {last_str}"
            )
            cfg_workers = load_config().workers
            self._workers_spin.setValue(cfg_workers)
            # Inicializar el panel de workers con la cuenta por defecto
            # (idle — muestra las filas en estado "—")
            self._init_worker_slots(cfg_workers)
            self._populate_sources(profile["urls"])
            self._ext_filter.setText(profile.get("ext_filter", ""))
            self._lbl_status.setText("Listo")

            if prescan_path:
                self._prescan_input.setText(prescan_path)
            if auto_download:
                QTimer.singleShot(200, self._on_download)
            elif auto_check:
                QTimer.singleShot(200, self._on_check)
        except Exception as exc:
            self._lbl_status.setText(f"Error al cargar: {exc}")

    async def _toggle_url(self, url_id: int, enabled: bool) -> None:
        try:
            await set_profile_url_enabled(INDEX_DB, url_id, enabled)
            if self._profile:
                await self._load_async(self._profile["id"])
        except Exception as exc:
            self._lbl_status.setText(f"Error: {exc}")

    async def _add_url_async(self) -> None:
        url = self._new_url_input.text().strip()
        if not url or not self._profile:
            return
        self._add_url_widget.setVisible(False)
        try:
            from ...templates._registry import find_template
            cls = find_template(url)
            site = cls.name if cls else "unknown"
            await add_profile_url(
                db_path=INDEX_DB,
                profile_id=self._profile["id"],
                url=url,
                site=site,
            )
            await self._load_async(self._profile["id"])
        except Exception as exc:
            self._lbl_status.setText(f"Error: {exc}")

    async def _del_url_async(self) -> None:
        row = self._sources_table.currentRow()
        if row < 0 or not self._profile:
            return
        item = self._sources_table.item(row, _COL_SITE)
        if not item:
            return
        url_id = item.data(Qt.ItemDataRole.UserRole)
        if url_id is None:
            return
        try:
            async with aiosqlite.connect(INDEX_DB) as db:
                await db.execute("DELETE FROM profile_urls WHERE id = ?", (url_id,))
                await db.commit()
            await self._load_async(self._profile["id"])
        except Exception as exc:
            self._lbl_status.setText(f"Error: {exc}")

    async def _do_check(self) -> None:
        """Itera la API para contar archivos nuevos sin descargar nada."""
        if not self._profile:
            return
        self._set_busy(True)
        self._log.clear()
        self._lbl_status.setText("Verificando…")
        total_new = 0

        try:
            from ...engine import DownloadEngine
            from ...templates._registry import get_template

            config = load_config()
            folder = Path(self._profile["folder_path"])
            await init_catalog(folder)

            async with DownloadEngine(config) as engine:
                for pu in self._profile["urls"]:
                    if not pu["enabled"] or not pu["url"]:
                        continue
                    template = get_template(pu["url"], engine)
                    if not template:
                        self._append_log(f"Sin template para {pu['url']}")
                        continue
                    artist_info = await template.get_artist_info(pu["url"])
                    self._append_log(f"⟳ {artist_info.name} ({pu['site']})…")
                    count_new = 0
                    async for file_info in template.iter_files(artist_info):
                        if file_info.remote_hash and await hash_exists(folder, file_info.remote_hash):
                            continue
                        if await url_exists(folder, file_info.url):
                            continue
                        count_new += 1
                        self._lbl_progress.setText(f"{total_new + count_new} nuevos")
                    self._append_log(f"  → {count_new} archivos nuevos")
                    total_new += count_new

            await update_profile_last_checked(INDEX_DB, self._profile["id"])
            await self._load_async(self._profile["id"])
            self._lbl_status.setText(
                f"Verificación completa — {total_new} archivos nuevos"
                if total_new else "Todo al día — sin archivos nuevos"
            )
            self._set_status_light("done")
        except asyncio.CancelledError:
            self._lbl_status.setText("Verificación cancelada.")
            self._set_status_light("cancelled")
        except Exception as exc:
            self._lbl_status.setText(f"Error: {exc}")
            self._append_log(f"✗ Error: {exc}")
            self._set_status_light("error")
        finally:
            self._set_busy(False)

    async def _do_download(self) -> None:
        """Descarga todos los archivos nuevos de las fuentes activas."""
        if not self._profile:
            return
        self._set_busy(True)
        self._log.clear()
        self._lbl_status.setText("Iniciando descarga…")

        # Contadores mutables accesibles desde closures anidadas
        downloaded_ref = [0]
        skipped_ref = [0]
        errors_ref = [0]
        deferred_count_ref = [0]
        folder = Path(self._profile["folder_path"])

        try:
            from ...engine import DownloadEngine, ErrorKind
            from ...gui.bridge import (
                _build_local_hash_map,
                _parse_ext_filter,
                _passes_ext_filter,
                build_filename,
            )
            from ...index import (
                get_or_create_artist,
                get_or_create_site,
                init_index,
                update_profile_url_sync,
            )
            from ...templates._registry import get_template

            config = load_config()
            workers = self._workers_spin.value()
            ext_filter = _parse_ext_filter(self._ext_filter.text())

            # Inicializar panel de workers
            self._init_worker_slots(workers)

            # Cola diferida: archivos que fallaron con error temporal
            deferred: list[tuple] = []

            async with DownloadEngine(config, workers=workers) as engine:
                for pu in self._profile["urls"]:
                    if not pu["enabled"] or not pu["url"]:
                        continue

                    template = get_template(pu["url"], engine)
                    if not template:
                        self._append_log(
                            f"✗ Sin template para: {pu['url']}"
                        )
                        continue

                    artist_info = await template.get_artist_info(pu["url"])
                    self._append_log(
                        f"▶ {artist_info.name} ({pu['site']})"
                    )

                    folder.mkdir(parents=True, exist_ok=True)
                    await init_catalog(folder)
                    await init_index(INDEX_DB)
                    site_id = await get_or_create_site(
                        INDEX_DB, artist_info.site
                    )
                    await get_or_create_artist(
                        db_path=INDEX_DB,
                        site_id=site_id,
                        artist_id=artist_info.artist_id,
                        name=artist_info.name,
                        folder_path=folder,
                    )

                    # Registrar artist_id en profile_url para que la
                    # migración de init_index no cree una entrada duplicada
                    # con url=NULL en la próxima ejecución.
                    await update_profile_url_sync(
                        INDEX_DB,
                        pu["id"],
                        artist_id=artist_info.artist_id,
                    )

                    # Snapshot del contador de descargas antes de esta fuente
                    dl_before = downloaded_ref[0]

                    local_hashes = await _build_local_hash_map(folder)
                    file_queue: asyncio.Queue = asyncio.Queue(
                        maxsize=workers * 3
                    )

                    # Sets de hashes/URLs ya encolados en esta sesión.
                    # Evita la race condition donde el mismo archivo aparece
                    # varias veces en la API antes de que el worker lo descargue
                    # y lo añada al catálogo (ambas instancias pasarían el
                    # hash_exists/url_exists del catálogo y se descargarían dos veces).
                    # ── Repartidor ─────────────────────────────────────────
                    # Única responsabilidad: garantizar que cada URL llega
                    # a lo sumo una vez a la cola (dedup de API).
                    # Las verificaciones de catálogo las hacen los workers.
                    seen_urls: set[str] = set()

                    async def producer() -> None:
                        try:
                            async for fi in template.iter_files(artist_info):
                                # Filtro de extensión
                                if not _passes_ext_filter(
                                    fi.filename, ext_filter, not ext_filter
                                ):
                                    skipped_ref[0] += 1
                                    self._append_log(
                                        f"  — {fi.filename[:60]}"
                                        "  [filtro ext]"
                                    )
                                    continue

                                # Dedup de URL (misma URL en múltiples posts)
                                if fi.url in seen_urls:
                                    skipped_ref[0] += 1
                                    self._append_log(
                                        f"  — {fi.filename[:60]}"
                                        "  [URL duplicada en API]"
                                    )
                                    continue

                                seen_urls.add(fi.url)
                                # Timeout de 120s en el put: si la cola lleva
                                # demasiado tiempo llena es señal de que los
                                # workers están colgados (deadlock). Levantar
                                # TimeoutError rompe el ciclo y termina la sesión
                                # con un mensaje claro en lugar de congelarse.
                                await asyncio.wait_for(
                                    file_queue.put(fi), timeout=120.0
                                )
                        finally:
                            # Señalizar fin a cada worker.
                            # Si el productor es cerrado por GC o por un
                            # event loop distinto (qasync reiniciado tras
                            # cancelación), el await puede fallar con
                            # RuntimeError → salir silenciosamente; los
                            # workers ya están cancelados por asyncio.gather.
                            for _ in range(workers):
                                try:
                                    await file_queue.put(None)
                                except RuntimeError:
                                    break

                    # ── Workers ────────────────────────────────────────────
                    # Set compartido entre todos los workers para evitar que
                    # dos workers descarguen simultáneamente archivos con el
                    # mismo hash (mismo contenido, distinta URL).
                    #
                    # La verificación es atómica en asyncio: entre el
                    # `await file_queue.get()` y el `.add()` no hay ningún
                    # punto de yield, por lo que ningún otro worker puede
                    # correr entre esas dos líneas.
                    in_progress_hashes: set[str] = set()

                    async def worker_task(slot_id: int) -> None:
                        while True:
                            fi = await file_queue.get()
                            if fi is None:
                                self._worker_idle(slot_id)
                                break

                            # ── Dedup entre workers (atómico) ──────────────
                            # Si otro worker ya está procesando el mismo hash
                            # (contenido idéntico, URL diferente) → saltar.
                            if fi.remote_hash and fi.remote_hash in in_progress_hashes:
                                skipped_ref[0] += 1
                                self._append_log(
                                    f"  — {fi.filename[:60]}"
                                    "  [hash en progreso]"
                                )
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                                continue

                            # Registrar hash como en progreso (sin await entre
                            # el get() anterior y este add → operación atómica)
                            if fi.remote_hash:
                                in_progress_hashes.add(fi.remote_hash)

                            try:
                                # ── Verificaciones de catálogo ─────────────
                                if await url_exists(folder, fi.url):
                                    skipped_ref[0] += 1
                                    self._append_log(
                                        f"  — {fi.filename[:60]}"
                                        "  [URL en catálogo]"
                                    )
                                    self._update_counters(
                                        downloaded_ref[0], skipped_ref[0],
                                        errors_ref[0], deferred_count_ref[0],
                                    )
                                    continue

                                if fi.remote_hash and await hash_exists(
                                    folder, fi.remote_hash
                                ):
                                    skipped_ref[0] += 1
                                    self._append_log(
                                        f"  — {fi.filename[:60]}"
                                        "  [hash en catálogo]"
                                    )
                                    self._update_counters(
                                        downloaded_ref[0], skipped_ref[0],
                                        errors_ref[0], deferred_count_ref[0],
                                    )
                                    continue

                                # ── Descarga ───────────────────────────────
                                counter = await next_counter(folder)
                                final_name = build_filename(
                                    artist_info.name, counter, fi.filename
                                )
                                self._worker_start(slot_id, fi.filename)

                                def make_cb(
                                    s: int,
                                ) -> Callable[[int, int], None]:
                                    # Throttle: actualizar la UI a 4 Hz máximo.
                                    # Cada chunk es ~65 KB; a 10 MB/s eso son
                                    # ~160 llamadas/s por worker → bloquea el
                                    # event loop de qasync con operaciones Qt.
                                    # Con el throttle solo se actualizan los
                                    # widgets cada 250 ms independientemente de
                                    # la velocidad de descarga.
                                    _last: list[float] = [0.0]

                                    def cb(done: int, total: int) -> None:
                                        now = time.monotonic()
                                        if now - _last[0] < 0.25:
                                            return
                                        _last[0] = now
                                        self._worker_progress(s, done, total)
                                    return cb

                                # Timeout total por archivo: aunque el servidor
                                # mande bytes esporádicos (reseteando el stall
                                # por chunk), un archivo no puede tardar más de
                                # 10 minutos en total antes de ser diferido.
                                try:
                                    result = await asyncio.wait_for(
                                        engine.download(
                                            url=fi.url,
                                            dest_dir=folder,
                                            filename=final_name,
                                            on_progress=make_cb(slot_id),
                                        ),
                                        timeout=600.0,  # 10 min máx por archivo
                                    )
                                except asyncio.TimeoutError:
                                    self._worker_done(slot_id, fi.filename, "⏸")
                                    self._append_log(
                                        f"  ⏸ {fi.filename[:55]}"
                                        "  [timeout total — diferido]"
                                    )
                                    deferred.append((fi, artist_info, folder))
                                    deferred_count_ref[0] += 1
                                    self._update_counters(
                                        downloaded_ref[0], skipped_ref[0],
                                        errors_ref[0], deferred_count_ref[0],
                                    )
                                    continue

                                if not result.ok:
                                    if result.error_kind in ErrorKind.DEFERRABLE:
                                        deferred.append(
                                            (fi, artist_info, folder)
                                        )
                                        self._worker_done(
                                            slot_id, fi.filename, "⏸"
                                        )
                                        self._append_log(
                                            f"  ⏸ {fi.filename[:55]}"
                                            f"  [{result.error_kind}]"
                                        )
                                    else:
                                        errors_ref[0] += 1
                                        self._worker_done(
                                            slot_id, fi.filename, "✗"
                                        )
                                        self._append_log(
                                            f"  ✗ {fi.filename[:45]}"
                                            f":  {result.error}"
                                        )
                                    self._update_counters(
                                        downloaded_ref[0], skipped_ref[0],
                                        errors_ref[0], deferred_count_ref[0],
                                    )
                                    continue

                                if result.file_hash is None:
                                    # No debería ocurrir: engine garantiza hash
                                    # en descargas exitosas. Si pasa, es un bug.
                                    errors_ref[0] += 1
                                    self._append_log(
                                        f"  ✗ {fi.filename[:50]}"
                                        ":  bug interno — hash nulo tras descarga"
                                    )
                                    if result.dest and result.dest.exists():
                                        result.dest.unlink()
                                    continue

                                # ── Catalogar resultado ────────────────────
                                if result.file_hash in local_hashes:
                                    # Existe en disco con otro nombre → renombrar.
                                    # new_path == result.dest (ambos = folder/final_name).
                                    old_path = local_hashes[result.file_hash]
                                    new_path = folder / final_name
                                    try:
                                        old_path.rename(new_path)
                                        local_hashes[result.file_hash] = new_path
                                        # Rename OK: result.dest es copia redundante
                                        if result.dest and result.dest.exists():
                                            result.dest.unlink()
                                        renamed = True
                                    except OSError:
                                        # Rename falló (ej: destino ya existe = result.dest).
                                        # result.dest ya está en la ubicación correcta;
                                        # old_path es el duplicado → intentar borrarlo.
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
                                        url_source=fi.url,
                                        file_size=result.file_size,
                                        counter=counter,
                                    )
                                    downloaded_ref[0] += 1
                                    if renamed:
                                        self._worker_done(slot_id, final_name, "↷")
                                        self._append_log(
                                            f"  ↷ {old_path.name}"
                                            f"  →  {final_name}"
                                            "  [renombrado]"
                                        )
                                    else:
                                        self._worker_done(slot_id, final_name, "✓")
                                        self._append_log(
                                            f"  ✓ {fi.filename[:40]}"
                                            f"  →  {final_name}"
                                        )

                                else:
                                    # Archivo nuevo
                                    await add_file(
                                        artist_dir=folder,
                                        file_hash=result.file_hash,
                                        filename=final_name,
                                        url_source=fi.url,
                                        file_size=result.file_size,
                                        counter=counter,
                                    )
                                    local_hashes[result.file_hash] = result.dest
                                    downloaded_ref[0] += 1
                                    self._worker_done(slot_id, final_name, "✓")
                                    self._append_log(
                                        f"  ✓ {fi.filename[:40]}"
                                        f"  →  {final_name}"
                                    )

                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )

                            except asyncio.CancelledError:
                                raise  # propagar cancelación normalmente
                            except Exception as _exc:
                                # Capturar cualquier excepción inesperada
                                # (OSError de disco, ProtocolError de red, etc.)
                                # para que UN archivo que falla no cancele
                                # los otros workers en ejecución paralela.
                                errors_ref[0] += 1
                                import traceback as _tb
                                _msg = f"{type(_exc).__name__}: {_exc}"
                                self._worker_done(slot_id, fi.filename, "✗")
                                self._append_log(
                                    f"  ✗ {fi.filename[:45]}  [excepción] {_msg}"
                                )
                                self._append_log(
                                    "    " + _tb.format_exc().splitlines()[-1]
                                )
                                self._update_counters(
                                    downloaded_ref[0], skipped_ref[0],
                                    errors_ref[0], deferred_count_ref[0],
                                )
                            finally:
                                # Liberar hash para que otros workers puedan
                                # procesar contenido idéntico si fuera necesario
                                if fi.remote_hash:
                                    in_progress_hashes.discard(fi.remote_hash)

                    # Crear Tasks explícitas para que asyncio pueda
                    # limpiarlas correctamente aunque el padre sea cancelado.
                    # Con raw coroutines en gather, si el padre se cancela
                    # antes de que gather envuelva las coroutines, quedan
                    # como objetos huérfanos y Python imprime
                    # "Exception ignored in: <coroutine object>".
                    _all_tasks = [
                        asyncio.create_task(producer(), name="producer"),
                        *[
                            asyncio.create_task(worker_task(i), name=f"worker-{i}")
                            for i in range(workers)
                        ],
                    ]
                    try:
                        # return_exceptions=True: si un worker lanza una
                        # excepción no capturada, los otros siguen corriendo.
                        # (Cada worker ya atrapa su propia excepción, pero
                        # este es un segundo nivel de seguridad.)
                        await asyncio.gather(*_all_tasks, return_exceptions=True)
                    except asyncio.CancelledError:
                        # Cancelar explícitamente y esperar que todas las
                        # tareas hijas terminen antes de propagar el error.
                        # Esto evita Tasks huérfanas y los warnings de GC.
                        for _t in _all_tasks:
                            _t.cancel()
                        await asyncio.gather(*_all_tasks, return_exceptions=True)
                        raise

                    # Actualizar last_synced y file_count de esta fuente
                    # y refrescar la tabla de fuentes inmediatamente
                    source_dl = downloaded_ref[0] - dl_before
                    new_count = (pu["file_count"] or 0) + source_dl
                    await update_profile_url_sync(
                        INDEX_DB,
                        pu["id"],
                        file_count=new_count,
                    )
                    # Refrescar fila en la tabla sin esperar al final
                    self._refresh_source_row(pu["id"], new_count)

                # ── Cola diferida ──────────────────────────────────────────
                if deferred:
                    self._append_log(
                        f"\n⏭ Cola diferida: {len(deferred)} archivo(s)…"
                    )
                    for file_info, a_info, dest_folder in deferred:
                        if await url_exists(dest_folder, file_info.url):
                            skipped_ref[0] += 1
                            continue
                        counter = await next_counter(dest_folder)
                        final_name = build_filename(
                            a_info.name, counter, file_info.filename
                        )
                        self._lbl_status.setText(
                            f"⏭ reintento: {file_info.filename[:55]}"
                        )
                        result = await engine.download(
                            url=file_info.url,
                            dest_dir=dest_folder,
                            filename=final_name,
                        )
                        if result.ok:
                            assert result.file_hash is not None
                            await add_file(
                                artist_dir=dest_folder,
                                file_hash=result.file_hash,
                                filename=final_name,
                                url_source=file_info.url,
                                file_size=result.file_size,
                                counter=counter,
                            )
                            downloaded_ref[0] += 1
                            self._append_log(
                                f"  ✓ {final_name} (reintento)"
                            )
                        else:
                            deferred_count_ref[0] += 1
                            self._append_log(
                                f"  ⏭ {file_info.filename}"
                                " — pendiente próx. sync"
                            )
                        self._update_counters(
                            downloaded_ref[0],
                            skipped_ref[0],
                            errors_ref[0],
                            deferred_count_ref[0],
                        )

            # Extraer valores finales para el resumen
            downloaded = downloaded_ref[0]
            skipped = skipped_ref[0]
            errors = errors_ref[0]
            deferred_count = deferred_count_ref[0]

            await update_profile_last_checked(INDEX_DB, self._profile["id"])
            summary = (
                f"Completado — ↓ {downloaded} nuevos  skip {skipped}"
            )
            if errors:
                summary += f"  ✗ {errors} errores"
            if deferred_count:
                summary += f"  ⏭ {deferred_count} para próxima sync"
            self._lbl_status.setText(summary)
            self._append_log(f"\n{summary}")
            self._set_status_light("done")
            await self._load_async(self._profile["id"])

        except asyncio.CancelledError:
            self._lbl_status.setText(
                f"Cancelado — ↓ {downloaded_ref[0]} descargados"
            )
            self._append_log("Descarga cancelada por el usuario.")
            self._set_status_light("cancelled")
        except Exception as exc:
            self._lbl_status.setText(f"Error: {exc}")
            self._append_log(f"\n✗ Error: {exc}")
            self._set_status_light("error")
        finally:
            self._download_task = None
            self._set_busy(False)

    async def _do_deduplicate(self) -> None:
        """
        Busca archivos duplicados (mismo hash, nombre distinto) en la
        carpeta del artista y elimina las copias extra.

        Lógica:
          1. Carga el catálogo → mapa hash→filename_canónico
          2. Escanea el disco → calcula SHA-256 de cada archivo
             (en thread para no bloquear el event loop)
          3. Agrupa rutas por hash
          4. Para cada grupo con >1 archivo:
             - Mantiene el que coincide con el nombre canónico del catálogo
               o, si ninguno coincide, el primero de la lista
             - Elimina el resto y acumula bytes liberados
          5. Reporta el resultado
        """
        if not self._profile:
            return
        self._set_busy(True)
        self._log.clear()
        self._lbl_status.setText("Deduplicando…")

        try:
            from ...catalog import get_all_files
            from ...hasher import sha256_file

            folder = Path(self._profile["folder_path"])
            if not folder.exists():
                self._lbl_status.setText("Carpeta del artista no encontrada.")
                return

            # Paso 1 — catálogo: hash → filename canónico
            catalog_rows = await get_all_files(folder)
            catalog_map: dict[str, str] = {
                row["hash"]: row["filename"] for row in catalog_rows
            }
            self._append_log(
                f"Catálogo: {len(catalog_map)} entradas"
            )

            # Paso 2 — escanear disco y calcular hashes en thread
            all_files = [
                p for p in folder.iterdir()
                if p.is_file() and p.name != "catalog.db"
            ]
            self._append_log(
                f"Archivos en disco: {len(all_files)}"
            )
            self._lbl_status.setText(
                f"Calculando hashes de {len(all_files)} archivos…"
            )

            def _scan() -> dict[str, list[Path]]:
                """Calcula hashes y agrupa rutas. Bloqueante, corre en thread."""
                groups: dict[str, list[Path]] = {}
                for p in all_files:
                    try:
                        h = sha256_file(p)
                    except OSError:
                        continue
                    groups.setdefault(h, []).append(p)
                return groups

            groups = await asyncio.to_thread(_scan)

            # Paso 3 — eliminar duplicados
            removed = 0
            freed_bytes = 0
            for file_hash, paths in groups.items():
                if len(paths) < 2:
                    continue
                # Preferir el que coincide con el catálogo
                canonical = catalog_map.get(file_hash)
                if canonical:
                    keep = next(
                        (p for p in paths if p.name == canonical),
                        paths[0],
                    )
                else:
                    keep = paths[0]

                for p in paths:
                    if p == keep:
                        continue
                    try:
                        size = p.stat().st_size
                        p.unlink()
                        freed_bytes += size
                        removed += 1
                        self._append_log(
                            f"  ✗ {p.name}  →  mantiene {keep.name}"
                        )
                    except OSError as exc:
                        self._append_log(f"  ⚠ No se pudo borrar {p.name}: {exc}")

            # Paso 4 — resultado
            freed_str = _fmt_size(freed_bytes)
            if removed:
                summary = (
                    f"Deduplicación completa — "
                    f"{removed} duplicado(s) eliminado(s), "
                    f"{freed_str} liberados"
                )
            else:
                summary = "Sin duplicados — la colección está limpia"
            self._lbl_status.setText(summary)
            self._append_log(f"\n{summary}")
            self._set_status_light("done")
            await self._load_async(self._profile["id"])

        except asyncio.CancelledError:
            self._lbl_status.setText("Deduplicación cancelada.")
            self._set_status_light("cancelled")
        except Exception as exc:
            self._lbl_status.setText(f"Error al deduplicar: {exc}")
            self._append_log(f"\n✗ Error: {exc}")
            self._set_status_light("error")
        finally:
            self._download_task = None
            self._set_busy(False)

    async def _do_prescan(self, prescan_path: Path) -> None:
        """Pre-escanea una carpeta de archivos existentes antes de descargar."""
        if not self._profile:
            return

        from ...engine import DownloadEngine
        from ...organizer import organize
        from ...templates._registry import get_template

        profile = self._profile
        folder = Path(profile["folder_path"])

        # Intentar obtener artist_id desde la BD primero
        main_url = next(
            (pu for pu in profile["urls"] if pu["enabled"] and pu.get("artist_id")),
            None,
        )

        # Si no está en BD (perfil recién creado), resolverlo desde la API
        if not main_url:
            url_entry = next(
                (pu for pu in profile["urls"] if pu["enabled"] and pu.get("url")),
                None,
            )
            if not url_entry:
                self._append_log("  ⚠ Pre-scan omitido — sin URL activa")
                return
            try:
                self._append_log("  Pre-scan: resolviendo artista desde API…")
                async with DownloadEngine(load_config()) as engine:
                    tmpl = get_template(url_entry["url"], engine)
                    if not tmpl:
                        self._append_log(
                            "  ⚠ Pre-scan omitido — sin template para la URL"
                        )
                        return
                    artist_info = await tmpl.get_artist_info(url_entry["url"])
                artist_id = artist_info.artist_id
                site = url_entry["site"]
            except Exception as exc:
                self._append_log(f"  ⚠ Pre-scan omitido — error API: {exc}")
                return
        else:
            artist_id = main_url["artist_id"]
            site = main_url["site"]

        def on_progress(processed: int, total: int, filename: str) -> None:
            self._lbl_status.setText(f"Pre-scan [{processed}/{total}]: {filename[:40]}")

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
        self._append_log(f"  Pre-scan: {scan_result.summary()}")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _fmt_size(n: int) -> str:
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _fmt_speed(bps: float) -> str:
    """Formatea velocidad en bytes/s a string legible."""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} GB/s"
