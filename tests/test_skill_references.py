from unittest.mock import patch

from pocket_agent.extensions.skills import call_skill_tool
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
