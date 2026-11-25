"""執行環境路徑：區分「唯讀的隨附資源」與「可寫的使用者資料」。

開發模式下 bundle 就是 repo 根目錄；PyInstaller 打包後兩者分家：

    bundle_dir()   → <exe 目錄>/_internal   唯讀，隨程式發佈
    user_dir()     → ~/.pocket-agent            可寫，使用者自己的東西

之所以要分，是因為打包後 bundle 內容會被 exe 一起覆蓋更新，
寫進去的東西下次升級就沒了；而且部分環境的程式目錄不可寫。
凡是執行期會被寫入的檔案（mcp_config.yaml、plugin 裝進來的
skills/commands/agents）一律走 user_dir()。
"""

import logging
import re
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from pocket_agent.system.constants import USER_DATA_DIR_NAME

DEFAULT_SCRATCH_SESSION_ID = "default"
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9\-_]+$")
_active_session_id: ContextVar[str | None] = ContextVar("pocket_agent_session_id", default=None)


def validate_session_id(session_id: str) -> str:
    """驗證並正規化 Session ID，防止路徑穿越或非法字元。"""
    clean = str(session_id or "").strip()
    if not clean or not _SESSION_ID_RE.match(clean):
        raise ValueError(f"無效的 Session ID 格式: {session_id}")
    return clean


def get_active_session_id() -> str | None:
    """取得目前執行上下文的作用中 Session ID（未設定時回傳 None）。"""
    return _active_session_id.get()


def set_active_session_id(session_id: str | None) -> Token[str | None]:
    """設定目前執行上下文的作用中 Session ID，回傳可供還原之 Token。"""
    if session_id is None or not str(session_id).strip():
        return _active_session_id.set(None)
    return _active_session_id.set(validate_session_id(str(session_id)))


@contextmanager
def session_scope(session_id: str | None) -> Iterator[None]:
    """於指定區段內綁定作用中 Session ID，離開時自動還原外層上下文。"""
    if session_id is None or not str(session_id).strip():
        yield
        return
    token = set_active_session_id(session_id)
    try:
        yield
    finally:
        _active_session_id.reset(token)


def is_frozen() -> bool:
    """是否為 PyInstaller 打包後的執行檔。"""
    return getattr(sys, "frozen", False)


def is_portable() -> bool:
    """是否為可攜式發佈版（隨包附一份 Python，用 pocket.bat 啟動）。

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


def portable_root() -> Path | None:
    """可攜式發佈版的安裝根目錄（包含 pocket.bat、python/ 與 app/）。"""
    if not is_portable():
        return None
    return bundle_dir().parent


def build_info_path() -> Path:
    """隨包發佈的建置中繼資訊檔路徑（app/BUILD_INFO.json）。"""
    return bundle_dir() / "BUILD_INFO.json"


def user_dir() -> Path:
    """使用者資料根目錄，不存在時建立。"""
    d = Path.home() / USER_DATA_DIR_NAME
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

    全域集中於 ~/.pocket-agent/logs/（依日期切分與輪轉），
    避免在工作目錄隨意產生 .agents/logs 污染版本庫。
    """
    _ = cwd
    d = user_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scratch_root() -> Path:
    """所有會話暫存目錄的根路徑（~/.pocket-agent/scratch），不主動建立目錄。"""
    return user_dir() / "scratch"


def scratch_dir(session_id: str | None = None, *, create: bool = False) -> Path:
    """依會話隔離的臨時暫存目錄（~/.pocket-agent/scratch/<session_id>/）。

    採延遲建立策略，預設僅回傳安全驗證後的路徑，於實際寫入時才建立目錄。
    """
    if session_id is not None:
        sid = validate_session_id(session_id)
    else:
        sid = get_active_session_id() or DEFAULT_SCRATCH_SESSION_ID
    root = scratch_root()
    d = root / sid
    if not d.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
        raise ValueError(f"Session ID 超出安全路徑範圍: {sid}")
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_session_scratch_dir(session_id: str) -> Path | None:
    """解析特定會話之暫存目錄；若為保留預設名稱或非安全子目錄則回傳 None。"""
    sid = validate_session_id(session_id)
    if sid == DEFAULT_SCRATCH_SESSION_ID:
        return None
    root = scratch_root().resolve(strict=False)
    raw_target = scratch_root() / sid
    target = raw_target.resolve(strict=False)
    if target.parent != root or raw_target.is_symlink() or not target.is_dir():
        return None
    return target


def get_session_scratch_size(session_id: str) -> int:
    """計算指定會話暫存目錄下所有檔案的總位元組數。"""
    target = _resolve_session_scratch_dir(session_id)
    if target is None:
        return 0
    total = 0
    for p in target.rglob("*"):
        try:
            if p.is_symlink() or not p.is_file():
                continue
            total += p.stat().st_size
        except OSError:
            continue
    return total


def delete_session_scratch(session_id: str) -> int:
    """刪除指定會話之暫存目錄，回傳釋放的位元組數（失敗時記錄警告並回傳 0）。"""
    target = _resolve_session_scratch_dir(session_id)
    if target is None:
        return 0
    freed = get_session_scratch_size(session_id)
    try:
        shutil.rmtree(target)
        return freed
    except OSError as exc:
        logging.getLogger("pocket-agent").warning("刪除會話暫存目錄失敗 %s: %s", target, exc)
        return 0


def format_display_path(path_str: str, base_dir: Path | None = None) -> str:
    """將檔案路徑轉為相對於工作目錄的簡短路徑，無法相對化時安全保留原路徑。"""
    if not path_str or not isinstance(path_str, str):
        return ""
    raw = Path(path_str.strip())
    base = (base_dir or Path.cwd()).resolve()
    try:
        if raw.is_absolute():
            rel = raw.resolve().relative_to(base)
            return rel.as_posix()
    except ValueError, RuntimeError:
        pass

    try:
        home = Path.home().resolve()
        if raw.is_absolute():
            rel_home = raw.resolve().relative_to(home)
            return f"~/{rel_home.as_posix()}"
    except ValueError, RuntimeError:
        pass

    return raw.as_posix() if hasattr(raw, "as_posix") else str(path_str)
