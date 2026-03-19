#!/usr/bin/env bash
# cherry-dl launcher
# Gestiona su propio entorno Python via uv — independiente del sistema.
#
# Flujo:
#   1. Instala uv en .tools/ si no está presente
#   2. uv instala Python 3.12 dentro del proyecto si no está
#   3. uv crea/actualiza el venv con las dependencias
#   4. Lanza cherry-dl

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$SCRIPT_DIR/.tools"
UV="$TOOLS_DIR/uv"
PYTHON_VERSION="3.12"

echo "========================================="
echo "  cherry-dl launcher"
echo "========================================="

# ── 1. Instalar uv si no está ─────────────────────────────────────────────────
echo "[1/4] Verificando uv..."
if [ ! -f "$UV" ]; then
    echo "  Descargando uv (gestor de entornos Python)..."
    mkdir -p "$TOOLS_DIR"

    if command -v curl &>/dev/null; then
        curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$TOOLS_DIR" sh
    elif command -v wget &>/dev/null; then
        wget -qO- https://astral.sh/uv/install.sh | UV_INSTALL_DIR="$TOOLS_DIR" sh
    else
        echo "ERROR: Se necesita curl o wget para descargar uv."
        exit 1
    fi

    echo "  OK: uv instalado en $TOOLS_DIR"
else
    echo "  OK: uv presente ($("$UV" --version))"
fi

# ── 2. Verificar/instalar Python gestionado por uv ───────────────────────────
echo "[2/4] Verificando Python $PYTHON_VERSION..."
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

# Sin argumentos → abrir GUI. Con argumentos → modo CLI.
if [ $# -eq 0 ]; then
    "$UV" run --python "$PYTHON_VERSION" python -m cherry_dl gui
else
    "$UV" run --python "$PYTHON_VERSION" python -m cherry_dl "$@"
fi
