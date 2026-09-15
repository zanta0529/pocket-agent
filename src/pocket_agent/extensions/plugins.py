"""Plugin 系統：把 skills（知識）+ MCP server 設定（工具）打包成一個 zip 安裝。

Plugin zip 結構：
    my-plugin.zip
    ├── plugin.yaml        # name（必填）、description、version
    ├── skills/            # 零到多個 skill 資料夾
    │   └── <name>/SKILL.md
    └── mcp.yaml           # 要合併進 mcp_config.yaml 的 servers 設定（可省略）

安裝紀錄存在 ~/.pocket-agent/plugins.json，移除時據此清掉
所屬的 skill 資料夾與 MCP server 設定。

安裝目標一律是 ~/.pocket-agent/ 而非程式目錄：打包成 exe 後
程式目錄唯讀，而且會被下次更新整個覆蓋掉。
"""

import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import yaml

from pocket_agent.system.paths import bundle_dir, mcp_config_path, user_dir

log = logging.getLogger("pocket-agent")

# 安裝目標一律是使用者目錄：內建目錄打包後唯讀，
# 且會被下次程式更新整個覆蓋掉。
SKILLS_DIR = user_dir() / "skills"
COMMANDS_DIR = user_dir() / "commands"
SUBAGENTS_DIR = user_dir() / "subagents"
REGISTRY_PATH = user_dir() / "plugins.json"

# 衝突檢查要連內建的一起看，否則會裝出一個蓋不掉內建同名項目的鬼影。
BUILTIN_SKILLS_DIR = bundle_dir() / "skills"
BUILTIN_COMMANDS_DIR = bundle_dir() / "commands"
BUILTIN_SUBAGENTS_DIR = bundle_dir() / "subagents"


def _mcp_config_file() -> Path:
    return mcp_config_path()


# ── 安裝紀錄 ─────────────────────────────────────


def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            log.exception("plugins.json 解析失敗")
    return {}


def _save_registry(registry: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def list_plugins() -> list[dict]:
    """已安裝 plugin 的 [{name, version, description, skills, mcp_servers}]。"""
    registry = _load_registry()
    return [{"name": name, **info} for name, info in sorted(registry.items())]


# ── MCP 設定合併 ─────────────────────────────────


def _load_mcp_config() -> dict:
    path = _mcp_config_file()
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    data.setdefault("servers", {})
    if data["servers"] is None:
        data["servers"] = {}
    return data


def _save_mcp_config(data: dict) -> None:
    path = _mcp_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _parse_plugin_mcp(mcp_file: Path) -> dict:
    """plugin 的 mcp.yaml：接受 {servers: {...}} 或直接是 {name: {...}}。"""
    data = yaml.safe_load(mcp_file.read_text(encoding="utf-8")) or {}
    if "servers" in data and isinstance(data["servers"], dict):
        return data["servers"]
    return data if isinstance(data, dict) else {}


# ── 安裝 ─────────────────────────────────────────


def install_plugin_zip(zip_path: str | Path, overwrite: bool = False) -> dict:
    """安裝 plugin zip，回傳 {name, version, skills, mcp_servers}。

    衝突（plugin 已安裝 / skill 同名 / MCP server 同名）時
    raise FileExistsError；格式錯誤 raise ValueError。
    """
    zip_path = Path(zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if Path(member).is_absolute() or ".." in Path(member).parts:
                raise ValueError(f"zip 內含不安全路徑：{member}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            zf.extractall(tmp_dir)
            tmp = Path(tmp_dir)

            # plugin.yaml 可能在根目錄，或唯一的頂層資料夾內
            manifest_file = tmp / "plugin.yaml"
            if not manifest_file.exists():
                candidates = list(tmp.glob("*/plugin.yaml"))
                if len(candidates) == 1:
                    manifest_file = candidates[0]
                    tmp = manifest_file.parent
                else:
                    raise ValueError("zip 裡找不到 plugin.yaml，不是有效的 plugin 包。")

            manifest = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
            name = str(manifest.get("name", "")).strip()
            if not name:
                raise ValueError("plugin.yaml 缺少 name 欄位。")

            version = str(manifest.get("version", "")).strip()
            description = str(manifest.get("description", "")).strip()

            # 內容盤點
            skill_dirs = (
                sorted(md.parent for md in (tmp / "skills").glob("*/SKILL.md")) if (tmp / "skills").is_dir() else []
            )

            command_files = sorted((tmp / "commands").glob("*.md")) if (tmp / "commands").is_dir() else []

            agent_files = sorted((tmp / "subagents").glob("*.md")) if (tmp / "subagents").is_dir() else []

            mcp_file = tmp / "mcp.yaml"
            new_servers = _parse_plugin_mcp(mcp_file) if mcp_file.exists() else {}

            if not (skill_dirs or command_files or agent_files or new_servers):
                raise ValueError("plugin 裡沒有任何 skill、command、subagent 或 MCP server。")

            # 衝突檢查
            registry = _load_registry()
            mcp_config = _load_mcp_config()

            conflicts = []
            if name in registry:
                conflicts.append(f"plugin「{name}」已安裝")
            for d in skill_dirs:
                if (SKILLS_DIR / d.name).exists() or (BUILTIN_SKILLS_DIR / d.name).exists():
                    conflicts.append(f"skill「{d.name}」")
            for f in command_files:
                if (COMMANDS_DIR / f.name).exists() or (BUILTIN_COMMANDS_DIR / f.name).exists():
                    conflicts.append(f"command「{f.stem}」")
            for f in agent_files:
                if (SUBAGENTS_DIR / f.name).exists() or (BUILTIN_SUBAGENTS_DIR / f.name).exists():
                    conflicts.append(f"subagent「{f.stem}」")
            for server_name in new_servers:
                if server_name in mcp_config["servers"]:
                    conflicts.append(f"MCP server「{server_name}」")

            if conflicts and not overwrite:
                raise FileExistsError("、".join(conflicts))

            # 覆蓋模式：先移除同名舊 plugin，衝突項目直接蓋過
            if name in registry:
                uninstall_plugin(name)
                mcp_config = _load_mcp_config()

            # 就位：skills
            installed_skills = []
            for d in skill_dirs:
                target = SKILLS_DIR / d.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(d, target)
                installed_skills.append(d.name)

            # 就位：commands
            installed_commands = []
            for f in command_files:
                COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, COMMANDS_DIR / f.name)
                installed_commands.append(f.stem)

            # 就位：subagents
            installed_agents = []
            for f in agent_files:
                SUBAGENTS_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, SUBAGENTS_DIR / f.name)
                installed_agents.append(f.stem)

            # 就位：MCP servers
            for server_name, server_cfg in new_servers.items():
                mcp_config["servers"][server_name] = server_cfg
            if new_servers:
                _save_mcp_config(mcp_config)

            # 紀錄
            registry = _load_registry()
            registry[name] = {
                "version": version,
                "description": description,
                "skills": installed_skills,
                "commands": installed_commands,
                "subagents": installed_agents,
                "mcp_servers": list(new_servers),
            }
            _save_registry(registry)

            log.info(
                "plugin 已安裝：%s（%d skills, %d commands, %d agents, %d MCP）",
                name,
                len(installed_skills),
                len(installed_commands),
                len(installed_agents),
                len(new_servers),
            )

            return {
                "name": name,
                "version": version,
                "skills": installed_skills,
                "commands": installed_commands,
                "subagents": installed_agents,
                "mcp_servers": list(new_servers),
            }


# ── 移除 ─────────────────────────────────────────


def uninstall_plugin(name: str) -> None:
    """移除 plugin：刪除其 skill 資料夾、MCP server 設定與安裝紀錄。"""
    registry = _load_registry()
    info = registry.get(name)
    if info is None:
        raise KeyError(f"沒有安裝過 plugin「{name}」。")

    # 舊版把 plugin 內容裝在 repo 目錄，兩處都清才不會留下孤兒。
    legacy_root = Path(__file__).resolve().parent.parent

    for skill_name in info.get("skills", []):
        for base in (SKILLS_DIR, legacy_root / "skills"):
            target = base / skill_name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    for command_name in info.get("commands", []):
        for base in (COMMANDS_DIR, legacy_root / "commands"):
            (base / f"{command_name}.md").unlink(missing_ok=True)

    for agent_name in info.get("subagents", []):
        for base in (SUBAGENTS_DIR, legacy_root / "subagents"):
            (base / f"{agent_name}.md").unlink(missing_ok=True)

    server_names = info.get("mcp_servers", [])
    if server_names:
        mcp_config = _load_mcp_config()
        changed = False
        for server_name in server_names:
            if server_name in mcp_config["servers"]:
                del mcp_config["servers"][server_name]
                changed = True
        if changed:
            _save_mcp_config(mcp_config)

    del registry[name]
    _save_registry(registry)
    log.info("plugin 已移除：%s", name)
