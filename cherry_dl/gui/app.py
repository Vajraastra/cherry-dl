"""
cherry-dl GUI — PySide6 + qasync.

Arquitectura:
  QMainWindow
    └─ QStackedWidget   ← router de vistas
         ├─ [0] ProfilesView       ← pantalla principal
         ├─ [1] NewProfileWizard   ← wizard creación
         ├─ [2] ArtistDetailView   ← detalle/descarga
         └─ [3] SettingsView       ← configuración

qasync fusiona el event loop de asyncio con el de Qt, eliminando el
bridge de queue.Queue + hilo daemon que usaba la GUI anterior (DPG).
"""

from __future__ import annotations

import sys
from typing import Any

import asyncio
import qasync
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QStackedWidget,
)

from .theme import STYLESHEET
from .views.profiles_view import ProfilesView
from .views.new_profile_wizard import NewProfileWizard
from .views.artist_detail_view import ArtistDetailView
from .views.settings_view import SettingsView

# Índices del QStackedWidget
_VIEW_PROFILES = 0
_VIEW_WIZARD = 1
_VIEW_DETAIL = 2
_VIEW_SETTINGS = 3


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("cherry-dl")
        self.setMinimumSize(1000, 680)
        self.resize(1280, 820)

        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        # ── Vistas registradas ───────────────────────────────────────────
        self._profiles_view = ProfilesView(nav=self.navigate_to)
        self._wizard = NewProfileWizard(nav=self.navigate_to)
        self._detail_view = ArtistDetailView(nav=self.navigate_to)
        self._settings_view = SettingsView(nav=self.navigate_to)

        self._stack.addWidget(self._profiles_view)   # índice 0
        self._stack.addWidget(self._wizard)           # índice 1
        self._stack.addWidget(self._detail_view)      # índice 2
        self._stack.addWidget(self._settings_view)    # índice 3

        self._stack.setCurrentIndex(_VIEW_PROFILES)

    # ── Navegación entre vistas ─────────────────────────────────────────

    def navigate_to(self, view_name: str, **kwargs: Any) -> None:
        """Cambia la vista activa del QStackedWidget."""
        match view_name:
            case "profiles":
                # Recargar la lista al volver (puede haber nuevos perfiles)
                self._profiles_view._loaded = False
                self._stack.setCurrentIndex(_VIEW_PROFILES)

            case "new_profile":
                self._wizard.reset()
                self._stack.setCurrentIndex(_VIEW_WIZARD)

            case "artist_detail":
                profile_id = kwargs.get("profile_id")
                auto_dl = kwargs.get("auto_download", False)
                auto_chk = kwargs.get("auto_check", False)
                prescan = kwargs.get("prescan_path")
                if profile_id is not None:
                    self._detail_view.load_profile(
                        profile_id,
                        auto_download=auto_dl,
                        auto_check=auto_chk,
                        prescan_path=prescan,
                    )
                self._stack.setCurrentIndex(_VIEW_DETAIL)

            case "settings":
                self._stack.setCurrentIndex(_VIEW_SETTINGS)

            case _:
                pass


# ── Entry point ─────────────────────────────────────────────────────────────


def run_app() -> None:
    """Inicializa Qt + qasync y lanza la ventana principal."""
    app = QApplication.instance() or QApplication(sys.argv)
    # Forzar Fusion: respeta QSS al 100%, evita que Breeze/KDE
    # sobreescriba colores de texto en QSpinBox y widgets nativos.
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setApplicationName("cherry-dl")

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    with loop:
        window = MainWindow()
        window.show()
        loop.run_forever()
