from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict


class CommandResult:
    """統一的系統指令執行結果，前端根據 status 進行不同渲染"""

    def __init__(self, status: str, message: str, payload: Dict[str, Any] = None):
        self.status = status
        self.message = message
        self.payload = payload or {}


class SysCommand(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def execute(self, args_str: str, agent: Any) -> CommandResult:
        pass


class AddDirCommand(SysCommand):
    @property
    def name(self) -> str:
        return "add-dir"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        if not args_str.strip():
            return CommandResult("error", "⚠ 用法：/add-dir <目錄路徑>")

        status, msg, target_dir = agent.check_new_workspace_dir(args_str.strip())

        if status in ("invalid", "not_found"):
            return CommandResult("error", f"⚠ {msg}")
        elif status in ("in_default", "already_authorized"):
            return CommandResult("success", f"✓ {msg}")
        else:
            return CommandResult("needs_auth", msg, {"target_dir": target_dir})


class WorkspaceCommand(SysCommand):
    @property
    def name(self) -> str:
        return "workspace"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        lines = ["【預設工作區】", f"  {Path.cwd().resolve()}"]
        if agent.workspace_dirs:
            lines.append("\n【額外授權目錄】")
            for wd in agent.workspace_dirs:
                lines.append(f"  {wd}")
        else:
            lines.append("\n【額外授權目錄】無")
        return CommandResult("success", "\n".join(lines))


class ModelCommand(SysCommand):
    @property
    def name(self) -> str:
        return "model"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        parts = args_str.split()
        if len(parts) >= 2:
            provider = parts[0].lower()
            model_name = parts[1]
        elif len(parts) == 1:
            from pocket_agent.system.config import config

            provider = config.get("provider", "openai")
            model_name = parts[0]
        else:
            return CommandResult("interactive_menu", "請選擇模型")

        try:
            from pocket_agent.system.auth import get_openai_client

            agent.client = get_openai_client(provider=provider, model=model_name)
            agent.model = model_name
            return CommandResult("success", f"✓ 當次對話已切換為：{provider.upper()} - {model_name}")
        except Exception as e:
            return CommandResult("error", f"⚠ 切換模型失敗：{e}")


class LearnCommand(SysCommand):
    @property
    def name(self) -> str:
        return "learn"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        # CLI / GUI 透過狀態標記 "learn_interactive" 決定進入非同步與對話框流程
        return CommandResult("learn_interactive", "請等待萃取...")


class UndoCommand(SysCommand):
    @property
    def name(self) -> str:
        return "undo"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        import json
        import shutil

        from pocket_agent.system.paths import user_dir

        backup_dir = user_dir() / "backups"
        mapping_file = backup_dir / "latest_backup_map.json"

        if not mapping_file.exists():
            return CommandResult("error", "⚠ 找不到可用的備份紀錄。")

        try:
            mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
            if not mapping:
                return CommandResult("error", "⚠ 備份紀錄為空。")

            # 取出最後一筆紀錄
            original_path_str, backup_path_str = list(mapping.items())[-1]
            original_path = Path(original_path_str)
            backup_path = Path(backup_path_str)

            if not backup_path.exists():
                return CommandResult("error", f"⚠ 備份檔已遺失：{backup_path}")

            shutil.copy2(backup_path, original_path)

            # 從紀錄中移除並回寫
            mapping.pop(original_path_str)
            mapping_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

            return CommandResult("success", f"✓ 已成功還原檔案：{original_path.name}")
        except Exception as e:
            return CommandResult("error", f"⚠ 還原失敗：{e}")


class ConfigCommand(SysCommand):
    @property
    def name(self) -> str:
        return "config"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        from pocket_agent.system.config import config

        parts = args_str.split()
        if not parts:
            # 顯示當前設定
            data = config.json_provider._data
            if not data:
                return CommandResult("success", "【全域設定檔】\n(目前沒有自訂設定)")
            lines = ["【全域設定檔】"]
            for k, v in data.items():
                lines.append(f"  {k}: {v}")
            return CommandResult("success", "\n".join(lines))

        if len(parts) >= 3 and parts[0].lower() == "set":
            key = parts[1]
            val_str = " ".join(parts[2:])

            # 簡單型別轉換
            if val_str.lower() in ("true", "yes", "1"):
                val = True
            elif val_str.lower() in ("false", "no", "0"):
                val = False
            else:
                val = val_str

            config.set(key, val)
            return CommandResult("success", f"✓ 已將 {key} 設定為 {val}")

        return CommandResult("error", "⚠ 用法：/config 顯示設定，或 /config set <key> <value> 更改設定")


class SysCommandRegistry:
    def __init__(self):
        self._commands: Dict[str, SysCommand] = {}

    def register(self, command: SysCommand):
        self._commands[command.name] = command

    def execute(self, raw_input: str, agent: Any) -> CommandResult | None:
        raw_input = raw_input.strip()
        if not raw_input.startswith("/"):
            return None

        parts = raw_input.split(maxsplit=1)
        cmd_name = parts[0][1:]  # 移除斜線
        args_str = parts[1] if len(parts) > 1 else ""

        if cmd_name in self._commands:
            return self._commands[cmd_name].execute(args_str, agent)

        return None


# 全域單例
sys_registry = SysCommandRegistry()
sys_registry.register(AddDirCommand())
sys_registry.register(WorkspaceCommand())
sys_registry.register(ModelCommand())
sys_registry.register(LearnCommand())
sys_registry.register(UndoCommand())
sys_registry.register(ConfigCommand())
