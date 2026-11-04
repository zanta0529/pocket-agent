import asyncio
import hashlib
import io
import json
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from pocket_agent.frontend.cli.commands.core import CommandContext
from pocket_agent.frontend.cli.commands.misc import UpdateCommand
from pocket_agent.frontend.cli.input import SlashCommandCompleter
from pocket_agent.frontend.cli.render import CliRenderer
from pocket_agent.frontend.cli.state import CliState
from pocket_agent.main import main as cli_main
from pocket_agent.system import updater
from pocket_agent.system.updater import UpdateManager, is_same_commit

_OLD_SHA = "1" * 40
_NEW_SHA = "a1b2c3d" + ("2" * 33)


def _make_zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in files.items():
            zf.writestr(arcname, content)
    return buf.getvalue()


def _setup_fake_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit: str = _OLD_SHA,
    deps_content: bytes = b"httpx==0.28.1\n",
) -> Path:
    root = tmp_path / "PocketAgent"
    app_dir = root / "app"
    py_dir = root / "python"
    app_dir.mkdir(parents=True)
    py_dir.mkdir(parents=True)
    (app_dir / "PORTABLE").write_text("portable-marker", encoding="utf-8")
    (app_dir / "requirements.txt").write_bytes(deps_content)
    deps_sha = hashlib.sha256(deps_content).hexdigest()

    build_info = {
        "version": "0.1.0",
        "commit": commit,
        "repo": "example/pocket-agent",
        "branch": "main",
        "python_version": "3.14",
        "deps_sha256": deps_sha,
    }
    info_file = app_dir / "BUILD_INFO.json"
    info_file.write_text(json.dumps(build_info), encoding="utf-8")

    monkeypatch.setattr(updater, "sys", SimpleNamespace(platform="win32", version_info=(3, 14, 0)))
    monkeypatch.setattr(updater, "is_portable", lambda: True)
    monkeypatch.setattr(updater, "portable_root", lambda: root)
    monkeypatch.setattr(updater, "bundle_dir", lambda: app_dir)
    monkeypatch.setattr(updater, "build_info_path", lambda: info_file)
    monkeypatch.setattr(updater.config, "get", lambda key, default=None: default)
    return root


@pytest.mark.parametrize(
    ("local_sha", "remote_sha", "expected"),
    [
        (_OLD_SHA, _OLD_SHA, True),
        (_NEW_SHA.upper(), _NEW_SHA.lower(), True),
        ("a1b2c3d", _NEW_SHA, True),
        (_OLD_SHA, _NEW_SHA, False),
        ("", _NEW_SHA, False),
        ("123", _NEW_SHA, False),
    ],
)
def test_is_same_commit(local_sha: str, remote_sha: str, expected: bool) -> None:
    assert is_same_commit(local_sha, remote_sha) is expected


def test_dev_mode_disables_all_update_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pocket_agent.system.paths.is_portable", lambda: False)
    monkeypatch.setattr(updater, "is_portable", lambda: False)

    mgr = UpdateManager()
    assert mgr.is_supported() is False
    assert asyncio.run(mgr.download_and_stage()) is None

    # 1. 補全選單在開發模式下不列出 /update
    state = CliState("openai", "gpt-6-astra")
    completer = SlashCommandCompleter(state)
    completions = list(completer.get_completions(Document("/up"), CompleteEvent()))
    assert all(c.text != "update" for c in completions)

    # 2. /update 指令在開發模式下僅提示且不退出
    out = StringIO()
    renderer = CliRenderer(Console(file=out, force_terminal=False))
    ctx = CommandContext(agent=SimpleNamespace(), state=state, reader=Mock(), renderer=renderer, tools=[])
    res = asyncio.run(UpdateCommand().execute(ctx, ""))
    assert res.action == "handled"
    assert state.pending_update_restart is False
    assert "git pull" in out.getvalue()

    # 3. CLI `pocket-agent update` 在開發模式下安全提示並返回 0
    runner = CliRunner()
    cli_res = runner.invoke(cli_main, ["update"])
    assert cli_res.exit_code == 0
    assert "git pull" in cli_res.output


def test_up_to_date_commit_skips_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_fake_portable(tmp_path, monkeypatch, commit=_NEW_SHA)
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        if url.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": _NEW_SHA, "commit": {"message": "same commit"}})
        raise AssertionError(f"Unexpected download URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        updater.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mgr = UpdateManager()
    assert asyncio.run(mgr.download_and_stage()) is None
    assert len(requested_urls) == 1


def test_commit_update_stages_app_items_and_skips_pip_when_deps_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_deps = b"httpx==0.28.1\n"
    _setup_fake_portable(tmp_path, monkeypatch, commit=_OLD_SHA, deps_content=same_deps)

    prefix = f"pocket-agent-{_NEW_SHA}"
    repo_zip = _make_zip_bytes(
        {
            f"{prefix}/src/pocket_agent/__init__.py": b"__version__ = '0.2.0'\n",
            f"{prefix}/mcp_servers/demo.py": b"# mcp\n",
            f"{prefix}/requirements.txt": same_deps,
            f"{prefix}/pyproject.toml": b'[project]\nname = "pocket-agent"\nversion = "0.2.0"\n',
            f"{prefix}/tests/test_dummy.py": b"# dev-only test\n",
            f"{prefix}/.github/workflows/ci.yml": b"name: CI\n",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": _NEW_SHA, "commit": {"message": "feat: auto update\ndetails"}})
        if url == f"https://codeload.github.com/example/pocket-agent/zip/{_NEW_SHA}":
            return httpx.Response(200, content=repo_zip)
        raise AssertionError(f"Unexpected URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        updater.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mgr = UpdateManager()
    staged = asyncio.run(mgr.download_and_stage())
    assert staged is not None
    assert staged.commit == _NEW_SHA
    assert staged.short_sha == "a1b2c3d"
    assert staged.message == "feat: auto update"
    assert staged.install_deps is False

    staged_app = staged.payload_dir / "app"
    assert (staged_app / "PORTABLE").read_text(encoding="utf-8") == "portable-marker"
    assert (staged_app / "src" / "pocket_agent" / "__init__.py").is_file()
    assert not (staged_app / "tests").exists()
    assert not (staged_app / ".github").exists()

    saved_info = json.loads((staged_app / "BUILD_INFO.json").read_text(encoding="utf-8"))
    assert saved_info["commit"] == _NEW_SHA
    assert saved_info["version"] == "0.2.0"


def test_commit_update_marks_install_deps_when_requirements_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_fake_portable(tmp_path, monkeypatch, commit=_OLD_SHA, deps_content=b"httpx==0.28.1\n")

    prefix = f"pocket-agent-{_NEW_SHA}"
    repo_zip = _make_zip_bytes(
        {
            f"{prefix}/src/pocket_agent/__init__.py": b"",
            f"{prefix}/requirements.txt": b"httpx==0.28.1\nrich==14.0.0\n",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": _NEW_SHA, "commit": {"message": "chore: bump rich"}})
        if url == f"https://codeload.github.com/example/pocket-agent/zip/{_NEW_SHA}":
            return httpx.Response(200, content=repo_zip)
        raise AssertionError(f"Unexpected URL: {url}")

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        updater.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler)),
    )

    mgr = UpdateManager()
    staged = asyncio.run(mgr.download_and_stage())
    assert staged is not None
    assert staged.install_deps is True


def test_zip_slip_traversal_rejected(tmp_path: Path) -> None:
    evil_bytes = _make_zip_bytes({"../../evil.py": b"print('pwned')"})
    zip_file = tmp_path / "evil.zip"
    zip_file.write_bytes(evil_bytes)
    with pytest.raises(ValueError, match="不安全的壓縮檔路徑"):
        updater._safe_extract_zip(zip_file, tmp_path / "extract")


def test_manual_update_command_and_apply_script_unicode_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unicode_dir = tmp_path / "繁體中文目錄 測試"
    root = _setup_fake_portable(unicode_dir, monkeypatch, commit=_OLD_SHA)

    payload = root / ".update_staging" / "payload"
    (payload / "app").mkdir(parents=True)
    ready_file = root / ".update_staging" / "READY.json"
    ready_file.write_text(
        json.dumps(
            {
                "commit": _NEW_SHA,
                "short_sha": "a1b2c3d",
                "message": "feat: update",
                "install_deps": True,
                "payload_dir": str(payload),
            }
        ),
        encoding="utf-8",
    )

    # 1. 驗證 /update 無須互動確認即可設定 pending_update_restart 並請求退出重啟
    monkeypatch.setattr(
        updater.update_manager,
        "download_and_stage",
        lambda on_status=None: asyncio.sleep(0, result=updater.update_manager.get_staged_update()),
    )
    monkeypatch.setattr(updater.update_manager, "is_supported", lambda: True)

    state = CliState("openai", "gpt-6-astra")
    out = StringIO()
    renderer = CliRenderer(Console(file=out, force_terminal=False))
    ctx = CommandContext(agent=SimpleNamespace(), state=state, reader=Mock(), renderer=renderer, tools=[])
    res = asyncio.run(UpdateCommand().execute(ctx, ""))
    assert res.action == "quit"
    assert state.pending_update_restart is True
    assert state.staged_update_version == "a1b2c3d"

    # 2. 驗證 apply_staged_update 以純 ASCII 批次檔搭配環境變數傳遞中文路徑與套件更新旗標
    popen_calls: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(updater, "tempfile", SimpleNamespace(gettempdir=lambda: str(tmp_path)))

    def fake_popen(cmd: list[str], env: dict[str, str], **kwargs: object) -> Mock:
        popen_calls.append((cmd, env))
        return Mock()

    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)

    project_cwd = unicode_dir / "我的工作區"
    project_cwd.mkdir(parents=True)
    ok = UpdateManager().apply_staged_update(restart=True, cwd=project_cwd)
    assert ok is True
    assert len(popen_calls) == 1
    cmd, passed_env = popen_calls[0]
    bat_file = Path(cmd[2])
    assert bat_file.read_bytes().isascii()
    assert passed_env["PA_ROOT"] == str(root.resolve())
    assert passed_env["PA_CWD"] == str(project_cwd.resolve())
    assert passed_env["PA_INSTALL_DEPS"] == "1"
    assert passed_env["PA_VERSION"] == "a1b2c3d"
    assert passed_env["PA_RESTART"] == "1"
