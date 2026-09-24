#!/usr/bin/env bash
# 發生錯誤即停止執行
set -e

# 切換到腳本所在目錄，確保 Mac 雙擊執行時路徑正確
cd "$(dirname "$0")"

VENV_PY="venv/bin/python"

if [ -f "$VENV_PY" ]; then
    RUN_PY="$VENV_PY"
else
    echo "============================================================"
    echo "[INFO] Virtual environment not found. Detecting Python..."
    echo "============================================================"

    # 偵測 Mac 上常見的 Python 執行檔 (Homebrew 或官方版)
    if command -v python3 >/dev/null 2>&1; then
        SYS_PY="python3"
    elif command -v python >/dev/null 2>&1; then
        SYS_PY="python"
    else
        echo "[ERROR] Neither 'python3' nor 'python' was found in your PATH."
        echo "Please install Python 3.11+ via Homebrew (brew install python3)."
        echo "============================================================"
        exit 1
    fi

    echo "[INFO] Found Python runtime: $SYS_PY"

    CREATE_CMD=""
    if $SYS_PY -c "import venv" >/dev/null 2>&1; then
        CREATE_CMD="$SYS_PY -m venv venv"
    elif command -v virtualenv >/dev/null 2>&1; then
        CREATE_CMD="virtualenv venv"
    elif command -v uv >/dev/null 2>&1; then
        CREATE_CMD="uv venv venv"
    fi

    if [ -n "$CREATE_CMD" ]; then
        echo "[INFO] Creating virtual environment: $CREATE_CMD"
        if ! $CREATE_CMD; then
            echo "[WARNING] Failed to create virtual environment."
            RUN_PY="$SYS_PY"
        fi
    else
        echo "[WARNING] No venv module or virtualenv tool found."
        RUN_PY="$SYS_PY"
    fi

    if [ -z "$RUN_PY" ]; then
        if [ ! -f "$VENV_PY" ]; then
            RUN_PY="$SYS_PY"
        else
            RUN_PY="$VENV_PY"
        fi
    fi

    echo "[INFO] Upgrading pip..."
    "$RUN_PY" -m pip install --upgrade pip

    echo "[INFO] Installing dependencies from requirements.txt ..."
    if ! "$RUN_PY" -m pip install -r requirements.txt; then
        echo "[ERROR] Failed to install dependencies."
        exit 1
    fi

    echo "[INFO] Environment setup completed!"
    echo "============================================================"
fi

PYTHONPATH="$(pwd)/src"
export PYTHONPATH
"$RUN_PY" -m pocket_agent "$@"
