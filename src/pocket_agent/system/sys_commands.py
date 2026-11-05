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


class RmDirCommand(SysCommand):
    @property
    def name(self) -> str:
        return "rm-dir"

    def execute(self, args_str: str, agent: Any) -> CommandResult:
        if not args_str.strip():
            return CommandResult("interactive_rm_dir", "", payload={"dirs": agent.workspace_dirs})

        success, msg = agent.remove_workspace_dir(args_str.strip())
        if success:
            return CommandResult("success", f"✓ {msg}")
        else:
            return CommandResult("error", f"⚠ {msg}")


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
            # 將參數交還給前端處理，以確保 UI 狀態同步與連線清理
            return CommandResult("interactive_menu", "", payload={"args": args_str})
        elif len(parts) == 1:
            from pocket_agent.system.config import config

            provider = config.get("active_provider", "openai")
            model_name = parts[0]
            return CommandResult("interactive_menu", "", payload={"args": f"{provider} {model_name}"})
        else:
            return CommandResult("interactive_menu", "請選擇模型")


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
        from pathlib import Path

        from pocket_agent.system.paths import user_dir

        backup_dir = user_dir() / "backups"
        mapping_file = backup_dir / "latest_backup_map.json"

        if not mapping_file.exists():
            return CommandResult("error", "⚠ 找不到可用的備份紀錄。")

        try:
            mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
            if not mapping:
                return CommandResult("error", "⚠ 備份紀錄為空。")

            if not args_str.strip():
                return CommandResult("interactive_undo", "選擇要還原的檔案", {"mapping": mapping})

            # 有提供 args_str，視為指定還原的路徑
            target_path_str = args_str.strip()
            if target_path_str not in mapping:
                return CommandResult("error", f"⚠ 備份紀錄中找不到該檔案：{target_path_str}")

            backup_path = Path(mapping[target_path_str])
            original_path = Path(target_path_str)

            if not backup_path.exists():
                return CommandResult("error", f"⚠ 備份檔已遺失：{backup_path}")

            size = backup_path.stat().st_size
            bkup_name = backup_path.name

            shutil.copy2(backup_path, original_path)

            # 從紀錄中移除並回寫
            mapping.pop(target_path_str)
            mapping_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

            msg = f"✓ 已成功還原檔案！\n  從備份檔: {bkup_name} ({size} bytes)\n  覆蓋回到: {original_path.resolve()}"
            return CommandResult("success", msg)
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
            # 交由前端 CLI 渲染互動式選單
            data = config.json_provider._data
            return CommandResult("interactive_config", "", payload={"data": data})

        if len(parts) >= 3 and parts[0].lower() == "set":
            key = parts[1]
            val_str = " ".join(parts[2:])

            # 簡單型別轉換 (布林值、數字、None 或字串)
            if val_str.lower() in ("true", "yes"):
                val = True
            elif val_str.lower() in ("false", "no"):
                val = False
            elif val_str.lower() in ("none", "null"):
                val = None
            else:
                try:
                    if "." in val_str:
                        val = float(val_str)
                    else:
                        val = int(val_str)
                except ValueError:
                    val = val_str

            config.set(key, val)
            is_secret = any(x in key.lower() for x in ("key", "token", "password", "secret"))
            display_val = "********" if is_secret else val
            return CommandResult("success", f"✓ 已將 {key} 設定為 {display_val}")

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
sys_registry.register(RmDirCommand())
sys_registry.register(WorkspaceCommand())
sys_registry.register(ModelCommand())
sys_registry.register(LearnCommand())
sys_registry.register(UndoCommand())
sys_registry.register(ConfigCommand())
