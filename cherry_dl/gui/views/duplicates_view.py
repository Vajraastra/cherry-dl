"""
Vista de detección y fusión de perfiles duplicados (port de la DuplicateScreen
de la TUI sobre PySide6).

Fase 1 (automática al mostrar): compara URLs (site+artist_id) y similitud de
nombre de todos los pares N×N.
  - URL match → "Fusión automática"
  - nombre ≥ 0.80 / ≥ 0.60 → "Revisión manual"
Fase 2 (manual, bajo demanda): compare_by_hash_join (SQL ATTACH + INNER JOIN)
sobre los pares no-definitivos.
  - coverage ≥ 0.51 → pasa a fusión automática
  - coverage 0.10–0.50 → revisión manual con porcentaje

Ejecutar fusiones: migrate_unique_files → gestión de huérfanos → merge_profiles
→ ofrece compactar.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...catalog import compare_by_hash_join, migrate_unique_files
from ...config import INDEX_DB
from ...dedup import (
    compact_folders,
    dup_keep_remove,
    handle_orphans,
    name_similarity,
    url_overlap,
)
from ...index import (
    add_exclusion,
    get_exclusions,
    get_profile,
    init_index,
    list_profiles,
    merge_profiles,
)


class DuplicatesView(QWidget):
    """Detección y fusión de perfiles duplicados."""

    HASH_AUTO = 0.51   # coverage ≥ → fusión automática
    HASH_MIN = 0.10    # coverage < → ruido, ignorar
    NAME_PROB = 0.80   # nombre similar ≥ → probable
    NAME_POSS = 0.60   # nombre similar ≥ → posible

    def __init__(self, nav: Callable, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._nav = nav
        self._loaded = False
        self._busy = False
        self._auto: list[dict] = []
        self._review: list[dict] = []
        self._phase2_candidates: list[dict] = []
        self._exclusions: set[tuple[int, int]] = set()
        self._setup_ui()

    # ── Construcción de UI ───────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        btn_back = QPushButton("← Volver")
        btn_back.clicked.connect(lambda: self._nav("profiles"))
        lbl_title = QLabel("Buscar perfiles duplicados")
        lbl_title.setObjectName("lbl_title")
        header.addWidget(btn_back)
        header.addWidget(lbl_title)
        header.addStretch()
        root.addLayout(header)

        self._status = QLabel("")
        self._status.setObjectName("lbl_subtitle")
        root.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        root.addWidget(self._progress)

        # ── Sección automática ─────────────────────────────────────────────
        lbl_auto = QLabel("FUSIÓN AUTOMÁTICA  (URL match o ≥51% hashes en común)")
        lbl_auto.setObjectName("lbl_section")
        root.addWidget(lbl_auto)
        self._auto_table = self._make_table(["Mantener", "←", "Eliminar", "Razón"])
        root.addWidget(self._auto_table, stretch=1)

        # ── Sección revisión ───────────────────────────────────────────────
        lbl_rev = QLabel("REVISIÓN MANUAL  (nombre similar o hash 10–50%)")
        lbl_rev.setObjectName("lbl_section")
        root.addWidget(lbl_rev)
        self._review_table = self._make_table(["✓", "Perfil A", "↔", "Perfil B", "Similitud"])
        self._review_table.itemChanged.connect(self._on_review_item_changed)
        root.addWidget(self._review_table, stretch=1)

        # ── Acciones ───────────────────────────────────────────────────────
        actions = QHBoxLayout()
        self._btn_hash = QPushButton("↺ Comparar por hashes")
        self._btn_exec_auto = QPushButton("✓ Ejecutar fusiones auto")
        self._btn_exec_auto.setObjectName("btn_primary")
        self._btn_exec_auto.setEnabled(False)
        self._btn_exec_manual = QPushButton("⟳ Fusionar seleccionados")
        self._btn_exec_manual.setEnabled(False)
        self._btn_exclude = QPushButton("✕ Marcar como distintos")
        self._btn_exclude.setEnabled(False)
        actions.addWidget(self._btn_hash)
        actions.addStretch()
        actions.addWidget(self._btn_exclude)
        actions.addWidget(self._btn_exec_manual)
        actions.addWidget(self._btn_exec_auto)
        root.addLayout(actions)

        self._btn_hash.clicked.connect(self._on_hash_scan)
        self._btn_exec_auto.clicked.connect(self._on_exec_auto)
        self._btn_exec_manual.clicked.connect(self._on_exec_manual)
        self._btn_exclude.clicked.connect(self._on_exclude)

    def _make_table(self, headers: list[str]) -> QTableWidget:
        t = QTableWidget()
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.verticalHeader().setVisible(False)
        hdr = t.horizontalHeader()
        hdr.setStretchLastSection(True)
        return t

    # ── Ciclo de vida ──────────────────────────────────────────────────────

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._loaded:
            QTimer.singleShot(0, self._start_phase1)

    def reset(self) -> None:
        self._loaded = False

    def _start_phase1(self) -> None:
        self._loaded = True
        self._auto.clear()
        self._review.clear()
        self._phase2_candidates.clear()
        self._auto_table.setRowCount(0)
        self._review_table.setRowCount(0)
        task = asyncio.ensure_future(self._scan_phase1())
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._status.setText(f"Error: {exc}")
            import traceback
            traceback.print_exception(type(exc), exc, exc.__traceback__)

    # ── Helpers de UI ───────────────────────────────────────────────────────

    def _set_progress(self, done: int, total: int) -> None:
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(done)

    def _refresh_buttons(self) -> None:
        any_checked = any(p.get("checked") for p in self._review)
        self._btn_exec_auto.setEnabled(bool(self._auto) and not self._busy)
        self._btn_exec_manual.setEnabled(any_checked and not self._busy)
        self._btn_exclude.setEnabled(any_checked and not self._busy)
        self._btn_hash.setEnabled(not self._busy)

    def _excluded(self, id_a: int, id_b: int) -> bool:
        return (min(id_a, id_b), max(id_a, id_b)) in self._exclusions

    def _add_auto_row(self, pair: dict) -> None:
        keep_id, remove_id, keep_name, remove_name = dup_keep_remove(pair)
        t = self._auto_table
        r = t.rowCount()
        t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(keep_name))
        t.setItem(r, 1, QTableWidgetItem("←"))
        t.setItem(r, 2, QTableWidgetItem(remove_name))
        t.setItem(r, 3, QTableWidgetItem(pair.get("reason", "")))

    def _add_review_row(self, pair: dict) -> None:
        t = self._review_table
        r = t.rowCount()
        t.blockSignals(True)
        t.insertRow(r)
        chk = QTableWidgetItem()
        chk.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
        )
        chk.setCheckState(Qt.CheckState.Unchecked)
        # Guarda la clave del par para mapear el toggle de vuelta.
        chk.setData(Qt.ItemDataRole.UserRole, (pair["id_a"], pair["id_b"]))
        t.setItem(r, 0, chk)
        t.setItem(r, 1, QTableWidgetItem(pair["name_a"]))
        t.setItem(r, 2, QTableWidgetItem("↔"))
        t.setItem(r, 3, QTableWidgetItem(pair["name_b"]))
        t.setItem(r, 4, QTableWidgetItem(pair.get("reason", "")))
        t.blockSignals(False)

    def _find_review_pair(self, id_a: int, id_b: int) -> dict | None:
        for p in self._review:
            if {p["id_a"], p["id_b"]} == {id_a, id_b}:
                return p
        return None

    # ── Fase 1: URL + nombre ─────────────────────────────────────────────────

    async def _scan_phase1(self) -> None:
        self._busy = True
        self._refresh_buttons()
        self._status.setText("Fase 1: comparando URLs y nombres…")
        await init_index(INDEX_DB)
        self._exclusions = await get_exclusions(INDEX_DB)

        slim = await list_profiles(INDEX_DB)
        full: list[dict] = []
        for p in slim:
            fp = await get_profile(INDEX_DB, p["id"])
            if fp:
                full.append(fp)

        n = len(full)
        total_pairs = n * (n - 1) // 2
        done = 0

        for i in range(n):
            for j in range(i + 1, n):
                a, b = full[i], full[j]
                done += 1
                if self._excluded(a["id"], b["id"]):
                    continue

                base = {
                    "id_a": a["id"], "name_a": a["display_name"],
                    "folder_a": a["folder_path"], "created_at_a": a.get("created_at"),
                    "id_b": b["id"], "name_b": b["display_name"],
                    "folder_b": b["folder_path"], "created_at_b": b.get("created_at"),
                }

                match = url_overlap(a["urls"], b["urls"])
                if match:
                    pair = {**base, "tier": "url_match", "reason": f"URL: {match}"}
                    self._auto.append(pair)
                    self._add_auto_row(pair)
                    self._set_progress(done, total_pairs)
                    continue

                sim = name_similarity(a["display_name"], b["display_name"])
                if sim >= self.NAME_POSS:
                    tier = "name_similar" if sim >= self.NAME_PROB else "name_possible"
                    pair = {**base, "tier": tier,
                            "reason": f"nombre {sim*100:.0f}% similar", "checked": False}
                    self._review.append(pair)
                    self._phase2_candidates.append(pair)
                    self._add_review_row(pair)
                else:
                    self._phase2_candidates.append({**base, "tier": "unknown", "reason": ""})

                self._set_progress(done, total_pairs)

        self._set_progress(total_pairs, total_pairs)
        self._status.setText(
            f"Fase 1 completa — {len(self._auto)} para fusión automática · "
            f"{len(self._review)} para revisión · {total_pairs} pares analizados"
        )
        self._busy = False
        self._refresh_buttons()

    # ── Fase 2: hashes ───────────────────────────────────────────────────────

    def _on_hash_scan(self) -> None:
        if self._busy:
            return
        reply = QMessageBox.question(
            self,
            "Comparación por hashes",
            "Esta fase compara los hashes SHA-256 ya almacenados en tus bases de "
            "datos — no escanea archivos en disco.\n\n"
            "Se ejecuta como una consulta SQL directa (INNER JOIN). Con catálogos "
            "de >50.000 archivos puede tardar varios minutos.\n\n¿Iniciar el scan?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        task = asyncio.ensure_future(self._scan_phase2())
        task.add_done_callback(self._on_task_done)

    async def _scan_phase2(self) -> None:
        self._busy = True
        self._refresh_buttons()
        candidates = [
            p for p in self._phase2_candidates
            if p.get("tier") != "url_match" and not self._excluded(p["id_a"], p["id_b"])
        ]
        total = len(candidates)
        if total == 0:
            self._status.setText("No hay pares candidatos para scan de hashes.")
            self._busy = False
            self._refresh_buttons()
            return

        self._status.setText(f"Fase 2: comparando hashes ({total} par(es))…")
        for done, pair in enumerate(candidates):
            try:
                result = await compare_by_hash_join(
                    Path(pair["folder_a"]), Path(pair["folder_b"])
                )
            except Exception as exc:
                print(f"Hash join error ({pair['name_a']} ↔ {pair['name_b']}): {exc}")
                self._set_progress(done + 1, total)
                continue

            coverage = result.get("coverage", 0.0)
            total_a = result.get("total_a", 0)
            total_b = result.get("total_b", 0)
            reason = (
                f"{coverage*100:.1f}% hashes en común "
                f"({result['matches']}/{min(total_a, total_b)})"
            )

            if coverage < self.HASH_MIN:
                pass
            elif coverage >= self.HASH_AUTO:
                new_pair = {**pair, "tier": "hash_definite", "reason": reason}
                self._auto.append(new_pair)
                self._add_auto_row(new_pair)
                self._drop_review_pair(pair["id_a"], pair["id_b"])
            else:
                existing = self._find_review_pair(pair["id_a"], pair["id_b"])
                if existing:
                    existing["reason"] = reason
                    self._update_review_reason(pair["id_a"], pair["id_b"], reason)
                else:
                    new_pair = {**pair, "tier": "hash_probable",
                                "reason": reason, "checked": False}
                    self._review.append(new_pair)
                    self._phase2_candidates.append(new_pair)
                    self._add_review_row(new_pair)

            self._set_progress(done + 1, total)
            self._refresh_buttons()

        self._status.setText(
            f"Fase 2 completa — {len(self._auto)} para fusión automática · "
            f"{len(self._review)} para revisión"
        )
        self._busy = False
        self._refresh_buttons()

    def _drop_review_pair(self, id_a: int, id_b: int) -> None:
        self._review = [
            p for p in self._review if {p["id_a"], p["id_b"]} != {id_a, id_b}
        ]
        self._rebuild_review_table()

    def _update_review_reason(self, id_a: int, id_b: int, reason: str) -> None:
        for r in range(self._review_table.rowCount()):
            item = self._review_table.item(r, 0)
            if item and tuple(item.data(Qt.ItemDataRole.UserRole)) == (id_a, id_b):
                self._review_table.item(r, 4).setText(reason)
                return

    def _rebuild_review_table(self) -> None:
        self._review_table.setRowCount(0)
        for pair in self._review:
            self._add_review_row(pair)
            if pair.get("checked"):
                r = self._review_table.rowCount() - 1
                self._review_table.blockSignals(True)
                self._review_table.item(r, 0).setCheckState(Qt.CheckState.Checked)
                self._review_table.blockSignals(False)

    # ── Toggle de revisión ───────────────────────────────────────────────────

    def _on_review_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        pair = self._find_review_pair(key[0], key[1])
        if pair is None:
            return
        pair["checked"] = item.checkState() == Qt.CheckState.Checked
        self._refresh_buttons()

    # ── Acciones ─────────────────────────────────────────────────────────────

    def _on_exec_auto(self) -> None:
        if self._auto and not self._busy:
            task = asyncio.ensure_future(self._execute_merges(list(self._auto)))
            task.add_done_callback(self._on_task_done)

    def _on_exec_manual(self) -> None:
        selected = [p for p in self._review if p.get("checked")]
        if selected and not self._busy:
            task = asyncio.ensure_future(self._execute_merges(selected))
            task.add_done_callback(self._on_task_done)

    def _on_exclude(self) -> None:
        selected = [p for p in self._review if p.get("checked")]
        if selected and not self._busy:
            task = asyncio.ensure_future(self._exclude_selected(selected))
            task.add_done_callback(self._on_task_done)

    async def _exclude_selected(self, selected: list[dict]) -> None:
        for pair in selected:
            await add_exclusion(INDEX_DB, pair["id_a"], pair["id_b"])
            self._exclusions.add(
                (min(pair["id_a"], pair["id_b"]), max(pair["id_a"], pair["id_b"]))
            )
        self._review = [p for p in self._review if not p.get("checked")]
        self._rebuild_review_table()
        self._status.setText(
            f"{len(selected)} par(es) marcados como distintos — no aparecerán de nuevo."
        )
        self._refresh_buttons()

    # ── Ejecutar fusiones ────────────────────────────────────────────────────

    async def _execute_merges(self, pairs: list[dict]) -> None:
        self._busy = True
        self._refresh_buttons()
        merged = 0
        total_moved = 0
        total_orphans = 0
        errors: list[str] = []
        folders_to_compact: list[tuple[int, str]] = []

        for pair in pairs:
            keep_id, remove_id, keep_name, remove_name = dup_keep_remove(pair)
            keep_folder = Path(pair["folder_a"] if pair["id_a"] == keep_id else pair["folder_b"])
            remove_folder = Path(pair["folder_b"] if pair["id_a"] == keep_id else pair["folder_a"])
            self._status.setText(f"Fusionando: {remove_name} → {keep_name}…")

            result = None
            try:
                result = await migrate_unique_files(remove_folder, keep_folder)
                total_moved += result["moved"]
                total_orphans += result["orphaned"]
                if result["errors"]:
                    errors.extend(result["errors"][:5])
                if result["orphaned"] > 0:
                    action = self._ask_orphan_action(str(remove_folder), result["orphaned"])
                    await asyncio.to_thread(
                        handle_orphans, remove_folder, result["orphaned_paths"], action
                    )
            except Exception as exc:
                errors.append(f"Migración {remove_name}: {exc}")

            try:
                await merge_profiles(INDEX_DB, keep_id, remove_id)
                merged += 1
                if result and result.get("moved", 0) > 0:
                    folders_to_compact.append((keep_id, str(keep_folder)))
            except Exception as exc:
                errors.append(f"Índice {remove_name}: {exc}")

        # Limpiar pares ejecutados
        merged_ids = {(p["id_a"], p["id_b"]) for p in pairs}
        self._auto = [p for p in self._auto if (p["id_a"], p["id_b"]) not in merged_ids]
        self._review = [p for p in self._review if (p["id_a"], p["id_b"]) not in merged_ids]
        self._rebuild_auto_table()
        self._rebuild_review_table()

        msg = (
            f"✓ {merged} fusión(es) — {total_moved} archivo(s) migrados · "
            f"{total_orphans} huérfanos gestionados"
        )
        if errors:
            msg += f"   ({len(errors)} error(es): {errors[0]})"
        self._status.setText(msg)

        self._busy = False
        self._refresh_buttons()

        if folders_to_compact:
            reply = QMessageBox.question(
                self,
                "¿Compactar numeración?",
                "Los archivos migrados se agregaron al final de la numeración.\n"
                "Compactar elimina los huecos en la secuencia. ¿Compactar ahora?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._status.setText("Compactando…")
                task = asyncio.ensure_future(self._compact_and_report(folders_to_compact))
                task.add_done_callback(self._on_task_done)

    async def _compact_and_report(self, folders: list[tuple[int, str]]) -> None:
        await compact_folders(folders)
        self._status.setText("✓ Compactación completa.")

    def _rebuild_auto_table(self) -> None:
        self._auto_table.setRowCount(0)
        for pair in self._auto:
            self._add_auto_row(pair)

    def _ask_orphan_action(self, folder: str, n_orphans: int) -> str:
        """Pregunta qué hacer con los huérfanos. Retorna delete|rename|ignore."""
        box = QMessageBox(self)
        box.setWindowTitle("Archivos huérfanos")
        box.setText(
            f"{n_orphans} archivo(s) en\n{folder}\n"
            "ya existen en el perfil destino (mismo hash).\n\n¿Qué deseas hacer?"
        )
        btn_del = box.addButton("Borrar", QMessageBox.ButtonRole.DestructiveRole)
        btn_ren = box.addButton("Renombrar carpeta", QMessageBox.ButtonRole.ActionRole)
        btn_ign = box.addButton("Ignorar", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(btn_ign)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_del:
            return "delete"
        if clicked is btn_ren:
            return "rename"
        return "ignore"
