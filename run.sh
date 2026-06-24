#!/usr/bin/env bash
# cherry-dl launcher (portable — Linux / macOS / Windows vía Git Bash)
#
# Filosofía: CERO dependencia de instalaciones del sistema. Todo el runtime
# (uv + Python + venv) vive dentro del proyecto en .tools/ y .venv/, y se
# regenera por OS. No se usa el Python del sistema ni el directorio global de uv.
#
# Flujo:
#   1. Instala uv en .tools/ si no está (binario por plataforma)
#   2. uv instala un Python 3.12 standalone DENTRO de .tools/python
#   3. uv crea/actualiza el venv del proyecto con las dependencias
#   4. Lanza cherry-dl

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR/.tools"
PYTHON_VERSION="3.12"

# Python gestionado por uv → dentro del proyecto (portable, no en el home).
export UV_PYTHON_INSTALL_DIR="$TOOLS_DIR/python"
# No tocar el PATH del sistema al instalar uv.
export UV_NO_MODIFY_PATH=1

# ── 0. Detección de OS ────────────────────────────────────────────────────────
# uname: Linux / Darwin       → venv en bin/,     binario `uv`
#        MINGW*/MSYS*/CYGWIN*  → venv en Scripts/, `uv.exe`  (Git Bash)
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1; EXE=".exe"; VENV_BIN="Scripts" ;;
    *)                    IS_WINDOWS=0; EXE="";     VENV_BIN="bin"     ;;
esac
UV="$TOOLS_DIR/uv$EXE"

echo "========================================="
echo "  cherry-dl launcher"
echo "========================================="
echo "  OS: $(uname -s)  |  venv: $VENV_BIN/"

# ── 1. Instalar uv si no está ─────────────────────────────────────────────────
echo "[1/4] Verificando uv..."
if [ ! -f "$UV" ]; then
    echo "  Descargando uv (gestor de entornos Python)..."
    mkdir -p "$TOOLS_DIR"

    if [ "$IS_WINDOWS" -eq 1 ]; then
        # Windows: descargar el binario standalone directo con curl + unzip.
        # (Se evita install.ps1: Windows PowerShell 5.1 falla al invocarse desde
        # Git Bash por carga de módulos.)
        UV_ZIP="$TOOLS_DIR/uv.zip"
        UV_URL="https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
        curl -fsSL "$UV_URL" -o "$UV_ZIP"
        unzip -oq "$UV_ZIP" -d "$TOOLS_DIR"   # contiene uv.exe + uvx.exe
        rm -f "$UV_ZIP"
    else
        # Linux / macOS: instalador POSIX oficial.
        if command -v curl &>/dev/null; then
            curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$TOOLS_DIR" sh
        elif command -v wget &>/dev/null; then
            wget -qO- https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$TOOLS_DIR" sh
        else
            echo "ERROR: Se necesita curl o wget para descargar uv."
            exit 1
        fi
    fi

    if [ ! -f "$UV" ]; then
        echo "ERROR: uv no quedó en $UV. Revisa la salida del instalador."
        exit 1
    fi
    echo "  OK: uv instalado en $TOOLS_DIR"
else
    echo "  OK: uv presente ($("$UV" --version))"
fi

# ── 2. Verificar/instalar Python gestionado por uv ───────────────────────────
echo "[2/4] Verificando Python $PYTHON_VERSION (standalone en .tools/python)..."
if ! "$UV" python find "$PYTHON_VERSION" &>/dev/null 2>&1; then
    echo "  Instalando Python $PYTHON_VERSION (esto solo ocurre una vez)..."
    "$UV" python install "$PYTHON_VERSION"
    echo "  OK: Python $PYTHON_VERSION instalado."
else
    PYPATH=$("$UV" python find "$PYTHON_VERSION")
    echo "  OK: Python $PYTHON_VERSION en $PYPATH"
fi

# ── 3. Sincronizar venv y dependencias ───────────────────────────────────────
echo "[3/4] Verificando entorno virtual y dependencias..."
cd "$SCRIPT_DIR"

# Desactivar VIRTUAL_ENV del sistema para que uv use el venv del proyecto
unset VIRTUAL_ENV

# uv sync crea/actualiza el venv y verifica que todas las deps estén instaladas
"$UV" sync --python "$PYTHON_VERSION" --quiet

echo "  OK: entorno listo."

# ── 4. Lanzar cherry-dl ───────────────────────────────────────────────────────
echo "[4/4] Lanzando cherry-dl..."
echo "========================================="

# Sin argumentos → abrir TUI. Con argumentos → modo CLI.
if [ $# -eq 0 ]; then
    "$UV" run --python "$PYTHON_VERSION" python -m cherry_dl tui
else
    "$UV" run --python "$PYTHON_VERSION" python -m cherry_dl "$@"
fi
