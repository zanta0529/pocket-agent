"""執行環境路徑：區分「唯讀的隨附資源」與「可寫的使用者資料」。

開發模式下 bundle 就是 repo 根目錄；PyInstaller 打包後兩者分家：

    bundle_dir()   → <exe 目錄>/_internal   唯讀，隨程式發佈
    user_dir()     → ~/.pocket-agent            可寫，使用者自己的東西

之所以要分，是因為打包後 bundle 內容會被 exe 一起覆蓋更新，
寫進去的東西下次升級就沒了；而且部分環境的程式目錄不可寫。
凡是執行期會被寫入的檔案（mcp_config.yaml、plugin 裝進來的
skills/commands/agents）一律走 user_dir()。
"""

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否為 PyInstaller 打包後的執行檔。"""
    return getattr(sys, "frozen", False)


def is_portable() -> bool:
    """是否為可攜式發佈版（隨包附一份 Python，用 start.bat 啟動）。

    靠發佈時放進根目錄的 PORTABLE 標記檔判斷——可攜版跑的是
    一般 python.exe，沒有 sys.frozen 可以問。
    """
    return (Path(__file__).resolve().parent.parent.parent.parent / "PORTABLE").exists()


def is_packaged() -> bool:
    """是否為「發給使用者的成品」（PyInstaller 或可攜版皆算）。

    成品的程式目錄視同唯讀：可能放在共用磁碟，而且升級時整包
    替換，寫在裡面的東西會消失。
    """
    return is_frozen() or is_portable()


def bundle_dir() -> Path:
    """唯讀隨附資源的根目錄（GUI 圖檔、內建 skills/commands/agents）。"""
    if is_frozen():
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent.parent.parent


def user_dir() -> Path:
    """使用者資料根目錄，不存在時建立。"""
    d = Path.home() / ".pocket-agent"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_workspace_slug(cwd: Path | str | None = None) -> str:
    r"""依工作目錄絕對路徑產生唯一且安全的路徑 Slug（借鑑 Claude Code 規範）。

    轉換規則：
    1. 解析為絕對路徑。
    2. Windows 磁碟代號（如 C:\ 或 D:\）轉為 C-- 或 D--。
    3. 特殊字元與分隔符號轉為單一連字號 -。
    4. 若轉換後長度超過 60 字元，取前 48 字元並加上該完整路徑之 sha256 前 8 碼作為後綴，
       防止超出作業系統路徑長度上限。
    """
    import hashlib
    import re

    if cwd is None:
        path_str = str(Path.cwd().resolve())
    else:
        raw_str = str(cwd).replace("\\", "/")
        if raw_str.startswith("/") and not re.match(r"^[a-zA-Z]:", raw_str):
            path_str = raw_str
        else:
            path_str = str(Path(cwd).resolve())

    m = re.match(r"^([a-zA-Z]):[\\/](.*)$", path_str)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2)
        sanitized_rest = re.sub(r"[^a-zA-Z0-9_\-]+", "-", rest).strip("-")
        sanitized_rest = re.sub(r"-+", "-", sanitized_rest)
        slug = f"{drive}--{sanitized_rest}" if sanitized_rest else f"{drive}--"
    else:
        sanitized = re.sub(r"[^a-zA-Z0-9_\-]+", "-", path_str).strip("-")
        sanitized = re.sub(r"-+", "-", sanitized)
        slug = f"-{sanitized}" if sanitized else "-"

    if len(slug) > 60:
        h = hashlib.sha256(path_str.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug[:48].rstrip('-')}_{h}"

    return slug or "default"


def workspace_dir(cwd: Path | str | None = None) -> Path:
    """取得當前工作區在全域家目錄中的專屬隔離沙盒目錄。

    結構為：~/.pocket-agent/workspaces/<workspace-slug>/
    """
    slug = get_workspace_slug(cwd)
    d = user_dir() / "workspaces" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def mcp_config_path() -> Path:
    """可寫的 mcp_config.yaml。

    開發模式維持放在 repo 根目錄（已在 .gitignore），
    發佈版改放 user_dir()，避免寫進不可寫／會被升級覆蓋的程式目錄。
    """
    if is_packaged():
        return user_dir() / "mcp_config.yaml"
    return bundle_dir() / "mcp_config.yaml"


def mcp_example_path() -> Path:
    """隨附的 mcp_config.example.yaml 範本（唯讀）。"""
    return bundle_dir() / "mcp_config.example.yaml"


def env_path() -> Path:
    """.env 位置。發佈版放 user_dir()，讓使用者改得到。"""
    if is_packaged():
        return user_dir() / ".env"
    return bundle_dir() / ".env"


def log_dir(cwd: Path | str | None = None) -> Path:
    """系統日誌目錄。

    全域集中於 ~/.pocket-agent/workspaces/<slug>/logs，
    避免在工作目錄隨意產生 .agents/logs 污染版本庫。
    """
    try:
        d = workspace_dir(cwd) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        d = user_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


def scratch_dir(cwd: Path | str | None = None) -> Path:
    """臨時暫存目錄。

    全域集中於 ~/.pocket-agent/workspaces/<slug>/scratch，
    避免在工作目錄隨意產生 .agents/scratch 污染版本庫。
    """
    try:
        d = workspace_dir(cwd) / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        d = user_dir() / "scratch"
        d.mkdir(parents=True, exist_ok=True)
        return d
