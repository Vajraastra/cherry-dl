"""
Vista de configuración global de cherry-dl.

Secciones:
  - General:   carpeta de descargas, workers por defecto, timeout
  - Red:       delay_min, delay_max, retries_api, retries_file
  - Info:      rutas del sistema (solo lectura)

Los cambios se guardan al hacer clic en [Guardar] y son efectivos
de inmediato (las vistas que usen load_config() recogerán los valores).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..native_dialog import pick_directory_sync

from ...config import (
    CHERRY_DIR,
    CONFIG_FILE,
    INDEX_DB,
    NetworkConfig,
    UserConfig,
    load_config,
    save_config,
)

# Ancho fijo para todos los spinboxes
_SPIN_W = 140


class SettingsView(QWidget):
    """Pantalla de configuración global."""

    def __init__(self, nav: Callable, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav
        self._setup_ui()

    # ── Construcción de UI ─────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 24, 32, 24)
        root.setSpacing(16)

        # ── Header ─────────────────────────────────────────────────────────
        header = QHBoxLayout()
        btn_back = QPushButton("← Volver")
        btn_back.clicked.connect(lambda: self._nav("profiles"))
        lbl_title = QLabel("Configuración")
        lbl_title.setObjectName("lbl_title")
        header.addWidget(btn_back)
        header.addSpacing(12)
        header.addWidget(lbl_title)
        header.addStretch()
        root.addLayout(header)

        sep0 = QFrame()
        sep0.setObjectName("separator")
        sep0.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep0)

        # ── GENERAL ────────────────────────────────────────────────────────
        lbl_general = QLabel("GENERAL")
        lbl_general.setObjectName("lbl_section")
        root.addWidget(lbl_general)

        form_gen = QFormLayout()
        form_gen.setSpacing(10)
        form_gen.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form_gen.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        # Carpeta de descargas
        dir_row = QHBoxLayout()
        dir_row.setSpacing(8)
        self._download_dir = QLineEdit()
        self._download_dir.setPlaceholderText("/ruta/a/colecciones")
        btn_browse = QPushButton("Explorar")
        btn_browse.setMinimumWidth(100)
        btn_browse.setFixedHeight(44)
        btn_browse.setToolTip("Seleccionar carpeta")
        btn_browse.clicked.connect(self._on_browse)
        dir_row.addWidget(self._download_dir)
        dir_row.addWidget(btn_browse)
        form_gen.addRow("Carpeta de descargas:", dir_row)

        # Workers
        self._workers = QSpinBox()
        self._workers.setRange(1, 32)
        self._workers.setFixedWidth(_SPIN_W)
        self._workers.setToolTip(
            "Descargas paralelas por defecto.\n"
            "Sobreescribible por artista en la vista de detalle."
        )
        form_gen.addRow("Workers paralelos:", self._workers)

        # Timeout
        self._timeout = QSpinBox()
        self._timeout.setRange(5, 300)
        self._timeout.setSuffix(" s")
        self._timeout.setFixedWidth(_SPIN_W)
        self._timeout.setToolTip("Tiempo máximo de espera por request HTTP.")
        form_gen.addRow("Timeout HTTP:", self._timeout)

        root.addLayout(form_gen)

        sep1 = QFrame()
        sep1.setObjectName("separator")
        sep1.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep1)

        # ── RED ────────────────────────────────────────────────────────────
        lbl_net = QLabel("RED")
        lbl_net.setObjectName("lbl_section")
        root.addWidget(lbl_net)

        lbl_delay_hint = QLabel(
            "Valores globales. El delay evita bloqueos del servidor; "
            "los reintentos cubren errores temporales."
        )
        lbl_delay_hint.setObjectName("lbl_status")
        lbl_delay_hint.setWordWrap(True)
        root.addWidget(lbl_delay_hint)

        form_net = QFormLayout()
        form_net.setSpacing(10)
        form_net.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form_net.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        self._delay_min = QDoubleSpinBox()
        self._delay_min.setRange(0.0, 120.0)
        self._delay_min.setSingleStep(1.0)
        self._delay_min.setSuffix(" s")
        self._delay_min.setFixedWidth(_SPIN_W)
        form_net.addRow("Delay mínimo:", self._delay_min)

        self._delay_max = QDoubleSpinBox()
        self._delay_max.setRange(0.0, 300.0)
        self._delay_max.setSingleStep(1.0)
        self._delay_max.setSuffix(" s")
        self._delay_max.setFixedWidth(_SPIN_W)
        form_net.addRow("Delay máximo:", self._delay_max)

        self._retries_api = QSpinBox()
        self._retries_api.setRange(1, 20)
        self._retries_api.setFixedWidth(_SPIN_W)
        self._retries_api.setToolTip("Reintentos al fallar una llamada API.")
        form_net.addRow("Reintentos API:", self._retries_api)

        self._retries_file = QSpinBox()
        self._retries_file.setRange(1, 20)
        self._retries_file.setFixedWidth(_SPIN_W)
        self._retries_file.setToolTip(
            "Reintentos al fallar la descarga de un archivo."
        )
        form_net.addRow("Reintentos archivos:", self._retries_file)

        self._stall_timeout = QSpinBox()
        self._stall_timeout.setRange(10, 600)
        self._stall_timeout.setSuffix(" s")
        self._stall_timeout.setFixedWidth(_SPIN_W)
        self._stall_timeout.setToolTip(
            "Segundos sin recibir datos antes de considerar\n"
            "una descarga como bloqueada (stall)."
        )
        form_net.addRow("Timeout de stall:", self._stall_timeout)

        root.addLayout(form_net)

        sep2 = QFrame()
        sep2.setObjectName("separator")
        sep2.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(sep2)

        # ── INFO ───────────────────────────────────────────────────────────
        lbl_info = QLabel("INFORMACIÓN DEL SISTEMA")
        lbl_info.setObjectName("lbl_section")
        root.addWidget(lbl_info)

        form_info = QFormLayout()
        form_info.setSpacing(8)
        form_info.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._lbl_config_path = QLabel(str(CONFIG_FILE))
        self._lbl_config_path.setObjectName("lbl_subtitle")
        self._lbl_config_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form_info.addRow("Archivo de config:", self._lbl_config_path)

        self._lbl_index_path = QLabel(str(INDEX_DB))
        self._lbl_index_path.setObjectName("lbl_subtitle")
        self._lbl_index_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form_info.addRow("Índice (index.db):", self._lbl_index_path)

        self._lbl_cherry_dir = QLabel(str(CHERRY_DIR))
        self._lbl_cherry_dir.setObjectName("lbl_subtitle")
        self._lbl_cherry_dir.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form_info.addRow("Directorio cherry-dl:", self._lbl_cherry_dir)

        root.addLayout(form_info)
        root.addStretch()

        # ── Estado y acciones ──────────────────────────────────────────────
        self._lbl_status = QLabel("")
        self._lbl_status.setObjectName("lbl_status")
        root.addWidget(self._lbl_status)

        actions = QHBoxLayout()
        actions.addStretch()
        btn_reset = QPushButton("Restablecer valores por defecto")
        self._btn_save = QPushButton("Guardar")
        self._btn_save.setObjectName("btn_primary")
        actions.addWidget(btn_reset)
        actions.addWidget(self._btn_save)
        root.addLayout(actions)

        btn_reset.clicked.connect(self._on_reset)
        self._btn_save.clicked.connect(self._on_save)

    # ── Ciclo de vida ──────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._load_values()

    def _load_values(self) -> None:
        """Rellena los campos con la configuración actual."""
        cfg = load_config()
        self._download_dir.setText(cfg.download_dir)
        self._workers.setValue(cfg.workers)
        self._timeout.setValue(cfg.timeout)
        self._delay_min.setValue(cfg.network.delay_min)
        self._delay_max.setValue(cfg.network.delay_max)
        self._retries_api.setValue(cfg.network.retries_api)
        self._retries_file.setValue(cfg.network.retries_file)
        self._stall_timeout.setValue(cfg.network.stall_timeout)
        self._lbl_status.setText("")

    # ── Slots ──────────────────────────────────────────────────────────────

    def _on_browse(self) -> None:
        current = self._download_dir.text() or str(Path.home())
        folder = pick_directory_sync("Seleccionar carpeta de descargas", current)
        if folder:
            self._download_dir.setText(folder)

    def _on_reset(self) -> None:
        defaults = UserConfig()
        self._download_dir.setText(defaults.download_dir)
        self._workers.setValue(defaults.workers)
        self._timeout.setValue(defaults.timeout)
        self._delay_min.setValue(defaults.network.delay_min)
        self._delay_max.setValue(defaults.network.delay_max)
        self._retries_api.setValue(defaults.network.retries_api)
        self._retries_file.setValue(defaults.network.retries_file)
        self._stall_timeout.setValue(defaults.network.stall_timeout)
        self._lbl_status.setText(
            "Valores por defecto cargados — presiona Guardar para aplicar."
        )

    def _on_save(self) -> None:
        try:
            delay_min = self._delay_min.value()
            delay_max = self._delay_max.value()
            if delay_max < delay_min:
                self._lbl_status.setText(
                    "Error: el delay máximo debe ser ≥ al mínimo."
                )
                return

            cfg = load_config()
            updated = cfg.model_copy(update={
                "download_dir": self._download_dir.text().strip(),
                "workers":      self._workers.value(),
                "timeout":      self._timeout.value(),
                "network": NetworkConfig(
                    delay_min=delay_min,
                    delay_max=delay_max,
                    retries_api=self._retries_api.value(),
                    retries_file=self._retries_file.value(),
                    stall_timeout=self._stall_timeout.value(),
                ),
            })
            save_config(updated)
            self._lbl_status.setText("Configuración guardada.")
        except Exception as exc:
            self._lbl_status.setText(f"Error al guardar: {exc}")
