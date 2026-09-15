"""組裝可攜式發佈包：官方簽章的 python.exe + 原始碼 + 預裝套件。

為什麼不用 PyInstaller：PyInstaller 產出的 exe 沒有程式碼簽章，
且每次 build 的 hash 都是新的，會被 Symantec 之類的信譽式防毒判為
Unproven.LowPrevalence 直接隔離。改用這個做法後，實際被執行的
是 python.org 官方簽章、全球普及度極高的 pythonw.exe，繞開整個
信譽判定；改版時也不會重新中一次。

產出結構：
    PocketAgent/
    ├── start.bat            啟動器（雙擊這個）
    ├── PORTABLE             給 agent/paths.py 認的標記檔
    ├── python/              CPython 複本（含 tkinter、tcl/tk）
    └── app/                 原始碼與內建資源

用法： venv\\Scripts\\python.exe build_portable.py
"""

import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "dist" / "PocketAgent"

# base_prefix 才是真正的 Python 安裝位置（venv 的 prefix 指向 venv 自己）
BASE_PYTHON = Path(sys.base_prefix)
VENV_SITE_PACKAGES = ROOT / "venv" / "Lib" / "site-packages"

# 執行期用不到，佔空間就砍。tcl/ 不能砍——tkinter 要用。
PYTHON_EXCLUDES = {
    "Doc",
    "Tools",
    "include",
    "libs",
    "Scripts",
    "test",
    "idlelib",
    "tkinter/test",
}

# 要帶進 app/ 的原始碼與資源
APP_ITEMS = [
    "src",
    "browser_mcp.py",
    "gui",
    "skills",
    "commands",
    "subagents",
    "mcp_config.example.yaml",
    ".env.example",
    "requirements.txt",  # 讓「自動更新」之後能對照/補裝新套件
]

START_BAT = """@echo off
REM Launcher for the portable build.
REM
REM pythonw.exe (not python.exe) so no console window appears.
REM `start ""` detaches, letting this cmd window close immediately.
REM
REM ASCII-only on purpose: cmd.exe parses .bat with the OEM codepage
REM (Big5 on zh-TW), which mangles UTF-8 Chinese into garbage commands.
setlocal
cd /d "%~dp0"

REM Isolate from whatever Python the user already has. Without this the
REM bundled interpreter still picks up %APPDATA%\\Python\\PythonXY\\site-packages
REM and %PYTHONPATH%, so a colleague with a different uvicorn or httpx
REM installed gets different behaviour from what we shipped and tested.
REM PYTHONHOME would be worse still: it repoints at another installation.
set PYTHONNOUSERSITE=1
set PYTHONPATH=%~dp0app\src
set PYTHONHOME=

start "" "%~dp0python\\pythonw.exe" -m pocket_agent %*
endlocal
"""


def _ignore(path, names):
    """複製時略過不需要的目錄與 __pycache__。"""
    skip = {n for n in names if n in PYTHON_EXCLUDES or n == "__pycache__"}
    return skip


def main() -> int:
    if not VENV_SITE_PACKAGES.is_dir():
        print(f"[錯誤] 找不到 {VENV_SITE_PACKAGES}")
        print("      請先執行： py -m venv venv")
        print("                venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1

    print(f"基礎 Python： {BASE_PYTHON}")
    if OUT.exists():
        print("清除舊的 dist/PocketAgent ...")
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 1. Python 本體
    print("[1/5] 複製 Python 執行環境 ...")
    shutil.copytree(BASE_PYTHON, OUT / "python", ignore=_ignore)

    # 2. 套件（合併進 python/Lib/site-packages，執行期就不必設 PYTHONPATH）
    print("[2/5] 複製已安裝套件 ...")
    target_sp = OUT / "python" / "Lib" / "site-packages"
    target_sp.mkdir(parents=True, exist_ok=True)
    for item in VENV_SITE_PACKAGES.iterdir():
        # pip/setuptools/wheel 保留：自動更新之後要能在原地補裝新套件
        # （agent/updater.py 的 _maybe_update_deps 靠 python -m pip 運作）。
        # PyInstaller 只是舊的打包工具鏈，純屬佔空間，跳過。
        if item.name in {"__pycache__", "PyInstaller"}:
            continue
        if item.name.startswith("pyinstaller"):
            continue
        dest = target_sp / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    # 3. 原始碼
    print("[3/5] 複製程式碼 ...")
    app = OUT / "app"
    app.mkdir()
    for name in APP_ITEMS:
        src = ROOT / name
        if not src.exists():
            print(f"      ! 略過不存在的 {name}")
            continue
        if src.is_dir():
            shutil.copytree(src, app / name, ignore=_ignore)
        else:
            shutil.copy2(src, app / name)

    # 4. 啟動器與標記檔
    print("[4/5] 產生啟動器 ...")
    (OUT / "start.bat").write_text(START_BAT, encoding="ascii")
    (app / "PORTABLE").write_text(
        "可攜式發佈版標記。agent/paths.py 靠這個檔判斷要把設定與 log\n寫到 ~/.pocket-agent 而非程式目錄。請勿刪除。\n",
        encoding="utf-8",
    )

    # 5. 打包
    print("[5/5] 壓縮 ...")
    zip_path = ROOT / "dist" / "PocketAgent-portable.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in OUT.rglob("*"):
            if f.is_file():
                zf.write(f, Path("PocketAgent") / f.relative_to(OUT))

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print()
    print(f"完成： {zip_path}  （{size_mb:.0f} MB）")
    print("同事解壓縮後雙擊 PocketAgent\\start.bat 即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
