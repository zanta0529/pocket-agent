from unittest.mock import patch

import pytest

from pocket_agent.extensions.skills import call_skill_tool
from pocket_agent.system.i18n import set_locale
from pocket_agent.tools.fs import _resolve_skill_reference_path, _validate_safe_path, call_fs_tool


def test_bundled_skills_allowed_in_safe_path(tmp_path):
    fake_bundle = tmp_path / "bundle"
    skills_dir = fake_bundle / "skills" / "abc"
    skills_dir.mkdir(parents=True)
    ref_file = skills_dir / "ref.md"
    ref_file.write_text("reference data", encoding="utf-8")

    with patch("pocket_agent.system.paths.bundle_dir", return_value=fake_bundle):
        validated = _validate_safe_path(str(ref_file))
        assert validated == ref_file.resolve()


def test_resolve_skill_reference_path_with_prefix(tmp_path):
    fake_user_dir = tmp_path / "user"
    user_skills = fake_user_dir / "skills" / "my_skill" / "reference"
    user_skills.mkdir(parents=True)
    target_ref = user_skills / "guide.md"
    target_ref.write_text("guide content", encoding="utf-8")

    with patch("pocket_agent.extensions.skills.USER_SKILLS_DIR", fake_user_dir / "skills"):
        # 1. 包含技能名稱: my_skill/reference/guide.md
        resolved = _resolve_skill_reference_path("my_skill/reference/guide.md")
        assert resolved == target_ref.resolve()

        # 2. 省略技能名稱: reference/guide.md
        resolved_short = _resolve_skill_reference_path("reference/guide.md")
        assert resolved_short == target_ref.resolve()


def test_call_fs_tool_reads_skill_reference(tmp_path):
    fake_user_dir = tmp_path / "user"
    user_skills = fake_user_dir / "skills" / "my_skill" / "reference"
    user_skills.mkdir(parents=True)
    target_ref = user_skills / "spec.md"
    target_ref.write_text("specification details", encoding="utf-8")

    with (
        patch("pocket_agent.extensions.skills.USER_SKILLS_DIR", fake_user_dir / "skills"),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_user_dir),
    ):
        result = call_fs_tool("read_file", {"path": "reference/spec.md"}, on_confirm=None)
        assert "specification details" in result
        # 確保不洩漏主機實體根目錄
        assert str(fake_user_dir) not in result


def test_call_fs_tool_get_file_info_privacy(tmp_path):
    fake_user_dir = tmp_path / "user"
    user_skills = fake_user_dir / "skills" / "my_skill"
    user_skills.mkdir(parents=True)
    target_ref = user_skills / "info.txt"
    target_ref.write_text("hello", encoding="utf-8")

    with (
        patch("pocket_agent.extensions.skills.USER_SKILLS_DIR", fake_user_dir / "skills"),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_user_dir),
    ):
        info_out = call_fs_tool("get_file_info", {"path": "my_skill/info.txt"}, on_confirm=None)
        assert "路徑: my_skill/info.txt" in info_out
        assert str(fake_user_dir) not in info_out


def test_call_skill_tool_guidance_no_host_path_leak(tmp_path):
    skill_body = "This skill uses reference/doc.md"
    with (
        patch("pocket_agent.extensions.skills.load_skill", return_value=skill_body),
        patch("pocket_agent.extensions.skills.list_skills", return_value=[{"name": "demo"}]),
    ):
        prompt = call_skill_tool({"name": "demo"})
        assert "Skill 'demo' is active" in prompt
        assert "within this skill" in prompt
        assert "Do not guess host absolute paths" in prompt


def test_call_skill_tool_lists_bundled_resources_without_hardcoded_dirs(tmp_path):
    fake_user_dir = tmp_path / "user"
    skill_dir = fake_user_dir / "skills" / "generate-system-docs"
    (skill_dir / "references" / "templates").mkdir(parents=True)
    (skill_dir / "examples").mkdir()
    (skill_dir / ".hidden").mkdir()
    (skill_dir / "SKILL.md").write_text("Use references/templates/", encoding="utf-8")
    (skill_dir / "helper.txt").write_text("helper", encoding="utf-8")

    with (
        patch("pocket_agent.extensions.skills.load_skill", return_value="Use references/templates/"),
        patch(
            "pocket_agent.extensions.skills.list_skills",
            return_value=[{"name": "generate-system-docs", "path": skill_dir / "SKILL.md"}],
        ),
    ):
        prompt = call_skill_tool({"name": "generate-system-docs"}, workspace_dirs=[str(tmp_path / "proj")])
        assert "Skill 'generate-system-docs' is active" in prompt
        assert "within this skill" in prompt
        assert "Do not guess host absolute paths" in prompt
        assert "- generate-system-docs/examples/" in prompt
        assert "- generate-system-docs/references/" in prompt
        assert "- generate-system-docs/helper.txt" in prompt
        assert (
            prompt.index("generate-system-docs/examples/")
            < prompt.index("generate-system-docs/references/")
            < prompt.index("generate-system-docs/helper.txt")
        )
        assert "SKILL.md" not in prompt
        assert ".hidden" not in prompt
        assert ".pocket-agent" not in prompt
        assert ".agents" not in prompt
        assert str(fake_user_dir) not in prompt


def test_call_skill_tool_resource_list_respects_limit_and_skips_escaping_symlink(tmp_path):
    skill_dir = tmp_path / "user" / "skills" / "big-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    for idx in range(25):
        (skill_dir / f"dir_{idx:02d}").mkdir()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    try:
        (skill_dir / "aaa_escape").symlink_to(outside_dir, target_is_directory=True)
        symlink_created = True
    except OSError:
        symlink_created = False

    with (
        patch("pocket_agent.extensions.skills.load_skill", return_value="body"),
        patch(
            "pocket_agent.extensions.skills.list_skills",
            return_value=[{"name": "big-skill", "path": skill_dir / "SKILL.md"}],
        ),
    ):
        prompt = call_skill_tool({"name": "big-skill"}, workspace_dirs=[str(tmp_path / "proj")])
        listed = [line.strip() for line in prompt.splitlines() if line.strip().startswith("- big-skill/")]
        assert len(listed) == 20
        assert "- big-skill/dir_00/" in listed
        assert "- big-skill/dir_19/" in listed
        assert "- big-skill/dir_20/" not in listed
        if symlink_created:
            assert "- big-skill/aaa_escape/" not in listed


def test_validate_safe_path_workspace_pocket_agent_skills_gives_skill_hint(tmp_path):
    project_dir = tmp_path / "infoman"
    project_dir.mkdir(parents=True)
    fake_global = tmp_path / "home" / ".pocket-agent"
    fake_global.mkdir(parents=True)
    wrong_path = project_dir / ".pocket-agent" / "skills" / "generate-system-docs" / "references"

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        with pytest.raises(ValueError) as exc_info:
            _validate_safe_path(str(wrong_path), workspace_dirs=[str(project_dir)])

        msg = str(exc_info.value)
        assert "安全限制：禁止存取敏感目錄或檔案 (.pocket-agent)" in msg
        assert "<skill_name>/<subpath>" in msg
        assert "read_file" in msg
        assert "list_dir" in msg
        assert ".pocket-agent/skills" not in msg
        assert str(fake_global) not in msg

        res = call_fs_tool(
            "list_dir",
            {"path": str(wrong_path)},
            on_confirm=None,
            workspace_dirs=[str(project_dir)],
        )
        assert "[ERROR]" in res
        assert "<skill_name>/<subpath>" in res


def test_validate_safe_path_workspace_pocket_agent_non_skill_keeps_generic_error(tmp_path):
    project_dir = tmp_path / "infoman"
    project_dir.mkdir(parents=True)
    fake_global = tmp_path / "home" / ".pocket-agent"
    fake_global.mkdir(parents=True)
    wrong_config = project_dir / ".pocket-agent" / "config.json"

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        with pytest.raises(ValueError) as exc_info:
            _validate_safe_path(str(wrong_config), workspace_dirs=[str(project_dir)])

        msg = str(exc_info.value)
        assert msg == "安全限制：禁止存取敏感目錄或檔案 (.pocket-agent)"
        assert "<skill_name>/<subpath>" not in msg


def test_validate_safe_path_workspace_pocket_agent_i18n_en_us(tmp_path):
    set_locale("en_US")
    project_dir = tmp_path / "infoman"
    project_dir.mkdir(parents=True)
    fake_global = tmp_path / "home" / ".pocket-agent"
    fake_global.mkdir(parents=True)
    wrong_path = project_dir / ".pocket-agent" / "skills" / "generate-system-docs" / "references"
    wrong_config = project_dir / ".pocket-agent" / "config.json"

    expected_prefix = "Security restriction: access to sensitive directory or file (.pocket-agent) is forbidden"

    with (
        patch("pathlib.Path.cwd", return_value=project_dir),
        patch("pocket_agent.system.paths.user_dir", return_value=fake_global),
    ):
        with pytest.raises(ValueError) as exc_info:
            _validate_safe_path(str(wrong_path), workspace_dirs=[str(project_dir)])

        msg = str(exc_info.value)
        assert expected_prefix in msg
        assert "<skill_name>/<subpath>" in msg
        assert "read_file" in msg
        assert "list_dir" in msg
        assert ".pocket-agent/skills" not in msg
        assert str(fake_global) not in msg

        with pytest.raises(ValueError) as exc_info_cfg:
            _validate_safe_path(str(wrong_config), workspace_dirs=[str(project_dir)])

        cfg_msg = str(exc_info_cfg.value)
        assert cfg_msg == expected_prefix
        assert "<skill_name>/<subpath>" not in cfg_msg
