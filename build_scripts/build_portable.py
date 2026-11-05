r"""組裝可攜式發佈包：官方簽章的 python.exe + 原始碼 + 預裝套件。

為什麼不用 PyInstaller：PyInstaller 產出的 exe 沒有程式碼簽章，
且每次 build 的 hash 都是新的，會被 Symantec 之類的信譽式防毒判為
Unproven.LowPrevalence 直接隔離。改用這個做法後，實際被執行的
是 python.org 官方簽章、全球普及度極高的 pythonw.exe，繞開整個
信譽判定；改版時也不會重新中一次。

產出結構：
    PocketAgent/
    ├── pocket.bat           啟動器（終端機下指令或雙擊皆可）
    ├── PORTABLE             給 agent/paths.py 認的標記檔
    ├── python/              CPython 複本（含 tkinter、tcl/tk）
    └── app/                 原始碼與內建資源

用法： venv\Scripts\python.exe build_scripts\build_portable.py
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

# 因為本腳本放在 build_scripts/ 下，所以專案根目錄是上一層
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "PocketAgent"

# base_prefix 才是真正的 Python 安裝位置（venv 的 prefix 指向 venv 自己）
BASE_PYTHON = Path(sys.base_prefix)
VENV_SITE_PACKAGES = ROOT / "venv" / "Lib" / "site-packages"

# 執行期用不到的 CPython 內部模組或大型目錄（含全域 site-packages，避免混入非 venv 套件）
PYTHON_EXCLUDES = {
    "Doc",
    "Tools",
    "include",
    "libs",
    "Scripts",
    "test",
    "idlelib",
    "tkinter",
    "tcl",
    "turtle",
    "turtledemo",
    "unittest",
    "ensurepip",
    "pydoc_data",
    "site-packages",
}

# 開發、測試、打包工具與已排除之大型選用套件（不流入終端發布包）
# 註 1：pip / setuptools / wheel 刻意保留，讓線上更新在 requirements.txt 變動時能原地補裝新套件。
# 註 2：排除 pymupdf_layout / onnxruntime / numpy / networkx 後，pymupdf4llm 會自動降級使用純 pymupdf。
DEV_PACKAGES = {
    "pytest",
    "_pytest",
    "ruff",
    "coverage",
    "piptools",
    "pip_tools",
    "build",
    "pyproject_hooks",
    "iniconfig",
    "pluggy",
    "pytest_cov",
    "flit_core",
    "pyinstaller",
    "_pyinstaller_hooks_contrib",
    "pyinstaller_hooks_contrib",
    "altgraph",
    "pefile",
    "playwright",
    "greenlet",
    "pyee",
    "pymupdf_layout",
    "onnxruntime",
    "numpy",
    "numpy.libs",
    "networkx",
    "flatbuffers",
    "protobuf",
    "google",
}

# 應過濾的非執行期資料夾（例如套件內建的測試、範例與 C++ 開發標頭檔）
UNWANTED_DIR_NAMES = {
    "__pycache__",
    "tests",
    "test",
    "testing",
    "examples",
    "demos",
    "benchmarks",
    "docs",
    "mupdf-devel",
}

# pymupdf 目錄下屬於 pymupdf-layout 的子目錄與擴充檔（排除後自動使用純 pymupdf）
PYMUPDF_LAYOUT_ITEMS = {
    "layout",
    "_features.pyd",
    "_tgif.pyd",
    "features.py",
    "tgif.py",
}

# 應過濾的檔案後綴（除錯符號、靜態庫、C/C++/Cython 原始碼與標頭檔、型別標記檔）
UNWANTED_EXTENSIONS = {
    ".pdb",
    ".lib",
    ".a",
    ".chm",
    ".h",
    ".c",
    ".cpp",
    ".cxx",
    ".pyc",
    ".pyo",
    ".pyi",
    ".pxd",
    ".pyx",
}

# 要帶進 app/ 的原始碼與資源
APP_ITEMS = [
    "src",
    "mcp_servers",
    "subagents",
    "mcp_config.example.yaml",
    ".env.example",
    "requirements.txt",  # 讓「自動更新」之後能對照/補裝新套件
]

POCKET_BAT = r"""@echo off
REM Launcher for the portable build.
REM
REM ASCII-only on purpose: cmd.exe parses .bat with the OEM codepage
REM (Big5 on zh-TW), which mangles UTF-8 Chinese into garbage commands.
setlocal


REM Isolate from whatever Python the user already has. Without this the
REM bundled interpreter still picks up %APPDATA%\Python\PythonXY\site-packages
REM and %PYTHONPATH%, so a colleague with a different uvicorn or httpx
REM installed gets different behaviour from what we shipped and tested.
REM PYTHONHOME would be worse still: it repoints at another installation.
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH=%~dp0app\src"
set "PYTHONHOME="
for %%I in ("%~dp0.") do set "PA_ROOT=%%~fI"
set "PA_HANDOFF_DIR=%PA_ROOT%"
if defined TEMP for %%T in ("%TEMP%\.") do if exist "%%~fT\." set "PA_HANDOFF_DIR=%%~fT"

REM Run directly in current console window
set "PA_IN_PLACE_LAUNCHER=1"
set "PA_UPDATE_HANDOFF=%PA_HANDOFF_DIR%\pocket_agent_handoff_%RANDOM%_%RANDOM%.bat"
if exist "%PA_UPDATE_HANDOFF%" del /f /q "%PA_UPDATE_HANDOFF%" >nul 2>&1
"%~dp0python\python.exe" -m pocket_agent %*
set "EXIT_CODE=%ERRORLEVEL%"
goto POST_RUN

:RUN_RESUME
set "PA_IN_PLACE_LAUNCHER=1"
set "PA_UPDATE_HANDOFF=%PA_HANDOFF_DIR%\pocket_agent_handoff_%RANDOM%_%RANDOM%.bat"
if exist "%PA_UPDATE_HANDOFF%" del /f /q "%PA_UPDATE_HANDOFF%" >nul 2>&1
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH=%~dp0app\src"
set "PYTHONHOME="
"%~dp0python\python.exe" -m pocket_agent --resume
set "EXIT_CODE=%ERRORLEVEL%"

:POST_RUN
if "%EXIT_CODE%"=="75" goto APPLY_AND_RESUME
if "%EXIT_CODE%"=="76" goto APPLY_ONLY
goto FINISH

:APPLY_AND_RESUME
if not exist "%PA_UPDATE_HANDOFF%" goto HANDOFF_MISSING
call "%PA_UPDATE_HANDOFF%"
set "APPLY_EXIT=%ERRORLEVEL%"
if exist "%PA_UPDATE_HANDOFF%" del /f /q "%PA_UPDATE_HANDOFF%" >nul 2>&1
if not "%APPLY_EXIT%"=="0" goto APPLY_FAILED
goto RUN_RESUME

:APPLY_ONLY
if not exist "%PA_UPDATE_HANDOFF%" goto HANDOFF_MISSING
call "%PA_UPDATE_HANDOFF%"
set "EXIT_CODE=%ERRORLEVEL%"
if exist "%PA_UPDATE_HANDOFF%" del /f /q "%PA_UPDATE_HANDOFF%" >nul 2>&1
goto FINISH

:HANDOFF_MISSING
set "EXIT_CODE=1"
goto FINISH

:APPLY_FAILED
set "EXIT_CODE=%APPLY_EXIT%"
goto FINISH

:FINISH
if exist "%PA_UPDATE_HANDOFF%" del /f /q "%PA_UPDATE_HANDOFF%" >nul 2>&1
REM If launched from Explorer (double-clicked), keep window open on error
echo %cmdcmdline% | find /i "/c" >nul
if not errorlevel 1 (
    if not "%EXIT_CODE%"=="0" (
        echo.
        echo [ERROR] PocketAgent exited with code %EXIT_CODE%.
        pause
    )
)

endlocal & exit /b %EXIT_CODE%
"""


def _ignore(path, names):
    """複製時略過不需要的目錄、__pycache__、測試目錄與除錯符號。"""
    skip = set()
    is_pymupdf_root = Path(path).name.lower() == "pymupdf"
    for n in names:
        if n in PYTHON_EXCLUDES or n in UNWANTED_DIR_NAMES:
            skip.add(n)
        elif is_pymupdf_root and n in PYMUPDF_LAYOUT_ITEMS:
            skip.add(n)
        elif Path(n).suffix.lower() in UNWANTED_EXTENSIONS:
            skip.add(n)
    return skip


def _remove_readonly(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_output(args: list[str]) -> str:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def _detect_repo() -> str:
    gh_repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if gh_repo:
        return gh_repo
    remote = _git_output(["remote", "get-url", "origin"])
    m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", remote)
    return m.group(1) if m else ""


def _detect_version() -> str:
    ref_name = os.getenv("GITHUB_REF_NAME", "").strip()
    if os.getenv("GITHUB_REF_TYPE") == "tag" or ref_name.startswith("v"):
        if ref_name:
            return ref_name
    tag = _git_output(["describe", "--tags", "--exact-match"])
    if tag:
        return tag
    pyproject = ROOT / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            ver = str(data.get("project", {}).get("version") or "").strip()
            if ver:
                return ver
        except Exception:
            pass
    return "0.1.0"


def _collect_build_info() -> dict[str, str]:
    req_path = ROOT / "requirements.txt"
    deps_sha = _sha256_file(req_path) if req_path.is_file() else ""
    commit = (os.getenv("GITHUB_SHA", "").strip() or _git_output(["rev-parse", "HEAD"])).lower()
    return {
        "version": _detect_version(),
        "commit": commit,
        "repo": _detect_repo(),
        "branch": "main",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "deps_sha256": deps_sha,
    }


def main() -> int:
    if not VENV_SITE_PACKAGES.is_dir():
        print(f"[錯誤] 找不到 {VENV_SITE_PACKAGES}")
        print("      請先執行： py -m venv venv")
        print("                venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1

    print(f"基礎 Python： {BASE_PYTHON}")
    if OUT.exists():
        print("清除舊的 dist/PocketAgent ...")
        try:
            shutil.rmtree(OUT, onerror=_remove_readonly)
        except PermissionError as e:
            print(f"\n[錯誤] 無法清除舊的目錄：{e}")
            print("       這通常是因為你目前有開啟（或背景仍在執行）發布版目錄內的 Python。")
            print("       請先關閉所有執行中的 Pocket Agent（或從工作管理員強制結束 pythonw.exe），再重新執行。")
            return 1
    OUT.mkdir(parents=True)

    # 1. Python 本體
    print("[1/6] 複製 Python 執行環境 ...")
    shutil.copytree(BASE_PYTHON, OUT / "python", ignore=_ignore)

    # 2. 套件（合併進 python/Lib/site-packages，執行期就不必設 PYTHONPATH）
    print("[2/6] 複製已安裝套件 ...")
    target_sp = OUT / "python" / "Lib" / "site-packages"
    target_sp.mkdir(parents=True, exist_ok=True)
    for item in VENV_SITE_PACKAGES.iterdir():
        item_lower = item.name.lower()
        if item_lower in {"__pycache__", "pyinstaller"} or item_lower.startswith("pyinstaller"):
            continue
        # 排除開發與測試套件
        if any(
            item_lower == dev or item_lower.startswith(f"{dev}-") or item_lower.startswith(f"{dev}_")
            for dev in DEV_PACKAGES
        ):
            continue
        if item_lower in UNWANTED_DIR_NAMES:
            continue
        if item.is_file() and item.suffix.lower() in UNWANTED_EXTENSIONS:
            continue

        dest = target_sp / item.name
        if item.is_dir():
            shutil.copytree(item, dest, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    # 3. 原始碼
    print("[3/6] 複製程式碼 ...")
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
    print("[4/6] 產生啟動器與建置資訊 ...")
    build_info = _collect_build_info()
    (OUT / "pocket.bat").write_text(POCKET_BAT, encoding="ascii", newline="\r\n")
    (app / "PORTABLE").write_text(
        "可攜式發佈版標記。pocket_agent/system/paths.py 靠這個檔判斷要把設定與 log\n"
        "寫到 ~/.pocket-agent 而非程式目錄。請勿刪除。\n",
        encoding="utf-8",
    )
    (app / "BUILD_INFO.json").write_text(
        json.dumps(build_info, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 5. 產生說明書
    print("[5/6] 產生操作說明書 ...")
    try:
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_manual.py")], check=True)
        manual_src = ROOT / "dist" / "PocketAgent_Manual.md"
        if manual_src.exists():
            shutil.copy2(manual_src, OUT / "PocketAgent_Manual.md")
    except Exception as e:
        print(f"      ! 產生說明書失敗：{e}")

    # 6. 打包
    print("[6/6] 壓縮可攜式發佈包 ...")
    zip_path = ROOT / "dist" / "PocketAgent-portable.zip"
    zip_path.unlink(missing_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in OUT.rglob("*"):
            if f.is_file():
                zf.write(f, Path("PocketAgent") / f.relative_to(OUT))

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print()
    print(f"完成： {zip_path}  （{size_mb:.0f} MB）")
    print("使用者解壓縮後直接執行 PocketAgent\\pocket.bat（或雙擊 pocket.bat）即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
