"""
Diálogo nativo del OS para selección de carpeta.

Orden de preferencia:
  1. kdialog     — KDE / Dolphin  (Linux)
  2. zenity      — GTK / GNOME    (Linux)
  3. tkinter     — fallback cross-platform

El diálogo se abre en un hilo daemon para no bloquear el render loop de DPG.
La ruta elegida se entrega via `callback(path: str)`.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from typing import Callable


def pick_directory(
    title: str,
    callback: Callable[[str], None],
    start_dir: str = "",
) -> None:
    """
    Abre el selector de carpeta nativo del OS en un hilo daemon.
    Llama a `callback(path)` cuando el usuario elige una carpeta.
    Si el usuario cancela, `callback` no se llama.
    """
    def _run() -> None:
        path = _open_dialog(title, start_dir)
        if path:
            callback(path)

    threading.Thread(target=_run, daemon=True, name="native-dialog").start()


# ── Backends ──────────────────────────────────────────────────────────────────

def _open_dialog(title: str, start_dir: str) -> str:
    """Intenta cada backend en orden; retorna la ruta elegida o '' si cancela."""

    if shutil.which("kdialog"):
        result = _try_kdialog(title, start_dir)
        if result is not None:
            return result

    if shutil.which("zenity"):
        result = _try_zenity(title, start_dir)
        if result is not None:
            return result

    return _try_tkinter(title, start_dir)


def _try_kdialog(title: str, start_dir: str) -> str | None:
    """kdialog --getexistingdirectory (KDE)."""
    import os
    try:
        cmd = ["kdialog", "--getexistingdirectory", start_dir or ".", "--title", title]
        # Silenciar warnings de Qt sobre registro de portal DBus
        env = os.environ.copy()
        env["QT_LOGGING_RULES"] = "qt.qpa.services=false;qt.dbus=false"
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if proc.returncode == 0:
            return proc.stdout.strip()
        # returncode 1 = usuario canceló
        return ""
    except Exception:
        return None   # backend no disponible


def _try_zenity(title: str, start_dir: str) -> str | None:
    """zenity --file-selection --directory (GTK)."""
    try:
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if start_dir:
            cmd += [f"--filename={start_dir}/"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
        return ""
    except Exception:
        return None


def _try_tkinter(title: str, start_dir: str) -> str:
    """tkinter.filedialog como último recurso."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title=title, initialdir=start_dir or None)
        root.destroy()
        return path or ""
    except Exception:
        return ""
