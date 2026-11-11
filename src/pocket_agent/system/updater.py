"""Modern Portable 版線上自動更新管理模組（追蹤 GitHub 分支最新 Commit SHA）。

僅於 `is_portable()` 為 True 的可攜式打包環境啟用；開發人員使用 `run.bat`
執行原始碼時完全停用，避免覆寫 Git 工作目錄。當推送程式碼至 GitHub 主分支後，
可攜版會自動比對最新 Commit SHA 並下載對應快照更新 `app/`，無須手動建立 Release。
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import certifi
import httpx

from pocket_agent.core.cancellation import run_sync
from pocket_agent.system.config import config
from pocket_agent.system.constants import DEFAULT_MAX_REDIRECTS, DEFAULT_NETWORK_TIMEOUT_SEC
from pocket_agent.system.paths import build_info_path, bundle_dir, is_portable, portable_root

log = logging.getLogger("pocket-agent")

VERSION_FILE = "BUILD_INFO.json"
_SKIP_NAMES = {"__pycache__", ".git", "venv", ".venv"}

APP_SYNC_ITEMS = (
    "src",
    "mcp_servers",
    "subagents",
    "mcp_config.example.yaml",
    ".env.example",
    "requirements.txt",
)

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_SAFE_ASCII_TOKEN = re.compile(r"[^0-9A-Za-z._-]")
_DOWNLOAD_READ_TIMEOUT_SEC = 120.0

IN_PLACE_LAUNCHER_ENV = "PA_IN_PLACE_LAUNCHER"
IN_PLACE_HANDOFF_ENV = "PA_UPDATE_HANDOFF"
EXIT_CODE_UPDATE_RESTART = 75
EXIT_CODE_UPDATE_APPLY_ONLY = 76

_PORTABLE_MARKER_TEXT = (
    "可攜式發佈版標記。pocket_agent/system/paths.py 靠這個檔判斷要把設定與 log\n"
    "寫到 ~/.pocket-agent 而非程式目錄。請勿刪除。\n"
)

PORTABLE_LAUNCHER_BAT = r"""@echo off
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

_APPLY_BAT_BODY = r"""if not defined PA_STAGING set "PA_STAGING=%PA_ROOT%\.update_staging"
if not defined PA_PAYLOAD set "PA_PAYLOAD=%PA_STAGING%\payload"
if not defined PA_CWD set "PA_CWD=%CD%"
if not defined PA_SHOW_PROGRESS set "PA_SHOW_PROGRESS=%PA_RESTART%"
if not defined PA_SPAWN_RESUME set "PA_SPAWN_RESUME=%PA_RESTART%"
if not defined PA_PAUSE_ON_ABORT set "PA_PAUSE_ON_ABORT=%PA_RESTART%"

if "%PA_SHOW_PROGRESS%"=="1" echo [Pocket Agent] Applying update (%PA_VERSION%) ...

set /a RETRIES=0
:WAIT_LOOP
tasklist /FI "PID eq %PA_PID%" 2>nul | find "%PA_PID%" >nul
if errorlevel 1 goto SWAP_LOOP_INIT
set /a RETRIES+=1
if %RETRIES% GEQ 15 goto ABORT
ping -n 2 127.0.0.1 >nul
goto WAIT_LOOP

:SWAP_LOOP_INIT
set /a SWAP_RETRIES=0
:DO_SWAP
if exist "%PA_ROOT%\app.bak" rmdir /s /q "%PA_ROOT%\app.bak" >nul 2>&1
move "%PA_ROOT%\app" "%PA_ROOT%\app.bak" >nul 2>&1
if not errorlevel 1 goto MOVE_NEW_APP
set /a SWAP_RETRIES+=1
if %SWAP_RETRIES% GEQ 5 goto ABORT
ping -n 2 127.0.0.1 >nul
goto DO_SWAP

:MOVE_NEW_APP
move "%PA_PAYLOAD%\app" "%PA_ROOT%\app" >nul 2>&1
if errorlevel 1 goto ROLLBACK_SWAP

if not "%PA_INSTALL_DEPS%"=="1" goto MAYBE_UPGRADE_LAUNCHER
if "%PA_SHOW_PROGRESS%"=="1" echo [Pocket Agent] Updating Python packages ...
set "PYTHONNOUSERSITE=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PYTHONPATH="
set "PYTHONHOME="
"%PA_ROOT%\python\python.exe" -m pip install --no-warn-script-location -q -r "%PA_ROOT%\app\requirements.txt"
if errorlevel 1 goto ROLLBACK_DEPS

:MAYBE_UPGRADE_LAUNCHER
if not "%PA_UPGRADE_LAUNCHER%"=="1" goto CLEANUP
if not exist "%PA_PAYLOAD%\pocket.bat" goto CLEANUP
if not exist "%PA_ROOT%\pocket.bat" goto CLEANUP
find /i "PA_IN_PLACE_LAUNCHER" "%PA_ROOT%\pocket.bat" >nul 2>&1
if not errorlevel 1 goto CLEANUP
ping -n 2 127.0.0.1 >nul
copy /y "%PA_PAYLOAD%\pocket.bat" "%PA_ROOT%\pocket.bat" >nul 2>&1

:CLEANUP
if exist "%PA_ROOT%\app.bak" rmdir /s /q "%PA_ROOT%\app.bak" >nul 2>&1
if exist "%PA_STAGING%" rmdir /s /q "%PA_STAGING%" >nul 2>&1

if not "%PA_SPAWN_RESUME%"=="1" goto DONE
cd /d "%PA_CWD%"
set "PYTHONNOUSERSITE=1"
set "PYTHONPATH=%PA_ROOT%\app\src"
set "PYTHONHOME="
"%PA_ROOT%\python\python.exe" -m pocket_agent --resume
goto EXIT_OK

:DONE
if "%PA_SHOW_PROGRESS%"=="1" if "%PA_ANNOUNCE_COMPLETE%"=="1" echo [Pocket Agent] Update complete (%PA_VERSION%).

:EXIT_OK
endlocal
exit /b 0

:ROLLBACK_DEPS
rmdir /s /q "%PA_ROOT%\app" >nul 2>&1
:ROLLBACK_SWAP
move "%PA_ROOT%\app.bak" "%PA_ROOT%\app" >nul 2>&1
goto ABORT

:ABORT
if "%PA_SHOW_PROGRESS%"=="1" echo [ERROR] Update failed or files are locked by another running instance.
if "%PA_PAUSE_ON_ABORT%"=="1" pause
endlocal
exit /b 1
"""

_APPLY_BAT = "@echo off\nsetlocal\n" + _APPLY_BAT_BODY


@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str = ""
    repo: str = ""
    branch: str = "main"
    python_version: str = ""
    deps_sha256: str = ""


@dataclass(frozen=True)
class CommitUpdateInfo:
    sha: str
    short_sha: str
    message: str
    zip_url: str


@dataclass(frozen=True)
class StagedUpdate:
    commit: str
    short_sha: str
    message: str
    install_deps: bool
    payload_dir: Path


def _ssl_verify() -> bool | str:
    try:
        import truststore

        truststore.inject_into_ssl()
        return True
    except ImportError:
        return certifi.where()


def _is_truthy(val: object, default: bool = True) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off", "")


def is_same_commit(local_sha: str, remote_sha: str) -> bool:
    """比對本機與遠端 Commit SHA 是否一致（相容完整 40 碼與至少 7 碼之短雜湊）。"""
    local_norm = local_sha.strip().lower()
    remote_norm = remote_sha.strip().lower()
    if not local_norm or not remote_norm:
        return False
    if len(local_norm) == len(remote_norm):
        return local_norm == remote_norm
    if 7 <= len(local_norm) < len(remote_norm):
        return remote_norm.startswith(local_norm)
    return False


def _python_xy() -> str:
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def get_current_build_info() -> BuildInfo:
    """讀取隨包發佈的 BUILD_INFO.json；若不存在則回傳執行期推導之保底資訊。"""
    info_file = build_info_path()
    py_ver = _python_xy()
    req_file = bundle_dir() / "requirements.txt"
    fallback_deps = hashlib.sha256(req_file.read_bytes()).hexdigest() if req_file.is_file() else ""

    if info_file.is_file():
        try:
            raw = json.loads(info_file.read_text(encoding="utf-8"))
            return BuildInfo(
                version=str(raw.get("version") or "0.1.0"),
                commit=str(raw.get("commit") or "").strip().lower(),
                repo=str(raw.get("repo") or "").strip(),
                branch=str(raw.get("branch") or "main").strip(),
                python_version=str(raw.get("python_version") or py_ver),
                deps_sha256=str(raw.get("deps_sha256") or fallback_deps),
            )
        except Exception as exc:
            log.debug("讀取 BUILD_INFO.json 失敗：%s", exc)
    return BuildInfo(version="0.1.0", python_version=py_ver, deps_sha256=fallback_deps)


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """解壓縮並逐一驗證目標路徑，防範 Zip-Slip 目錄穿越攻擊。"""
    dest_resolved = dest_dir.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = (dest_resolved / member.filename).resolve()
            try:
                target.relative_to(dest_resolved)
            except ValueError as exc:
                raise ValueError(f"偵測到不安全的壓縮檔路徑：{member.filename}") from exc
        zf.extractall(dest_resolved)


def _merge_copy(
    source: Path,
    dest: Path,
    report: Callable[[str], None] | None = None,
) -> list[tuple[str, str]]:
    """遞迴複製目錄內容並收集失敗項目，供上層阻斷不完整更新。"""
    failed: list[tuple[str, str]] = []
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in _SKIP_NAMES or item.suffix.lower() in (".pyc", ".pyo"):
            continue
        target = dest / item.name
        if item.is_dir():
            failed.extend(_merge_copy(item, target, report))
        else:
            try:
                shutil.copy2(item, target)
            except Exception as exc:
                if report is not None:
                    report(f"複製失敗 {item.name}：{exc}")
                failed.append((item.name, str(exc)))
    return failed


def _read_source_version(source_root: Path, fallback: str) -> str:
    pyproject = source_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            ver = str(data.get("project", {}).get("version") or "").strip()
            if ver:
                return ver
        except Exception:
            pass
    return fallback


class UpdateManager:
    """負責檢查 GitHub 最新 Commit、下載組裝 `app/` 預備包，以及產生外部交接腳本。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._in_place_handoff_path: Path | None = None
        self._launcher_cwd: Path | None = None

    def consume_launcher_env(self) -> None:
        """擷取並自 os.environ 移除啟動器交接變數，避免洩漏給 MCP 或 Shell 子行程。"""
        flag = os.environ.pop(IN_PLACE_LAUNCHER_ENV, "").strip()
        handoff = os.environ.pop(IN_PLACE_HANDOFF_ENV, "").strip()
        if flag == "1" and handoff:
            self._in_place_handoff_path = Path(handoff)
            self._launcher_cwd = Path.cwd().resolve()
        else:
            self._in_place_handoff_path = None
            self._launcher_cwd = None

    def _resolve_handoff_path(self, root: Path, staged: StagedUpdate, cwd: Path | None) -> Path | None:
        raw_path = self._in_place_handoff_path
        if raw_path is None and os.environ.get(IN_PLACE_LAUNCHER_ENV, "").strip() == "1":
            env_handoff = os.environ.get(IN_PLACE_HANDOFF_ENV, "").strip()
            if env_handoff:
                raw_path = Path(env_handoff)
        if raw_path is None or raw_path.suffix.lower() != ".bat":
            return None

        expected_payload = (root / ".update_staging" / "payload").resolve()
        if staged.payload_dir.resolve() != expected_payload:
            return None

        expected_cwd = self._launcher_cwd or Path.cwd().resolve()
        if cwd is not None and cwd.resolve() != expected_cwd:
            return None

        try:
            parent_resolved = raw_path.parent.resolve(strict=True)
        except OSError:
            return None
        if not parent_resolved.is_dir():
            return None

        allowed_roots = (Path(tempfile.gettempdir()).resolve(), root.resolve())
        if not any(parent_resolved.is_relative_to(allowed) for allowed in allowed_roots):
            return None
        return parent_resolved / raw_path.name

    def is_supported(self) -> bool:
        return sys.platform == "win32" and is_portable()

    def is_auto_update_enabled(self) -> bool:
        return self.is_supported() and _is_truthy(config.get("system.auto_update", True), default=True)

    def _resolve_repo(self, current: BuildInfo) -> str:
        configured = str(config.get("system.update_repo", "") or "").strip()
        repo = configured or current.repo.strip()
        if not repo:
            raise ValueError("尚未設定更新儲存庫（請設定 system.update_repo）")
        if not _REPO_PATTERN.match(repo):
            raise ValueError(f"更新儲存庫格式無效：{repo}")
        return repo

    def _resolve_branch(self, current: BuildInfo) -> str:
        configured = str(config.get("system.update_branch", "") or "").strip()
        branch = configured or current.branch.strip() or "main"
        if not _BRANCH_PATTERN.match(branch) or ".." in branch:
            raise ValueError(f"更新分支名稱格式無效：{branch}")
        return branch

    def get_staged_update(self) -> StagedUpdate | None:
        root = portable_root()
        if root is None:
            return None
        ready_file = root / ".update_staging" / "READY.json"
        if not ready_file.is_file():
            return None
        try:
            data = json.loads(ready_file.read_text(encoding="utf-8"))
            commit = str(data["commit"]).strip().lower()
            payload_dir = Path(str(data["payload_dir"]))
            if not _COMMIT_SHA_PATTERN.match(commit) or not (payload_dir / "app").is_dir():
                return None
            return StagedUpdate(
                commit=commit,
                short_sha=str(data.get("short_sha") or commit[:7]),
                message=str(data.get("message") or ""),
                install_deps=bool(data.get("install_deps", False)),
                payload_dir=payload_dir,
            )
        except Exception:
            return None

    async def check_for_update(self) -> CommitUpdateInfo | None:
        if not self.is_supported():
            return None
        current = get_current_build_info()
        repo = self._resolve_repo(current)
        branch = self._resolve_branch(current)
        api_url = f"https://api.github.com/repos/{repo}/commits/{branch}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "PocketAgent-Updater"}

        timeout = httpx.Timeout(DEFAULT_NETWORK_TIMEOUT_SEC)
        async with httpx.AsyncClient(
            timeout=timeout,
            max_redirects=DEFAULT_MAX_REDIRECTS,
            follow_redirects=True,
            verify=_ssl_verify(),
        ) as client:
            resp = await client.get(api_url, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

        remote_sha = str(data.get("sha") or "").strip().lower()
        if not _COMMIT_SHA_PATTERN.match(remote_sha):
            raise ValueError("GitHub API 回傳的 Commit SHA 格式無效")
        if is_same_commit(current.commit, remote_sha):
            return None

        raw_msg = str((data.get("commit") or {}).get("message") or "")
        first_line = raw_msg.splitlines()[0].strip() if raw_msg else ""
        zip_url = f"https://codeload.github.com/{repo}/zip/{remote_sha}"
        return CommitUpdateInfo(
            sha=remote_sha,
            short_sha=remote_sha[:7],
            message=first_line,
            zip_url=zip_url,
        )

    def _assemble_payload_sync(
        self,
        zip_path: Path,
        staging_dir: Path,
        current: BuildInfo,
        remote: CommitUpdateInfo,
        repo: str,
        branch: str,
    ) -> tuple[Path, bool]:
        raw_dir = staging_dir / "raw"
        _safe_extract_zip(zip_path, raw_dir)
        zip_path.unlink(missing_ok=True)

        roots = [d for d in raw_dir.iterdir() if d.is_dir()]
        if len(roots) != 1:
            raise ValueError("更新快照壓縮包結構不符預期（頂層目錄數量異常）")
        source_root = roots[0]
        if not (source_root / "src" / "pocket_agent").is_dir():
            raise ValueError("更新快照壓縮包缺少 src/pocket_agent 核心程式碼目錄")

        payload_dir = staging_dir / "payload"
        payload_app = payload_dir / "app"
        payload_app.mkdir(parents=True, exist_ok=True)

        failed_files: list[tuple[str, str]] = []
        for item_name in APP_SYNC_ITEMS:
            src_item = source_root / item_name
            if not src_item.exists():
                continue
            dest_item = payload_app / item_name
            if src_item.is_dir():
                failed_files.extend(_merge_copy(src_item, dest_item))
            else:
                try:
                    shutil.copy2(src_item, dest_item)
                except Exception as exc:
                    failed_files.append((src_item.name, str(exc)))

        if failed_files:
            failed_summary = ", ".join(f"{name} ({err})" for name, err in failed_files[:5])
            raise RuntimeError(f"檔案更新未完整套用，為避免版本混合損壞已中止更新。失敗檔案：{failed_summary}")

        existing_portable = bundle_dir() / "PORTABLE"
        if existing_portable.is_file():
            shutil.copy2(existing_portable, payload_app / "PORTABLE")
        else:
            (payload_app / "PORTABLE").write_text(_PORTABLE_MARKER_TEXT, encoding="utf-8")

        req_file = payload_app / "requirements.txt"
        new_deps_sha = hashlib.sha256(req_file.read_bytes()).hexdigest() if req_file.is_file() else ""
        install_deps = bool(new_deps_sha and new_deps_sha != current.deps_sha256)

        new_build_info = {
            "version": _read_source_version(source_root, current.version),
            "commit": remote.sha,
            "repo": repo,
            "branch": branch,
            "python_version": current.python_version or _python_xy(),
            "deps_sha256": new_deps_sha,
        }
        (payload_app / VERSION_FILE).write_text(
            json.dumps(new_build_info, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (payload_dir / "pocket.bat").write_text(PORTABLE_LAUNCHER_BAT, encoding="ascii", newline="\r\n")
        shutil.rmtree(raw_dir, ignore_errors=True)
        return payload_dir.resolve(), install_deps

    async def download_and_stage(self, on_status: Callable[[str], None] | None = None) -> StagedUpdate | None:
        """檢查、下載並組裝最新 Commit 程式碼至預備區（若已是最新版則回傳 None）。"""
        if not self.is_supported():
            return None

        async with self._lock:
            current = get_current_build_info()
            existing = self.get_staged_update()
            remote = await self.check_for_update()
            if remote is None:
                if existing and not is_same_commit(current.commit, existing.commit):
                    return existing
                return None
            if existing and existing.commit == remote.sha:
                return existing

            root = portable_root()
            if root is None:
                return None

            repo = self._resolve_repo(current)
            branch = self._resolve_branch(current)

            staging_dir = root / ".update_staging"
            staging_dir.mkdir(parents=True, exist_ok=True)
            probe = staging_dir / ".write_probe"
            probe.write_text("ok", encoding="ascii")
            probe.unlink(missing_ok=True)

            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)

            if on_status:
                summary = f" - {remote.message}" if remote.message else ""
                on_status(f"正在下載最新更新 ({remote.short_sha}{summary})…")

            part_path = staging_dir / "source.zip.part"
            zip_path = staging_dir / "source.zip"
            timeout = httpx.Timeout(DEFAULT_NETWORK_TIMEOUT_SEC, read=_DOWNLOAD_READ_TIMEOUT_SEC)

            async with httpx.AsyncClient(
                timeout=timeout,
                max_redirects=DEFAULT_MAX_REDIRECTS,
                follow_redirects=True,
                verify=_ssl_verify(),
            ) as client:
                async with client.stream("GET", remote.zip_url, headers={"User-Agent": "PocketAgent-Updater"}) as resp:
                    resp.raise_for_status()
                    with part_path.open("wb") as fh:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)

            part_path.replace(zip_path)
            if on_status:
                on_status(f"正在解壓並組裝 ({remote.short_sha}) 更新內容…")

            payload_dir, install_deps = await run_sync(
                self._assemble_payload_sync,
                zip_path,
                staging_dir,
                current,
                remote,
                repo,
                branch,
            )

            ready_data = {
                "commit": remote.sha,
                "short_sha": remote.short_sha,
                "message": remote.message,
                "install_deps": install_deps,
                "payload_dir": str(payload_dir),
            }
            (staging_dir / "READY.json").write_text(json.dumps(ready_data, ensure_ascii=False), encoding="utf-8")
            return StagedUpdate(
                commit=remote.sha,
                short_sha=remote.short_sha,
                message=remote.message,
                install_deps=install_deps,
                payload_dir=payload_dir,
            )

    def apply_staged_update_for_cli(
        self,
        restart: bool = False,
        cwd: Path | None = None,
        *,
        foreground: bool = False,
    ) -> tuple[bool, int]:
        """套用已預備之更新；若由新版啟動器呼叫則優先交由原視窗接手並回傳專用結束碼。"""
        if not self.is_supported():
            return False, 0
        staged = self.get_staged_update()
        root = portable_root()
        if staged is None or root is None:
            return False, 0

        safe_version = _SAFE_ASCII_TOKEN.sub("", staged.short_sha)[:40] or "unknown"
        if restart or foreground:
            handoff_target = self._resolve_handoff_path(root, staged, cwd)
            if handoff_target is not None:
                announce = "0" if restart else "1"
                header = (
                    "@echo off\n"
                    "setlocal\n"
                    f'set "PA_PID={os.getpid()}"\n'
                    f'set "PA_INSTALL_DEPS={"1" if staged.install_deps else "0"}"\n'
                    f'set "PA_VERSION={safe_version}"\n'
                    'set "PA_RESTART=0"\n'
                    'set "PA_SHOW_PROGRESS=1"\n'
                    'set "PA_SPAWN_RESUME=0"\n'
                    'set "PA_PAUSE_ON_ABORT=0"\n'
                    'set "PA_UPGRADE_LAUNCHER=0"\n'
                    f'set "PA_ANNOUNCE_COMPLETE={announce}"\n'
                )
                handoff_target.write_text(header + _APPLY_BAT_BODY, encoding="ascii", newline="\r\n")
                exit_code = EXIT_CODE_UPDATE_RESTART if restart else EXIT_CODE_UPDATE_APPLY_ONLY
                return True, exit_code

        bat_path = Path(tempfile.gettempdir()) / "pocket_agent_apply_update.bat"
        bat_path.write_text(_APPLY_BAT, encoding="ascii", newline="\r\n")

        env = dict(os.environ)
        env.pop(IN_PLACE_LAUNCHER_ENV, None)
        env.pop(IN_PLACE_HANDOFF_ENV, None)
        env["PA_ROOT"] = str(root.resolve())
        env["PA_PAYLOAD"] = str(staged.payload_dir.resolve())
        env["PA_STAGING"] = str((root / ".update_staging").resolve())
        env["PA_CWD"] = str((cwd or Path.cwd()).resolve())
        env["PA_PID"] = str(os.getpid())
        env["PA_INSTALL_DEPS"] = "1" if staged.install_deps else "0"
        env["PA_VERSION"] = safe_version
        env["PA_RESTART"] = "1" if restart else "0"
        env["PA_UPGRADE_LAUNCHER"] = "1"

        if restart:
            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        else:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        subprocess.Popen(["cmd.exe", "/c", str(bat_path)], env=env, creationflags=flags, close_fds=True)
        return True, 0

    def apply_staged_update(self, restart: bool = False, cwd: Path | None = None) -> bool:
        """啟動外部批次檔或原視窗交接腳本，於當前行程結束後原子套用已預備之更新。"""
        applied, _ = self.apply_staged_update_for_cli(restart=restart, cwd=cwd, foreground=restart)
        return applied


update_manager = UpdateManager()
