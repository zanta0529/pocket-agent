# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller onedir 打包設定。

用 onedir 而非 onefile：onefile 每次啟動都要把整包解壓到 temp（慢），
而且防毒對 onefile 的誤判率明顯高很多，公司環境容易直接被攔。

    pyinstaller --noconfirm build_scripts\PocketAgent.spec

產出 dist/PocketAgent/，整個資料夾壓成 zip 就能發佈。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 因為本設定檔放在 build_scripts/ 下，所以專案根目錄為上一層
ROOT = Path(SPECPATH).parent

# 隨程式發佈的唯讀資源。
# 要和 pocket_agent/system/paths.py 的 bundle_dir() 假設一致。
datas = [
    (str(ROOT / "subagents"),                       "subagents"),
    (str(ROOT / "mcp_config.example.yaml"),      "."),
    (str(ROOT / ".env.example"),                 "."),
    (str(ROOT / "mcp_servers" / "*.py"), "mcp_servers"),
]

# mcp 用動態 import 選 transport，openai 也有不少延遲載入的子模組。
# mcp.cli 需要 typer（requirements 沒有它，也用不到），掃到會直接讓 build 掛掉。
# mysql_mcp / mssql_mcp 透過 runpy 與 importlib 動態載入 pymysql / pymssql（含 Cython 內部依賴 decimal、uuid）。
hiddenimports = (
    collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
    + collect_submodules("openai")
    + collect_submodules("pymysql")
    + collect_submodules("pymssql")
    + ["keyring.backends.Windows", "win32timezone", "decimal", "uuid"]
)

a = Analysis(
    [str(ROOT / "src" / "pocket_agent" / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # playwright 只在使用者啟用瀏覽器工具時才需要，且它會拖進整包
    # driver 二進位（數百 MB）。排除後瀏覽器 MCP 走外部 python 執行。
    excludes=["playwright", "pytest", "IPython", "customtkinter", "tkinterdnd2"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe_cli = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PocketAgent-cli",
    debug=False,
    strip=False,
    upx=False,          # UPX 壓縮是防毒誤判的大宗，關掉
    console=True,       # CLI 程式，需要黑窗
)

coll = COLLECT(
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PocketAgent",
)
