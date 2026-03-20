"""
Tema QSS de cherry-dl.

Paleta:
  Fondo profundo  #0f0f1a
  Fondo panel     #1a1a2e
  Fondo elemento  #16213e
  Acento cherry   #e75480
  Acento hover    #ff6b9d
  Texto principal #e0e0e0
  Texto secundario #888888
  Borde           #2a2a42
"""

STYLESHEET = """
/* ── Base ───────────────────────────────────────────────────────── */

QMainWindow {
    background-color: #0f0f1a;
}

QWidget {
    background-color: transparent;
    color: #e0e0e0;
    font-family: "Segoe UI", "Noto Sans", "Ubuntu", sans-serif;
    font-size: 13px;
}

/* ── Botones ────────────────────────────────────────────────── */

QPushButton {
    background-color: #1a1a2e;
    color: #c0c0d8;
    border: 1px solid #2a2a42;
    border-radius: 6px;
    padding: 6px 16px;
    min-height: 30px;
}

QPushButton:hover {
    background-color: #24243e;
    border-color: #e75480;
    color: #e0e0e0;
}

QPushButton:pressed {
    background-color: #e75480;
    color: #ffffff;
    border-color: #e75480;
}

QPushButton:disabled {
    background-color: #12121e;
    color: #444466;
    border-color: #1e1e30;
}

QPushButton#btn_primary {
    background-color: #e75480;
    color: #ffffff;
    border: none;
    font-weight: bold;
}

QPushButton#btn_primary:hover {
    background-color: #ff6b9d;
}

QPushButton#btn_primary:pressed {
    background-color: #c2185b;
}

QPushButton#btn_danger {
    background-color: transparent;
    color: #e75480;
    border: 1px solid #e75480;
}

QPushButton#btn_danger:hover {
    background-color: #e75480;
    color: #ffffff;
}

/* ── Inputs ─────────────────────────────────────────────────── */

QLineEdit {
    background-color: #1a1a2e;
    border: 1px solid #2a2a42;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e0e0e0;
}

QLineEdit:focus {
    border-color: #e75480;
}

QLineEdit:disabled {
    background-color: #12121e;
    color: #444466;
}

QSpinBox {
    background-color: #1a1a2e;
    border: 1px solid #2a2a42;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e0e0e0;
}

QSpinBox:focus {
    border-color: #e75480;
}

QSpinBox::up-button, QSpinBox::down-button {
    background-color: #24243e;
    border: none;
    width: 24px;
}

QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: #e75480;
}

QDoubleSpinBox {
    background-color: #1a1a2e;
    border: 1px solid #2a2a42;
    border-radius: 6px;
    padding: 6px 10px;
    color: #e0e0e0;
}

QDoubleSpinBox:focus {
    border-color: #e75480;
}

QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    background-color: #24243e;
    border: none;
    width: 24px;
}

QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #e75480;
}

/* ── Tabla ──────────────────────────────────────────────────── */

QTableWidget {
    background-color: #12122a;
    alternate-background-color: #15152e;
    border: 1px solid #2a2a42;
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: #e75480;
    selection-color: #ffffff;
    outline: none;
}

QTableWidget::item {
    padding: 8px 12px;
    border: none;
}

QTableWidget::item:selected {
    background-color: #e75480;
    color: #ffffff;
}

QTableWidget::item:hover:!selected {
    background-color: #1e1e38;
}

QHeaderView {
    background-color: transparent;
}

QHeaderView::section {
    background-color: #1a1a2e;
    color: #8888aa;
    font-weight: bold;
    font-size: 11px;
    letter-spacing: 0.5px;
    padding: 8px 12px;
    border: none;
    border-bottom: 1px solid #2a2a42;
    text-transform: uppercase;
}

QHeaderView::section:first {
    border-top-left-radius: 8px;
}

QHeaderView::section:last {
    border-top-right-radius: 8px;
}

/* ── Scrollbar ──────────────────────────────────────────── */

QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}

QScrollBar::handle:vertical {
    background: #2a2a42;
    border-radius: 4px;
    min-height: 30px;
}

QScrollBar::handle:vertical:hover {
    background: #e75480;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background: transparent;
    height: 8px;
}

QScrollBar::handle:horizontal {
    background: #2a2a42;
    border-radius: 4px;
    min-width: 30px;
}

QScrollBar::handle:horizontal:hover {
    background: #e75480;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ── Labels ─────────────────────────────────────────────────── */

QLabel#lbl_title {
    font-size: 22px;
    font-weight: bold;
    color: #e0e0e0;
}

QLabel#lbl_section {
    font-size: 11px;
    font-weight: bold;
    color: #8888aa;
    letter-spacing: 0.8px;
    text-transform: uppercase;
    padding-bottom: 4px;
}

QLabel#lbl_subtitle {
    font-size: 12px;
    color: #666688;
}

QLabel#lbl_status {
    font-size: 11px;
    color: #666688;
}

QLabel#lbl_cherry {
    font-size: 13px;
    color: #e75480;
}

/* ── Separador ──────────────────────────────────────────── */

QFrame#separator {
    background-color: #2a2a42;
    max-height: 1px;
    border: none;
}

/* ── Barra de actividad global ───────────────────────────── */

QFrame#activity_bar {
    background-color: #12121e;
    border-top: 1px solid #1e1e30;
}

/* ── ProgressBar ──────────────────────────────────────────── */

QProgressBar {
    background-color: #1a1a2e;
    border: 1px solid #2a2a42;
    border-radius: 4px;
    text-align: center;
    color: #e0e0e0;
    font-size: 11px;
    height: 8px;
}

QProgressBar::chunk {
    background-color: #e75480;
    border-radius: 4px;
}
"""
