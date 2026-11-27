"""一鍵安裝腳本與發布校驗流程靜態回歸測試。"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = REPO_ROOT / "build_scripts"


def test_windows_installers_are_pure_ascii_and_verify_checksums():
    for name in ("install.ps1", "install.cmd"):
        path = INSTALLER_DIR / name
        assert path.is_file(), f"Missing {name}"
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), f"{name} must stay pure ASCII to avoid Windows OEM codepage corruption"
        assert "PocketAgent-portable.zip" in text
        assert "SHA256SUMS.txt" in text
        assert "POCKET_AGENT_REQUIRE_CHECKSUM" in text
        assert "POCKET_AGENT_HOME" in text
        assert "POCKET_AGENT_VERSION" in text
        assert "PORTABLE" in text

    cmd_text = (INSTALLER_DIR / "install.cmd").read_text(encoding="ascii")
    assert "setx" not in cmd_text.lower()
    assert "--uninstall" in cmd_text
    assert "%PA_HOME%.staging." in cmd_text
    assert "PA_NO_PAUSE=1" in cmd_text
    assert 'endlocal & set "PATH=' in cmd_text
    assert "echo ;%CUR_USER_PATH%;" not in cmd_text
    assert '"%PA_HOME%\\bin\\pocket-agent.cmd" or run' not in cmd_text
    assert "pocket.cmd" in cmd_text
    assert "pocket-agent.cmd" in cmd_text

    ps1_text = (INSTALLER_DIR / "install.ps1").read_text(encoding="ascii")
    assert "[switch]$Uninstall" in ps1_text
    assert "POCKET_AGENT_UNINSTALL" in ps1_text
    assert '$ProgressPreference = "SilentlyContinue"' in ps1_text
    assert "$InstallDir.staging." in ps1_text
    assert "PA_NO_PAUSE=1" in ps1_text
    assert "pocket.cmd" in ps1_text
    assert "pocket-agent.cmd" in ps1_text


def test_portable_launcher_matches_updater_and_respects_no_pause():
    import build_scripts.build_portable as bp
    from pocket_agent.system import updater

    assert bp.POCKET_BAT == updater.PORTABLE_LAUNCHER_BAT
    assert "PA_NO_PAUSE" in bp.POCKET_BAT


def test_posix_installer_is_portable_sh_and_enforces_python314():
    sh_path = INSTALLER_DIR / "install.sh"
    assert sh_path.is_file()
    text = sh_path.read_text(encoding="utf-8")
    first_line = text.splitlines()[0]
    assert first_line == "#!/bin/sh"
    assert "set -eu" in text
    assert "[[" not in text
    assert "(3, 14)" in text
    assert "uv venv" in text
    assert "PORTABLE" in text
    assert "POCKET_AGENT_INSTALLER_MANAGED" in text
    assert '"$BIN_DIR/pocket"' in text
    assert '"$BIN_DIR/pocket-agent"' in text
    assert "--uninstall" in text


def test_pyproject_registers_short_and_full_cli_aliases():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["pocket"] == "pocket_agent.main:main"
    assert scripts["pocket-agent"] == "pocket_agent.main:main"


def test_release_workflow_generates_and_uploads_sha256sums():
    wf_text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "SHA256SUMS.txt" in wf_text
    assert "PocketAgent-portable.zip" in wf_text


def test_legacy_launchers_require_python_314():
    for name in ("run.bat", "run.sh", "run.command"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "3.11+" not in text
        assert "3.14+" in text
