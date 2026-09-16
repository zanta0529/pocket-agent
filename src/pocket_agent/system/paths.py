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
