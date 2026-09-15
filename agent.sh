#!/usr/bin/env bash
# 發生錯誤即停止執行
set -e

# 切換到腳本所在目錄
cd "$(dirname "$0")"

VENV_PY="venv/bin/python"

if [ -f "$VENV_PY" ]; then
    RUN_PY="$VENV_PY"
else
    echo "============================================================"
    echo "[INFO] Virtual environment not found. Detecting Python..."
    echo "============================================================"

    # 偵測 Python 執行檔
    if command -v python3 >/dev/null 2>&1; then
        SYS_PY="python3"
    elif command -v python >/dev/null 2>&1; then
        SYS_PY="python"
    else
        echo "[ERROR] Neither 'python3' nor 'python' was found in your PATH."
        echo "Please install Python 3.11+ (e.g., dnf install python3.11 on RHEL)."
        echo "============================================================"
        exit 1
    fi

    echo "[INFO] Found Python runtime: $SYS_PY"

    # 偵測建立虛擬環境的工具
    CREATE_CMD=""
    if $SYS_PY -c "import venv" >/dev/null 2>&1; then
        echo "[INFO] Detected standard library module: venv"
        CREATE_CMD="$SYS_PY -m venv venv"
    elif command -v virtualenv >/dev/null 2>&1; then
        echo "[INFO] Detected 'virtualenv' executable."
        CREATE_CMD="virtualenv venv"
    elif command -v uv >/dev/null 2>&1; then
        echo "[INFO] Detected 'uv' executable."
        CREATE_CMD="uv venv venv"
    fi

    # 建立虛擬環境
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

    # 確認虛擬環境內的 Python 執行檔是否存在
    if [ -z "$RUN_PY" ]; then
        if [ ! -f "$VENV_PY" ]; then
            echo "[WARNING] $VENV_PY was not created. Falling back to system Python."
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

export PYTHONPATH="$(pwd)/src"
"$RUN_PY" -m pocket_agent "$@"
