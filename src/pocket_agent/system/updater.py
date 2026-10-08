"""GitHub 自動更新：檢查 main 分支是否有新版，下載並套用。

開發模式（bundle_dir() 底下有 .git）直接用 git pull。
發佈給一般使用者的可攜版／打包版（沒有 .git）改抓 GitHub 該分支的
zip 快照解壓覆蓋，不需要使用者自己裝 git。

只覆蓋 bundle_dir()（隨程式發佈的唯讀資源：程式碼、內建 skills/
commands/agents、GUI 圖檔）。user_dir()（~/.pocket-agent，使用者自己
裝的 plugin/skill、對話紀錄、mcp_config.yaml、.env）完全不會被動到
——這些本來就不在 bundle_dir() 底下。採「有就蓋、沒有就跳過」的
合併式複製，絕不刪除本機獨有的檔案（PORTABLE 標記、venv、
產生的圖片/文件等），把「更新出錯」的風險降到最低。
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

from pocket_agent.system.paths import bundle_dir, is_frozen, is_packaged, is_portable

log = logging.getLogger("pocket-agent")

GITHUB_OWNER = "GarfieldHuang"
GITHUB_REPO = "pocket-agent"
GITHUB_BRANCH = "main"

VERSION_FILE = "VERSION"  # 存在 bundle_dir() 底下，記錄目前對應的 commit sha

# 更新時完全不覆蓋的名稱（防禦性保留；這些原本就不會出現在下載的 zip 裡）
_PRESERVE_NAMES = {
    "venv",
    ".venv",
    "PORTABLE",
    "mcp_config.yaml",
    ".env",
    "agent.log",
    "generated_images",
    "generated_files",
    "__pycache__",
    ".git",
}


def _ssl_verify():
    """沿用 agent.auth 已經注入好的 truststore 設定（支援公司 proxy CA）。"""
    from pocket_agent.system.auth import _SSL_VERIFY

    return _SSL_VERIFY


def _is_dev_repo() -> bool:
    return (bundle_dir() / ".git").exists()


def current_version() -> str | None:
    """目前版本識別：dev 模式讀 git HEAD；套件/可攜版讀 VERSION 檔。"""
    if _is_dev_repo():
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=bundle_dir(),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            log.exception("讀取 git HEAD 失敗")
        return None

    vfile = bundle_dir() / VERSION_FILE
    if vfile.exists():
        try:
            return vfile.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
    return None


def check_for_update() -> dict:
    """查詢 GitHub 上最新 commit，回傳版本比對結果。

    {'current': str|None, 'latest': str, 'latest_message': str,
     'update_available': bool}
    current 為 None 代表無法判斷目前版本（例如舊版留下的安裝，
    還沒有 VERSION 檔）——一律視為需要更新，更新後就會建立版本記錄。
    """
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/commits/{GITHUB_BRANCH}"
    resp = httpx.get(
        url,
        timeout=15,
        verify=_ssl_verify(),
        headers={"Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    data = resp.json()

    latest_sha = data["sha"]
    latest_message = (data.get("commit", {}).get("message") or "").splitlines()[0]
    current = current_version()

    return {
        "current": current,
        "latest": latest_sha,
        "latest_message": latest_message,
        "update_available": current != latest_sha,
    }


def apply_update(progress=None) -> dict:
    """套用更新。progress(str) 為選填的進度回呼（背景執行緒呼叫）。"""

    def report(msg: str):
        log.info("[Update] %s", msg)
        if progress is not None:
            try:
                progress(msg)
            except Exception:
                pass

    import click

    info = check_for_update()
    if info.get("update_available"):
        print(f"\n[系統提示] 發現新版本！最新提交：{info.get('latest_message')}")
    print("⚠ 警告：即將從 GitHub 下載並套用更新。這可能會改變系統行為或包含未知的第三方變更。")
    if not click.confirm("確定要繼續更新嗎？"):
        raise RuntimeError("使用者取消更新")

    if _is_dev_repo():
        return _update_via_git(report)
    return _update_via_zip(report)


def _update_via_git(report) -> dict:
    report("執行 git pull…")
    result = subprocess.run(
        ["git", "pull", "--ff-only"],
        cwd=bundle_dir(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git pull 失敗：{result.stderr.strip() or result.stdout.strip()}")

    message = result.stdout.strip() or "已是最新版本"
    report(message)

    if "Already up to date" not in result.stdout and "已經是最新" not in result.stdout:
        _maybe_update_deps(report)

    return {"message": message}


def _update_via_zip(report) -> dict:
    report("下載最新版本…")
    zip_url = f"https://codeload.github.com/{GITHUB_OWNER}/{GITHUB_REPO}/zip/refs/heads/{GITHUB_BRANCH}"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        zip_path = tmp / "update.zip"

        with httpx.stream("GET", zip_url, timeout=60, verify=_ssl_verify(), follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)

        report("解壓縮…")
        extract_dir = tmp / "extracted"
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                if Path(member).is_absolute() or ".." in Path(member).parts:
                    raise ValueError(f"更新包內含不安全路徑：{member}")
            zf.extractall(extract_dir)

        # GitHub 的 zip 快照內只有一個頂層資料夾，例如 pocket-agent-main/
        roots = [d for d in extract_dir.iterdir() if d.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("更新包結構不符預期，已中止（未變更任何檔案）。")
        source_root = roots[0]

        # 這次抓到的內容對應哪個 commit，留給下次比對用
        info = check_for_update()
        new_sha = info["latest"]

        report("套用檔案更新…")
        _merge_copy(source_root, bundle_dir(), report)

        (bundle_dir() / VERSION_FILE).write_text(new_sha, encoding="utf-8")

    _maybe_update_deps(report)
    report("更新完成")
    return {"message": "更新完成"}


def _merge_copy(source: Path, dest: Path, report) -> None:
    """把 source 疊到 dest：只新增/覆蓋，絕不刪除 dest 既有但 source 沒有的東西。"""
    for item in source.iterdir():
        if item.name in _PRESERVE_NAMES:
            continue

        target = dest / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _merge_copy(item, target, report)
        else:
            try:
                shutil.copy2(item, target)
            except Exception as e:
                report(f"略過 {item.name}（{e}）")


def _maybe_update_deps(report) -> None:
    """requirements.txt 有變動時，自動補裝新的相依套件。失敗不中止整個更新。"""
    req = bundle_dir() / "requirements.txt"
    if not req.exists():
        return

    report("更新相依套件…")
    python = sys.executable
    if is_packaged():
        venv_python = bundle_dir().parent / "venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            python = str(venv_python)

    try:
        subprocess.run(
            [python, "-m", "pip", "install", "-q", "-r", str(req)],
            timeout=300,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        report(f"相依套件安裝失敗（可稍後手動執行 pip install -r requirements.txt）：{e}")


def relaunch_and_exit() -> None:
    """啟動新的程式行程、結束目前行程，讓更新後的檔案生效。"""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    if is_frozen():
        subprocess.Popen([sys.executable], creationflags=creationflags)
    elif is_portable():
        _relaunch_portable(creationflags)
    else:
        _relaunch_dev(creationflags)

    sys.exit(0)


def _relaunch_portable(creationflags: int) -> None:
    """直接呼叫 pythonw.exe，不透過 cmd /c start 那層。

    `start.bat` 本身就是設計給人「雙擊」用的：它最後一行也是呼叫
    pythonw.exe（沒有主控台），但若改成程式呼叫 `cmd /c start "" start.bat`，
    `start` 這個指令的語意就是「開一個新視窗執行」——不管有沒有加
    DETACHED_PROCESS 都一樣，因為那個視窗是 cmd 內部另外跟 Windows
    要的，不吃我們這個 subprocess 呼叫的旗標。所以會跳出一個空的
    主控台視窗，這正是使用者回報的症狀。
    直接照 start.bat 的邏輯自己呼叫 pythonw.exe，就完全不會經過
    cmd.exe，也就不會有任何視窗閃現。
    """
    root = bundle_dir().parent  # 可攜版根目錄（python/、app/、start.bat 所在）
    pythonw = root / "python" / "pythonw.exe"
    main_py = bundle_dir() / "main.py"

    if pythonw.exists():
        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        subprocess.Popen(
            [str(pythonw), str(main_py)],
            cwd=str(root),
            env=env,
            creationflags=creationflags,
        )
    else:
        # 找不到內附的 pythonw.exe 才退回雙擊 start.bat 的行為（可能閃一下主控台）
        start_bat = root / "start.bat"
        subprocess.Popen(
            ["cmd", "/c", "start", "", str(start_bat)],
            cwd=str(root),
            creationflags=creationflags,
        )


def _relaunch_dev(creationflags: int) -> None:
    """開發模式：優先用同目錄的 pythonw.exe，避免多跳出一個主控台視窗。"""
    python = sys.executable
    pythonw = Path(python).with_name("pythonw.exe")
    if sys.platform == "win32" and pythonw.exists():
        python = str(pythonw)

    subprocess.Popen(
        [python, str(bundle_dir() / "main.py")],
        cwd=str(bundle_dir()),
        creationflags=creationflags,
    )
