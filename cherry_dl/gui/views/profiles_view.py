"""
Vista principal — lista de perfiles de artista.

Columnas: Artista | Fuentes | Archivos | Tamaño | Estado
Controles: [+ Nuevo Artista]  [⟳ Verificar todo]  [Buscar...]
Pie:       barra de actividad global
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import INDEX_DB
from ...index import delete_profile, init_index, list_profiles

# Índices de columna
_COL_NAME = 0
_COL_SOURCES = 1
_COL_FILES = 2
_COL_SIZE = 3
_COL_STATUS = 4


class ProfilesView(QWidget):
    """Lista de todos los perfiles con estadísticas de catálogo."""

    def __init__(
        self,
        nav: Callable,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._nav = nav
        self._rows: list[dict] = []
        self._loaded = False
        self._setup_ui()

    # ── Construcción de la UI ───────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 0)
        root.setSpacing(12)

        # ── Header ─────────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(8)

        lbl_title = QLabel("Artistas")
        lbl_title.setObjectName("lbl_title")

        self._search = QLineEdit()
        self._search.setPlaceholderText("Buscar artista…")
        self._search.setMaximumWidth(220)

        self._btn_refresh = QPushButton("⟳ Verificar todo")
        self._btn_new = QPushButton("+ Nuevo Artista")
        self._btn_new.setObjectName("btn_primary")
        self._btn_settings = QPushButton("⚙")
        self._btn_settings.setFixedWidth(36)
        self._btn_settings.setToolTip("Configuración")

        header.addWidget(lbl_title)
        header.addStretch()
        header.addWidget(self._search)
        header.addWidget(self._btn_refresh)
        header.addWidget(self._btn_new)
        header.addWidget(self._btn_settings)
        root.addLayout(header)

        # ── Tabla ──────────────────────────────────────────────────────────
        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(
            ["Artista", "Fuentes", "Archivos", "Tamaño", "Estado"]
        )
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)

        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(
            _COL_NAME, QHeaderView.ResizeMode.Stretch
        )
        for col in (_COL_SOURCES, _COL_FILES, _COL_SIZE, _COL_STATUS):
            hdr.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )

        root.addWidget(self._table)

        # ── Barra de actividad global ───────────────────────────────────────
        bar = QFrame()
        bar.setObjectName("activity_bar")
        bar.setFrameShape(QFrame.Shape.NoFrame)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(4, 6, 4, 6)

        self._lbl_status = QLabel("Sin descargas activas")
        self._lbl_status.setObjectName("lbl_status")

        self._lbl_count = QLabel("")
        self._lbl_count.setObjectName("lbl_subtitle")
        self._lbl_count.setAlignment(Qt.AlignmentFlag.AlignRight)

        bar_layout.addWidget(self._lbl_status)
        bar_layout.addStretch()
        bar_layout.addWidget(self._lbl_count)
        root.addWidget(bar)

        # ── Señales ─────────────────────────────────────────────────────────
        self._table.cellDoubleClicked.connect(self._on_row_double_click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._btn_new.clicked.connect(self._on_new_artist)
        self._btn_refresh.clicked.connect(self._on_refresh)
        self._btn_settings.clicked.connect(lambda: self._nav("settings"))
        self._search.textChanged.connect(self._filter_table)

        # Tecla Delete para borrar fila seleccionada
        shortcut = QShortcut(QKeySequence.StandardKey.Delete, self._table)
        shortcut.activated.connect(self._on_delete_selected)

    # ── Ciclo de vida ──────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._loaded:
            # QTimer asegura que la carga corre después de que Qt termine
            # de mostrar la ventana y qasync haya iniciado el event loop.
            QTimer.singleShot(0, self._schedule_load)

    def _schedule_load(self) -> None:
        asyncio.ensure_future(self._load_data())

    # ── Carga async de datos ───────────────────────────────────────────────

    async def _load_data(self) -> None:
        """Carga perfiles desde index.db y estadísticas de cada catalog.db."""
        self._lbl_status.setText("Cargando…")
        self._btn_refresh.setEnabled(False)
        self._table.setRowCount(0)

        try:
            await init_index(INDEX_DB)
            rows = await list_profiles(INDEX_DB)

            # Enriquecer con stats de catalog.db (total archivos + tamaño)
            from ...catalog import get_stats
            enriched: list[dict] = []
            for row in rows:
                folder = Path(row["folder_path"])
                if folder.exists():
                    stats = await get_stats(folder)
                else:
                    stats = {"total": 0, "total_size": 0}
                enriched.append({**row, **stats})

            self._rows = enriched
            self._populate_table(enriched)
            self._loaded = True

            n = len(enriched)
            self._lbl_count.setText(
                f"{n} artista{'s' if n != 1 else ''}"
            )
            if n == 0:
                self._lbl_status.setText(
                    "No hay artistas aún"
                    " — usa [+ Nuevo Artista] para agregar uno"
                )
            else:
                self._lbl_status.setText("Sin descargas activas")

        except Exception as exc:
            self._lbl_status.setText(f"Error al cargar: {exc}")

        finally:
            self._btn_refresh.setEnabled(True)

    def _populate_table(self, rows: list[dict]) -> None:
        """Rellena la tabla con la lista de perfiles enriquecidos."""
        self._table.setRowCount(len(rows))

        for r, row in enumerate(rows):
            # Artista — guarda profile_id como dato de usuario
            item_name = QTableWidgetItem(row["display_name"])
            item_name.setData(Qt.ItemDataRole.UserRole, row["id"])
            self._table.setItem(r, _COL_NAME, item_name)

            # Fuentes
            url_count = row.get("url_count", 0)
            site = row.get("primary_site", "")
            src_text = (
                f"1 ({site})" if url_count == 1 else f"{url_count} fuentes"
            )
            self._table.setItem(
                r, _COL_SOURCES, QTableWidgetItem(src_text)
            )

            # Archivos
            total = row.get("total", 0)
            self._table.setItem(
                r, _COL_FILES, _right_item(f"{total:,}")
            )

            # Tamaño
            self._table.setItem(
                r, _COL_SIZE,
                _right_item(_fmt_size(row.get("total_size", 0))),
            )

            # Estado
            last_checked = row.get("last_checked")
            status_text = (
                f"✓  {last_checked[:10]}" if last_checked else "Sin verificar"
            )
            self._table.setItem(
                r, _COL_STATUS, QTableWidgetItem(status_text)
            )

        self._table.resizeRowsToContents()

    # ── Filtro de búsqueda ─────────────────────────────────────────────────

    def _filter_table(self, query: str) -> None:
        q = query.strip().lower()
        for r in range(self._table.rowCount()):
            item = self._table.item(r, _COL_NAME)
            hidden = bool(q) and (
                item is None or q not in item.text().lower()
            )
            self._table.setRowHidden(r, hidden)

    # ── Slots de UI ────────────────────────────────────────────────────────

    def _on_row_double_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, _COL_NAME)
        if item:
            profile_id = item.data(Qt.ItemDataRole.UserRole)
            self._nav("artist_detail", profile_id=profile_id)

    def _on_new_artist(self) -> None:
        self._nav("new_profile")

    def _on_refresh(self) -> None:
        """Recarga la lista de perfiles desde la BD."""
        self._loaded = False
        asyncio.ensure_future(self._load_data())

    async def _do_delete(self, profile_id: int) -> None:
        """Elimina el perfil del índice y recarga la tabla."""
        try:
            await delete_profile(INDEX_DB, profile_id)
            self._loaded = False
            await self._load_data()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._lbl_status.setText(f"Error al borrar: {exc}")

    def _on_context_menu(self, pos) -> None:
        """Menú contextual al hacer clic derecho sobre una fila."""
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        item = self._table.item(row, _COL_NAME)
        if not item:
            return
        profile_id = item.data(Qt.ItemDataRole.UserRole)
        name = item.text()

        menu = QMenu(self)
        act_open = menu.addAction("Abrir")
        act_check = menu.addAction("Verificar actualizaciones")
        menu.addSeparator()
        act_delete = menu.addAction("Borrar perfil")

        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._nav("artist_detail", profile_id=profile_id)
        elif chosen == act_check:
            self._nav("artist_detail", profile_id=profile_id, auto_check=True)
        elif chosen == act_delete:
            self._confirm_delete(profile_id, name)

    def _on_delete_selected(self) -> None:
        """Borra el perfil seleccionado con tecla Delete."""
        rows = self._table.selectedItems()
        if not rows:
            return
        item = self._table.item(self._table.currentRow(), _COL_NAME)
        if item:
            self._confirm_delete(
                item.data(Qt.ItemDataRole.UserRole), item.text()
            )

    def _confirm_delete(self, profile_id: int, name: str) -> None:
        """Pide confirmación y borra el perfil de la BD."""
        reply = QMessageBox.question(
            self,
            "Borrar perfil",
            f"¿Borrar el perfil de «{name}»?\n\n"
            "Se eliminará el registro y sus URLs del índice.\n"
            "Los archivos descargados NO se borrarán.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            asyncio.ensure_future(self._do_delete(profile_id))


# ── Helpers ────────────────────────────────────────────────────────────────


def _right_item(text: str) -> QTableWidgetItem:
    """Item de tabla alineado a la derecha."""
    item = QTableWidgetItem(text)
    item.setTextAlignment(
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    )
    return item


def _fmt_size(n: int) -> str:
    size: float = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"
