# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包設定。

用 onedir 而非 onefile：onefile 每次啟動都要把整包解壓到 temp（慢），
而且防毒對 onefile 的誤判率明顯高很多，公司環境容易直接被攔。

    pyinstaller --noconfirm PocketAgent.spec

產出 dist/PocketAgent/，整個資料夾壓成 zip 就能發佈。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# 隨程式發佈的唯讀資源。左邊是來源，右邊是 bundle 內的相對路徑，
# 要和 agent/paths.py 的 bundle_dir() 假設一致。
datas = [
    (str(ROOT / "gui" / "assets"),               "gui/assets"),
    (str(ROOT / "agents"),                       "agents"),
    (str(ROOT / "mcp_config.example.yaml"),      "."),
    (str(ROOT / ".env.example"),                 "."),
    (str(ROOT / "browser_mcp.py"),               "."),
]

# customtkinter 的主題 json 與 tkinterdnd2 的 tkdnd 二進位不是 python 模組，
# PyInstaller 掃不到，要明確收集。
datas += collect_data_files("customtkinter")
datas += collect_data_files("tkinterdnd2")

# mcp 用動態 import 選 transport，openai 也有不少延遲載入的子模組。
# mcp.cli 需要 typer（requirements 沒有它，也用不到），掃到會直接讓 build 掛掉。
hiddenimports = (
    collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
    + collect_submodules("openai")
    + ["tkinterdnd2", "customtkinter", "keyring.backends.Windows", "win32timezone"]
)

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # playwright 只在使用者啟用瀏覽器工具時才需要，且它會拖進整包
    # driver 二進位（數百 MB）。排除後瀏覽器 MCP 走外部 python 執行。
    excludes=["playwright", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PocketAgent",
    debug=False,
    strip=False,
    upx=False,          # UPX 壓縮是防毒誤判的大宗，關掉
    console=False,      # GUI 程式，不開黑窗
    icon=str(ROOT / "gui" / "assets" / "ico_agent.ico"),
)

exe_cli = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="PocketAgent-cli",
    debug=False,
    strip=False,
    upx=False,
    console=True,       # CLI 程式，需要黑窗
    icon=str(ROOT / "gui" / "assets" / "ico_agent.ico"),
)

coll = COLLECT(
    exe,
    exe_cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="PocketAgent",
)
