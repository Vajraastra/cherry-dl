"""
Vista para crear un nuevo perfil de artista.

Campos:
  - URL principal + [Obtener nombre desde API] + detección de sitio local
  - Nombre del artista (auto-rellenado, editable)
  - Preview de carpeta destino
  - URLs adicionales opcionales
  - Workers paralelos, filtro de extensiones
  - [Crear perfil] / [Crear y Descargar]
"""

from __future__ import annotations

import asyncio
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..native_dialog import pick_directory_sync

from ...config import INDEX_DB, load_config
from ...engine import DownloadEngine
from ...profiles import (
    _safe_dirname,
    _site_from_url,
    add_url_to_profile,
    create_profile,
    resolve_artist_name,
)


class NewProfileWizard(QWidget):
    """Formulario de creación de nuevo perfil de artista."""

    def __init__(self, nav: Callable, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav
        self._extra_urls: list[str] = []
        self._detected_site: str = ""
        self._setup_ui()

    # ── Construcción de UI ─────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(14)

        # ── Header ─────────────────────────────────────────────────────────
        header = QHBoxLayout()
        btn_back = QPushButton("← Volver")
        btn_back.clicked.connect(lambda: self._nav("profiles"))
        lbl_title = QLabel("Nuevo Artista")
        lbl_title.setObjectName("lbl_title")
        header.addWidget(btn_back)
        header.addSpacing(12)
        header.addWidget(lbl_title)
        header.addStretch()
        root.addLayout(header)

        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep)

        # ── Formulario principal ────────────────────────────────────────────
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        url_row = QHBoxLayout()
        self._url_input = QLineEdit()
        self._url_input.setPlaceholderText("https://kemono.cr/patreon/user/12345")
        self._btn_resolve = QPushButton("Obtener nombre")
        self._btn_resolve.setFixedWidth(140)
        url_row.addWidget(self._url_input)
        url_row.addWidget(self._btn_resolve)
        form.addRow("URL principal:", url_row)

        self._lbl_site = QLabel("—")
        self._lbl_site.setObjectName("lbl_subtitle")
        form.addRow("Sitio detectado:", self._lbl_site)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("Nombre del artista")
        form.addRow("Nombre:", self._name_input)

        self._lbl_folder = QLabel("—")
        self._lbl_folder.setObjectName("lbl_subtitle")
        self._lbl_folder.setWordWrap(True)
        form.addRow("Carpeta destino:", self._lbl_folder)

        root.addLayout(form)

        # ── URLs adicionales ────────────────────────────────────────────────
        lbl_extra = QLabel("URLS ADICIONALES")
        lbl_extra.setObjectName("lbl_section")
        root.addWidget(lbl_extra)

        extra_row = QHBoxLayout()
        self._extra_url_input = QLineEdit()
        self._extra_url_input.setPlaceholderText("URL de otro servicio (opcional)")
        self._btn_add_url = QPushButton("+ Agregar")
        self._btn_add_url.setFixedWidth(100)
        extra_row.addWidget(self._extra_url_input)
        extra_row.addWidget(self._btn_add_url)
        root.addLayout(extra_row)

        self._extra_list = QListWidget()
        self._extra_list.setMaximumHeight(90)
        root.addWidget(self._extra_list)

        # ── Opciones ────────────────────────────────────────────────────────
        lbl_opts = QLabel("OPCIONES")
        lbl_opts.setObjectName("lbl_section")
        root.addWidget(lbl_opts)

        opts_form = QFormLayout()
        opts_form.setSpacing(10)
        opts_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 20)
        self._workers_spin.setValue(load_config().workers)
        opts_form.addRow("Workers paralelos:", self._workers_spin)

        self._ext_filter = QLineEdit()
        self._ext_filter.setPlaceholderText("jpg,png,mp4  (vacío = todos)")
        opts_form.addRow("Filtro de extensiones:", self._ext_filter)

        root.addLayout(opts_form)

        # ── Archivos preexistentes ──────────────────────────────────────────
        sep2 = QFrame()
        sep2.setObjectName("separator")
        sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)

        lbl_pre = QLabel("ARCHIVOS PREEXISTENTES")
        lbl_pre.setObjectName("lbl_section")
        root.addWidget(lbl_pre)

        lbl_pre_hint = QLabel(
            "Opcional. Si tienes archivos descargados previamente, "
            "cherry-dl los escaneará, calculará sus hashes, los renombrará "
            "y moverá a la carpeta del artista antes de descargar."
        )
        lbl_pre_hint.setObjectName("lbl_status")
        lbl_pre_hint.setWordWrap(True)
        root.addWidget(lbl_pre_hint)

        prescan_row = QHBoxLayout()
        self._prescan_input = QLineEdit()
        self._prescan_input.setPlaceholderText(
            "Carpeta con archivos existentes (opcional)"
        )
        self._btn_prescan_browse = QPushButton("Explorar")
        self._btn_prescan_browse.setMinimumWidth(100)
        self._btn_prescan_browse.setFixedHeight(44)
        self._btn_prescan_run = QPushButton("Pre-scan")
        self._btn_prescan_run.setMinimumWidth(100)
        self._btn_prescan_run.setFixedHeight(44)
        self._btn_prescan_run.setEnabled(False)
        prescan_row.addWidget(self._prescan_input)
        prescan_row.addWidget(self._btn_prescan_browse)
        prescan_row.addWidget(self._btn_prescan_run)
        root.addLayout(prescan_row)

        root.addStretch()

        # ── Barra de estado ─────────────────────────────────────────────────
        self._lbl_status = QLabel("")
        self._lbl_status.setObjectName("lbl_status")
        root.addWidget(self._lbl_status)

        # ── Botones de acción ───────────────────────────────────────────────
        actions = QHBoxLayout()
        actions.addStretch()
        self._btn_create = QPushButton("Crear perfil")
        self._btn_create.setEnabled(False)
        self._btn_create_dl = QPushButton("Crear y Descargar")
        self._btn_create_dl.setObjectName("btn_primary")
        self._btn_create_dl.setEnabled(False)
        actions.addWidget(self._btn_create)
        actions.addWidget(self._btn_create_dl)
        root.addLayout(actions)

        # ── Señales ─────────────────────────────────────────────────────────
        self._url_input.textChanged.connect(self._on_url_changed)
        self._name_input.textChanged.connect(self._update_folder_preview)
        self._btn_resolve.clicked.connect(self._on_resolve)
        self._btn_add_url.clicked.connect(self._on_add_extra_url)
        self._prescan_input.textChanged.connect(self._on_prescan_text_changed)
        self._btn_prescan_browse.clicked.connect(self._on_browse_prescan)
        self._btn_prescan_run.clicked.connect(self._on_prescan_run)
        self._btn_create.clicked.connect(self._on_create)
        self._btn_create_dl.clicked.connect(self._on_create_and_download)

    # ── Ciclo de vida ──────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Limpia el formulario para una nueva entrada."""
        self._url_input.clear()
        self._name_input.clear()
        self._extra_url_input.clear()
        self._extra_urls.clear()
        self._extra_list.clear()
        self._prescan_input.clear()
        self._btn_prescan_run.setEnabled(False)
        self._detected_site = ""
        self._lbl_site.setText("—")
        self._lbl_folder.setText("—")
        self._lbl_status.setText("")
        self._btn_create.setEnabled(False)
        self._btn_create_dl.setEnabled(False)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_url_changed(self, text: str) -> None:
        url = text.strip()
        if url:
            site = _site_from_url(url)
            self._detected_site = site if site != "unknown" else ""
            self._lbl_site.setText(site.upper() if site != "unknown" else "No reconocida")
        else:
            self._detected_site = ""
            self._lbl_site.setText("—")
        self._update_folder_preview()

    def _on_resolve(self) -> None:
        asyncio.ensure_future(self._resolve_name())

    def _on_add_extra_url(self) -> None:
        url = self._extra_url_input.text().strip()
        if not url or url in self._extra_urls:
            return
        self._extra_urls.append(url)
        site = _site_from_url(url)
        item = QListWidgetItem(f"[{site}]  {url}")
        item.setData(Qt.ItemDataRole.UserRole, url)
        self._extra_list.addItem(item)
        self._extra_url_input.clear()

    def _on_prescan_text_changed(self, text: str) -> None:
        self._btn_prescan_run.setEnabled(bool(text.strip()))

    def _on_browse_prescan(self) -> None:
        folder = pick_directory_sync("Carpeta con archivos preexistentes")
        if folder:
            self._prescan_input.setText(folder)

    def _on_prescan_run(self) -> None:
        asyncio.ensure_future(self._do_prescan())

    def _on_create(self) -> None:
        asyncio.ensure_future(self._do_create(download=False))

    def _on_create_and_download(self) -> None:
        asyncio.ensure_future(self._do_create(download=True))

    def _update_folder_preview(self) -> None:
        name = self._name_input.text().strip()
        site = self._detected_site or "sitio"
        if name:
            folder = load_config().download_path / site / _safe_dirname(name)
            self._lbl_folder.setText(str(folder))
        else:
            self._lbl_folder.setText("—")
        has_data = bool(self._url_input.text().strip() and name)
        self._btn_create.setEnabled(has_data)
        self._btn_create_dl.setEnabled(has_data)

    # ── Async ──────────────────────────────────────────────────────────────────

    async def _do_prescan(self) -> None:
        """Organiza archivos preexistentes en la carpeta destino del artista."""
        from pathlib import Path
        from ...organizer import organize
        from ...templates._registry import get_template

        url = self._url_input.text().strip()
        name = self._name_input.text().strip()
        prescan_str = self._prescan_input.text().strip()

        if not url or not name:
            self._lbl_status.setText(
                "Completa URL y nombre antes de hacer Pre-scan."
            )
            return

        self._btn_prescan_run.setEnabled(False)
        self._btn_create.setEnabled(False)
        self._btn_create_dl.setEnabled(False)
        self._lbl_status.setText("Pre-scan: resolviendo artista desde API…")

        try:
            config = load_config()
            async with DownloadEngine(config) as engine:
                tmpl = get_template(url, engine)
                if not tmpl:
                    self._lbl_status.setText(
                        "Pre-scan: no hay template para esta URL."
                    )
                    return
                artist_info = await tmpl.get_artist_info(url)

            def on_progress(processed: int, total: int, filename: str) -> None:
                self._lbl_status.setText(
                    f"Pre-scan [{processed}/{total}]: {filename[:50]}"
                )

            self._lbl_status.setText("Pre-scan: organizando archivos…")
            scan_result, _ = await organize(
                source_dir=Path(prescan_str),
                artist_name=name,
                artist_id=artist_info.artist_id,
                site=artist_info.site,
                dest_root=config.download_path,
                progress_cb=on_progress,
            )
            self._lbl_status.setText(
                f"Pre-scan completado — {scan_result.summary()}"
            )
        except Exception as exc:
            self._lbl_status.setText(f"Error en pre-scan: {exc}")
        finally:
            self._btn_prescan_run.setEnabled(True)
            has_data = bool(self._url_input.text().strip() and name)
            self._btn_create.setEnabled(has_data)
            self._btn_create_dl.setEnabled(has_data)

    async def _resolve_name(self) -> None:
        url = self._url_input.text().strip()
        if not url:
            self._lbl_status.setText("Ingresa una URL primero.")
            return
        self._btn_resolve.setEnabled(False)
        self._lbl_status.setText("Consultando API…")
        try:
            config = load_config()
            async with DownloadEngine(config) as engine:
                name = await resolve_artist_name(engine, url)
            self._name_input.setText(name)
            self._lbl_status.setText(f"Nombre obtenido: {name}")
        except Exception as exc:
            self._lbl_status.setText(f"Error: {exc}")
        finally:
            self._btn_resolve.setEnabled(True)

    async def _do_create(self, download: bool) -> None:
        name = self._name_input.text().strip()
        url = self._url_input.text().strip()
        if not name or not url:
            return
        self._btn_create.setEnabled(False)
        self._btn_create_dl.setEnabled(False)
        self._lbl_status.setText("Creando perfil…")
        try:
            config = load_config()
            profile_id = await create_profile(
                db_path=INDEX_DB,
                display_name=name,
                primary_url=url,
                base_dir=config.download_path,
            )
            for extra_url in self._extra_urls:
                await add_url_to_profile(INDEX_DB, profile_id, extra_url)
            self._lbl_status.setText(f"Perfil '{name}' creado.")
            if download:
                self._nav(
                    "artist_detail",
                    profile_id=profile_id,
                    auto_download=True,
                )
            else:
                QTimer.singleShot(600, lambda: self._nav("profiles"))
        except Exception as exc:
            self._lbl_status.setText(f"Error al crear perfil: {exc}")
            self._btn_create.setEnabled(True)
            self._btn_create_dl.setEnabled(True)
