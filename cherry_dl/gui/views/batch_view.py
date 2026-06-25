"""
Vista de descarga por lotes — procesa varios perfiles en una sola corrida.

Delega TODO el flujo por perfil en `download_service.run_profile_download`
(scan → pending_queue → cooldown → workers → diferidos → resume). Esta vista
sólo orquesta: recorre los perfiles seleccionados en secuencia y reintenta los
que avanzan pero quedan con pendientes, hasta que no quede nada, el usuario
detenga, o se alcance el máximo de iteraciones.

Diseño análogo a la `BatchScreen` de la TUI (que se deprecará), pero sobre el
servicio canónico en vez de su propia lógica de scan/descarga.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config import INDEX_DB, load_config
from ...downloads import EXT_GROUPS, _decode_profile_filter, _parse_ext_filter

# Tope de iteraciones de reintento del loop completo (red de seguridad).
_MAX_ITERATIONS = 4


class BatchView(QWidget):
    """Descarga por lotes multi-perfil sobre `download_service`."""

    def __init__(self, nav: Callable, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav
        self._loaded = False
        self._batch_task: asyncio.Task | None = None
        self._current_task: asyncio.Task | None = None
        self._stop_requested = False
        self._skip_requested = False
        self._batch_total = 0
        self._batch_done = 0
        self._setup_ui()

    # ── Construcción de UI ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        # ── Header ─────────────────────────────────────────────────────────
        header = QHBoxLayout()
        btn_back = QPushButton("← Volver")
        btn_back.clicked.connect(lambda: self._nav("profiles"))
        lbl_title = QLabel("Descarga por lotes")
        lbl_title.setObjectName("lbl_title")
        header.addWidget(btn_back)
        header.addWidget(lbl_title)
        header.addStretch()
        root.addLayout(header)

        # ── Panel de configuración (visible antes de iniciar) ──────────────
        self._config_panel = QWidget()
        cfg = QVBoxLayout(self._config_panel)
        cfg.setContentsMargins(0, 0, 0, 0)
        cfg.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(QLabel("Workers:"))
        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 20)
        self._workers_spin.setValue(load_config().workers)
        self._workers_spin.setFixedWidth(60)
        top.addWidget(self._workers_spin)
        top.addSpacing(16)
        self._chk_use_profile = QCheckBox("Usar el filtro configurado de cada perfil")
        self._chk_use_profile.toggled.connect(self._on_use_profile_toggled)
        top.addWidget(self._chk_use_profile)
        top.addStretch()
        self._btn_start = QPushButton("▶ Iniciar batch")
        self._btn_start.setObjectName("btn_primary")
        self._btn_start.clicked.connect(self._on_start)
        top.addWidget(self._btn_start)
        cfg.addLayout(top)

        # Filtro global por tipos (oculto si se usa el de cada perfil)
        self._filter_row = QWidget()
        fr = QHBoxLayout(self._filter_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.setSpacing(10)
        lbl_types = QLabel("Tipos:")
        lbl_types.setObjectName("lbl_subtitle")
        fr.addWidget(lbl_types)
        self._ext_checks: dict[str, QCheckBox] = {}
        for group_id, (label, _exts) in EXT_GROUPS.items():
            chk = QCheckBox(label)
            self._ext_checks[group_id] = chk
            fr.addWidget(chk)
        fr.addWidget(QLabel("Extra:"))
        self._ext_custom = QLineEdit()
        self._ext_custom.setPlaceholderText("psd,clip  (vacío = todos)")
        self._ext_custom.setMaximumWidth(160)
        fr.addWidget(self._ext_custom)
        fr.addStretch()
        cfg.addWidget(self._filter_row)

        # Selección de perfiles
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Perfiles a procesar:"))
        self._btn_toggle_all = QPushButton("Ninguno")
        self._btn_toggle_all.setFixedWidth(90)
        self._btn_toggle_all.clicked.connect(self._on_toggle_all)
        sel_row.addWidget(self._btn_toggle_all)
        sel_row.addStretch()
        cfg.addLayout(sel_row)

        self._profile_list = QListWidget()
        self._profile_list.setMaximumHeight(260)
        cfg.addWidget(self._profile_list)

        root.addWidget(self._config_panel)

        # ── Panel de ejecución ─────────────────────────────────────────────
        self._stats_lbl = QLabel("")
        self._stats_lbl.setObjectName("lbl_subtitle")
        root.addWidget(self._stats_lbl)

        self._current_lbl = QLabel("")
        root.addWidget(self._current_lbl)

        prog_row = QHBoxLayout()
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        prog_row.addWidget(self._progress)
        self._progress_lbl = QLabel("0 / 0")
        self._progress_lbl.setObjectName("lbl_status")
        self._progress_lbl.setFixedWidth(110)
        self._progress_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        prog_row.addWidget(self._progress_lbl)
        root.addLayout(prog_row)

        self._counters_lbl = QLabel("")
        self._counters_lbl.setObjectName("lbl_status")
        root.addWidget(self._counters_lbl)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setObjectName("log_panel")
        root.addWidget(self._log, stretch=1)

        # ── Footer ─────────────────────────────────────────────────────────
        footer = QHBoxLayout()
        footer.addStretch()
        self._btn_skip = QPushButton("⏭ Saltar perfil")
        self._btn_skip.setEnabled(False)
        self._btn_skip.clicked.connect(self._on_skip)
        self._btn_stop = QPushButton("⏹ Detener")
        self._btn_stop.setObjectName("btn_danger")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self._on_stop)
        footer.addWidget(self._btn_skip)
        footer.addWidget(self._btn_stop)
        root.addLayout(footer)

    # ── Ciclo de vida ──────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._loaded:
            QTimer.singleShot(0, self._schedule_load)

    def reset(self) -> None:
        """Permite recargar la lista de perfiles al re-entrar a la vista."""
        self._loaded = False

    def _schedule_load(self) -> None:
        task = asyncio.ensure_future(self._load_profiles())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _load_profiles(self) -> None:
        from ...catalog import pending_count
        from ...index import init_index, list_profiles

        await init_index(INDEX_DB)
        rows = await list_profiles(INDEX_DB)
        self._profile_list.clear()
        for row in rows:
            folder = Path(row["folder_path"])
            if folder.exists():
                _, eff = _decode_profile_filter(row.get("ext_filter", ""))
                pend = await pending_count(folder, ext_filter=eff or None)
                suffix = f"   ⏳ {pend}" if pend else ""
            else:
                suffix = "   (sin carpeta)"
            item = QListWidgetItem(f"{row['display_name']}{suffix}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self._profile_list.addItem(item)
        self._loaded = True
        self._stats_lbl.setText(f"{len(rows)} perfil(es) disponibles.")

    # ── Slots de configuración ─────────────────────────────────────────────

    def _on_use_profile_toggled(self, checked: bool) -> None:
        self._filter_row.setVisible(not checked)

    def _on_toggle_all(self) -> None:
        # Si hay alguno marcado → desmarcar todos; si no → marcar todos.
        any_checked = any(
            self._profile_list.item(i).checkState() == Qt.CheckState.Checked
            for i in range(self._profile_list.count())
        )
        new_state = Qt.CheckState.Unchecked if any_checked else Qt.CheckState.Checked
        for i in range(self._profile_list.count()):
            self._profile_list.item(i).setCheckState(new_state)
        self._btn_toggle_all.setText("Todos" if any_checked else "Ninguno")

    # ── Inicio / control ───────────────────────────────────────────────────

    def _selected_profile_ids(self) -> list[int]:
        ids: list[int] = []
        for i in range(self._profile_list.count()):
            item = self._profile_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids

    def _global_ext_filter(self) -> set[str]:
        ext: set[str] = set()
        for gid, chk in self._ext_checks.items():
            if chk.isChecked():
                ext.update("." + e for e in EXT_GROUPS[gid][1])
        ext.update(_parse_ext_filter(self._ext_custom.text()))
        return ext

    def _on_start(self) -> None:
        ids = self._selected_profile_ids()
        if not ids:
            QMessageBox.information(
                self, "Sin perfiles", "Selecciona al menos un perfil para el batch."
            )
            return
        workers = self._workers_spin.value()
        use_profile = self._chk_use_profile.isChecked()
        global_ext = set() if use_profile else self._global_ext_filter()

        self._config_panel.setVisible(False)
        self._btn_stop.setEnabled(True)
        self._btn_skip.setEnabled(True)
        self._stop_requested = False
        self._log.clear()

        self._batch_task = asyncio.ensure_future(
            self._run_batch(ids, workers, use_profile, global_ext)
        )
        self._batch_task.add_done_callback(self._on_batch_finished)

    def _on_stop(self) -> None:
        self._stop_requested = True
        self._btn_stop.setEnabled(False)
        self._btn_stop.setText("Deteniendo…")
        self._append_log("⏹ Detención solicitada — terminando el archivo actual…")
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def _on_skip(self) -> None:
        self._skip_requested = True
        self._append_log("⏭ Saltando el perfil actual…")
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    # ── Loop principal ──────────────────────────────────────────────────────

    async def _run_batch(
        self,
        profile_ids: list[int],
        workers: int,
        use_profile_filter: bool,
        global_ext: set[str],
    ) -> None:
        from ...catalog import pending_count
        from ...download_service import run_profile_download
        from ...index import get_profile

        completed = 0
        to_process = list(profile_ids)
        iteration = 0

        while to_process and not self._stop_requested:
            iteration += 1
            still: list[int] = []
            for pid in to_process:
                if self._stop_requested:
                    break
                self._set_stats(iteration, completed, len(to_process))
                profile = await get_profile(INDEX_DB, pid)
                if not profile:
                    continue

                if use_profile_filter:
                    _, ext_filter = _decode_profile_filter(profile.get("ext_filter", ""))
                else:
                    ext_filter = global_ext

                folder = Path(profile["folder_path"])
                before = (
                    await pending_count(folder, ext_filter=ext_filter or None)
                    if folder.exists() else 0
                )

                self._reset_progress()
                self._current_lbl.setText(
                    f"▶ {profile['display_name']}  (iteración {iteration})"
                )
                self._append_log(
                    f"\n━━ {profile['display_name']}  ·  iteración {iteration} ━━"
                )

                self._skip_requested = False
                self._current_task = asyncio.ensure_future(
                    run_profile_download(
                        profile,
                        workers=workers,
                        ext_filter=ext_filter,
                        exclude_mode=False,
                        emit=self._on_event,
                        resolve_auth=self._resolve_auth,
                    )
                )
                try:
                    summary = await self._current_task
                    completed += summary.downloaded
                except asyncio.CancelledError:
                    if self._stop_requested:
                        self._append_log("⏹ Batch detenido por el usuario.")
                        return
                    self._append_log(f"⏭ {profile['display_name']} — omitido.")
                    continue
                except Exception as exc:
                    self._append_log(f"✗ Error en {profile['display_name']}: {exc}")
                    continue
                finally:
                    self._current_task = None

                after = (
                    await pending_count(folder, ext_filter=ext_filter or None)
                    if folder.exists() else 0
                )
                if after > 0 and after < before:
                    still.append(pid)
                    self._append_log(
                        f"↺ {after} pendiente(s) — se reintentará en la próxima iteración."
                    )
                elif after > 0:
                    self._append_log(
                        f"⚠ {after} pendiente(s) sin progreso — se abandona este perfil."
                    )

            to_process = still
            if iteration >= _MAX_ITERATIONS and to_process:
                self._append_log(
                    f"⚠ Máximo de iteraciones ({_MAX_ITERATIONS}) alcanzado — "
                    f"{len(to_process)} perfil(es) con pendientes."
                )
                break

        self._append_log(f"\n✓ Batch finalizado — {completed} archivo(s) descargado(s).")

    def _on_batch_finished(self, task: asyncio.Task) -> None:
        self._batch_task = None
        self._btn_stop.setEnabled(False)
        self._btn_stop.setText("⏹ Detener")
        self._btn_skip.setEnabled(False)
        self._config_panel.setVisible(True)
        self._loaded = False  # refrescar pendientes al re-mostrar config
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                self._append_log(f"✗ Error inesperado en el batch: {exc}")
        # Recargar la lista de perfiles (pendientes actualizados)
        self._schedule_load()

    # ── Traducción de eventos del servicio ─────────────────────────────────

    def _on_event(self, ev) -> None:
        from ...download_service import (
            BatchInfo,
            Cooldown,
            Counters,
            Log,
            ScanProgress,
            SourceStarted,
            WorkerDone,
            WorkersResolved,
        )

        if isinstance(ev, Log):
            self._append_log(ev.msg)
        elif isinstance(ev, Counters):
            self._counters_lbl.setText(
                f"↓ {ev.downloaded} descargados   ·   ⊘ {ev.skipped} saltados   ·   "
                f"✗ {ev.errors} errores   ·   ⏸ {ev.deferred} diferidos"
            )
        elif isinstance(ev, BatchInfo):
            self._batch_total = ev.total
            self._batch_done = ev.offset
            self._update_progress()
        elif isinstance(ev, WorkerDone):
            # Cada archivo terminado (✓/↷/✗/⏸) avanza el progreso del perfil.
            self._batch_done += 1
            self._update_progress()
        elif isinstance(ev, WorkersResolved):
            self._append_log(f"Workers efectivos: {ev.count}")
        elif isinstance(ev, SourceStarted):
            self._current_lbl.setText(f"▶ {ev.artist} ({ev.site})")
        elif isinstance(ev, ScanProgress):
            self._current_lbl.setText(
                f"Escaneando… {ev.seen} vistos · {ev.queued} en cola"
            )
        elif isinstance(ev, Cooldown):
            self._append_log(f"⏸ Enfriamiento {ev.seconds:.0f}s…")

    async def _resolve_auth(self, site: str) -> bool:
        """Resuelve auth interactiva (Patreon) si una fuente la requiere."""
        if site != "patreon":
            self._append_log(f"  ✗ Auth de {site} no soportada en la GUI todavía")
            return False
        from ...auth.patreon import guided_login_patreon

        self._append_log("  ⟳ Abriendo el navegador para iniciar sesión en Patreon…")
        try:
            cookies = await guided_login_patreon(
                on_status=lambda m: self._append_log(f"    {m}"),
            )
        except Exception as exc:
            self._append_log(f"  ✗ Error en el login de Patreon: {exc}")
            return False
        if cookies and cookies.get("session_id"):
            self._append_log("  ✓ Sesión de Patreon iniciada y guardada.")
            return True
        self._append_log("  ✗ Login incompleto — no se capturó la sesión.")
        return False

    # ── Helpers de UI ───────────────────────────────────────────────────────

    def _set_stats(self, iteration: int, completed: int, remaining: int) -> None:
        self._stats_lbl.setText(
            f"Iteración {iteration}   ·   ✓ {completed} descargados   ·   "
            f"{remaining} perfil(es) en cola"
        )

    def _reset_progress(self) -> None:
        self._batch_total = 0
        self._batch_done = 0
        self._update_progress()

    def _update_progress(self) -> None:
        total = max(self._batch_total, 1)
        done = min(self._batch_done, total)
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._progress_lbl.setText(f"{self._batch_done} / {self._batch_total}")

    def _append_log(self, msg: str) -> None:
        self._log.appendPlainText(msg)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())
