"""集中化 MCP 稽核日誌寫入模組（含防日誌注入清洗與機密遮罩）。"""

import os
import re
import time
from pathlib import Path

from mcp_servers._shared.bootstrap import ensure_project_paths
from mcp_servers._shared.redaction import DEFAULT_REDACTION_POLICY, RedactionPolicy

_AUDIT_TARGET_SANITIZE_RE = re.compile(r"[\s='\"\\]+")
_MAX_AUDIT_FIELD_CHARS = 300
_MAX_AUDIT_DETAIL_CHARS = 1000


def get_audit_log_path() -> Path:
    """解析集中化稽核日誌 (agent.log) 路徑。"""
    env_log = (os.getenv("POCKET_AGENT_LOG_FILE") or "").strip()
    if env_log:
        return Path(env_log).expanduser().resolve()
    try:
        from pocket_agent.system.paths import log_dir

        return log_dir() / "agent.log"
    except ImportError:
        ensure_project_paths()
        try:
            from pocket_agent.system.paths import log_dir

            return log_dir() / "agent.log"
        except ImportError:
            return Path.home() / ".pocket-agent" / "logs" / "agent.log"


class AuditLogger:
    """將 MCP 工具操作以統一不可翻譯格式寫入 agent.log（自動遮罩機密並防止日誌注入）。"""

    def __init__(
        self,
        mcp_tag: str,
        *,
        redaction_policy: RedactionPolicy = DEFAULT_REDACTION_POLICY,
    ) -> None:
        self.mcp_tag = mcp_tag
        self.redaction_policy = redaction_policy

    def log(
        self,
        tool_name: str,
        command: str,
        *,
        target: str = "",
        status: str = "OK",
        detail: str = "",
        extra_secret: str = "",
    ) -> None:
        """寫入單筆稽核紀錄；失敗時靜默忽略以免中斷工具執行。"""
        try:
            log_path = get_audit_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            now_t = time.time()
            lt = time.localtime(now_t)
            ms = int((now_t - int(now_t)) * 1000)
            ts = f"{time.strftime('%Y-%m-%d %H:%M:%S', lt)},{ms:03d}"

            policy = self.redaction_policy
            raw_cmd = " ".join(policy.redact(command, extra_secret=extra_secret).splitlines()).strip()
            safe_cmd = policy.clip_text(raw_cmd, _MAX_AUDIT_FIELD_CHARS)

            raw_target = " ".join(policy.redact(target, extra_secret=extra_secret).splitlines()).strip()
            safe_target = policy.clip_text(
                _AUDIT_TARGET_SANITIZE_RE.sub("_", raw_target),
                _MAX_AUDIT_FIELD_CHARS,
            )

            raw_detail = " ".join(policy.redact(detail, extra_secret=extra_secret).splitlines()).strip()
            safe_detail = policy.clip_text(raw_detail, _MAX_AUDIT_DETAIL_CHARS)

            parts = [f"{ts} [AUDIT] [MCP:{self.mcp_tag}] tool={tool_name} status={status}"]
            if safe_target:
                parts.append(f"target={safe_target}")
            if safe_cmd:
                parts.append(f"command={safe_cmd!r}")
            if safe_detail:
                parts.append(f"detail={safe_detail!r}")

            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(" ".join(parts) + "\n")
        except Exception:
            pass
