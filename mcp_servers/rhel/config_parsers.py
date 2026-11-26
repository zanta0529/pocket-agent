"""RHEL 白名單設定檔安全摘要解析策略（Template Method ＋ Strategy Pattern）。"""

import re
from abc import ABC, abstractmethod

from mcp_servers.rhel import security, ssh_client


class BaseConfigStrategy(ABC):
    """白名單設定檔唯讀摘要基底類別（Template Method Pattern）。"""

    path: str

    @abstractmethod
    def build_remote_cmd(self, max_lines: int) -> str:
        """回傳遠端於 $real 上執行的只讀摘要指令。"""

    @abstractmethod
    def validate_output(self, raw_output: str, max_lines: int) -> str:
        """對遠端輸出執行嚴格格式驗證與遮罩，異常時 fail-closed 回傳 [ERROR]。"""

    def inspect(
        self,
        max_lines: int,
        ssh_host: str = "",
        ssh_port: str = "",
        ssh_user: str = "",
        ssh_password: str = "",
        ssh_key_path: str = "",
    ) -> str:
        """固定樣板流程：遠端真實路徑守衛 → 執行摘要命令 → 傳輸錯誤穿透 → 嚴格驗證輸出。"""
        cmd = security.remote_path_guard(self.path, directory=False) + self.build_remote_cmd(max_lines)
        result = ssh_client.run_remote(
            cmd,
            ssh_host,
            ssh_port,
            ssh_user,
            ssh_password,
            ssh_key_path,
            tool_name="rhel_read_config",
        )
        if ssh_client.is_transport_or_auth_error(result):
            return result
        if result.startswith("[ERROR]"):
            return f"[ERROR] 無法安全取得 {self.path} 設定摘要，已捨棄內容。"
        return self.validate_output(result, max_lines)


class SelinuxConfigStrategy(BaseConfigStrategy):
    """/etc/selinux/config：僅允許 SELINUX 列舉值。"""

    path = "/etc/selinux/config"
    _ALLOWED_ENUMS = frozenset({"SELINUX=enforcing", "SELINUX=permissive", "SELINUX=disabled"})

    def build_remote_cmd(self, max_lines: int) -> str:
        return (
            "awk -F= -- '/^[[:space:]]*SELINUX[[:space:]]*=/ {"
            'v=$2; gsub(/[[:space:]]/,"",v); '
            'if (v == "enforcing" || v == "permissive" || v == "disabled") print "SELINUX=" v'
            '}\' "$real"'
        )

    def validate_output(self, raw_output: str, max_lines: int) -> str:
        if raw_output not in self._ALLOWED_ENUMS:
            return "[ERROR] 無法取得唯一且有效的 SELINUX 設定，已捨棄內容。"
        return raw_output


class SshdConfigStrategy(BaseConfigStrategy):
    """/etc/ssh/sshd_config：僅回傳白名單安全基線鍵值。"""

    path = "/etc/ssh/sshd_config"
    _ALLOWED_KEYS = frozenset(
        {
            "port",
            "listenaddress",
            "permitrootlogin",
            "pubkeyauthentication",
            "passwordauthentication",
            "permitemptypasswords",
            "kbdinteractiveauthentication",
            "challengeresponseauthentication",
            "usepam",
            "maxauthtries",
            "maxsessions",
            "logingracetime",
            "x11forwarding",
            "allowagentforwarding",
            "allowtcpforwarding",
            "clientaliveinterval",
            "clientalivecountmax",
            "loglevel",
            "subsystem",
        }
    )

    def build_remote_cmd(self, max_lines: int) -> str:
        keys_space = " ".join(sorted(self._ALLOWED_KEYS))
        return (
            f'awk -v keys="{keys_space}" -v lim={max_lines} '
            '\'BEGIN {split(keys, arr, " "); for (i in arr) allow[arr[i]]=1} '
            "/^[[:space:]]*($|#)/ {next} "
            '{k=tolower($1); if (k in allow) {print; n++; if (n>=lim) exit}}\' "$real"'
        )

    def validate_output(self, raw_output: str, max_lines: int) -> str:
        if raw_output == "(沒有輸出)":
            return raw_output
        lines: list[str] = []
        for line in raw_output.splitlines()[:max_lines]:
            parts = line.strip().split(None, 1)
            if not parts or parts[0].lower() not in self._ALLOWED_KEYS:
                return "[ERROR] sshd_config 摘要包含非白名單欄位，已捨棄內容。"
            lines.append(line.strip())
        return "\n".join(lines) if lines else "(沒有輸出)"


class SysctlConfigStrategy(BaseConfigStrategy):
    """/etc/sysctl.conf：僅回傳白名單系統調校參數。"""

    path = "/etc/sysctl.conf"
    _ALLOWED_KEYS = frozenset(
        {
            "net.ipv4.ip_forward",
            "net.ipv6.conf.all.forwarding",
            "net.core.somaxconn",
            "net.core.netdev_max_backlog",
            "net.ipv4.tcp_syncookies",
            "net.ipv4.tcp_tw_reuse",
            "net.ipv4.tcp_fin_timeout",
            "vm.swappiness",
            "vm.overcommit_memory",
            "vm.panic_on_oom",
            "vm.dirty_ratio",
            "vm.dirty_background_ratio",
            "fs.file-max",
            "fs.inotify.max_user_watches",
            "kernel.randomize_va_space",
            "kernel.sysrq",
            "kernel.dmesg_restrict",
            "kernel.pid_max",
        }
    )
    _SAFE_VAL_RE = re.compile(r"^[0-9\s\-]+$")

    def build_remote_cmd(self, max_lines: int) -> str:
        keys_space = " ".join(sorted(self._ALLOWED_KEYS))
        return (
            f'awk -F= -v keys="{keys_space}" -v lim={max_lines} '
            '\'BEGIN {split(keys, arr, " "); for (i in arr) allow[arr[i]]=1} '
            "/^[[:space:]]*($|#|;)/ {next} "
            '{k=$1; gsub(/^[[:space:]]+|[[:space:]]+$/, "", k); '
            'v=$2; gsub(/^[[:space:]]+|[[:space:]]+$/, "", v); '
            'if (k in allow) {print k "=" v; n++; if (n>=lim) exit}}\' "$real"'
        )

    def validate_output(self, raw_output: str, max_lines: int) -> str:
        if raw_output == "(沒有輸出)":
            return raw_output
        lines: list[str] = []
        for line in raw_output.splitlines()[:max_lines]:
            if "=" not in line:
                return "[ERROR] sysctl.conf 摘要格式異常，已捨棄內容。"
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key not in self._ALLOWED_KEYS or not self._SAFE_VAL_RE.match(val):
                return "[ERROR] sysctl.conf 摘要包含非白名單參數或異常數值，已捨棄內容。"
            lines.append(f"{key}={val}")
        return "\n".join(lines) if lines else "(沒有輸出)"


class LimitsConfigStrategy(BaseConfigStrategy):
    """/etc/security/limits.conf：僅回傳合法 4 欄資源上限規則。"""

    path = "/etc/security/limits.conf"
    _VALID_TYPES = frozenset({"soft", "hard", "-"})
    _VALID_ITEMS = frozenset(
        {
            "nofile",
            "nproc",
            "memlock",
            "core",
            "stack",
            "fsize",
            "as",
            "data",
            "rss",
            "locks",
            "sigpending",
            "msgqueue",
            "nice",
            "rtprio",
            "maxlogins",
            "maxsyslogins",
            "cpu",
        }
    )

    def build_remote_cmd(self, max_lines: int) -> str:
        return (
            f"awk -v lim={max_lines} "
            "'/^[[:space:]]*($|#)/ {next} "
            'NF == 4 {print $1, $2, $3, $4; n++; if (n>=lim) exit}\' "$real"'
        )

    def validate_output(self, raw_output: str, max_lines: int) -> str:
        if raw_output == "(沒有輸出)":
            return raw_output
        lines: list[str] = []
        for line in raw_output.splitlines()[:max_lines]:
            parts = line.split()
            if len(parts) != 4 or parts[1] not in self._VALID_TYPES or parts[2] not in self._VALID_ITEMS:
                return "[ERROR] limits.conf 摘要包含非白名單欄位，已捨棄內容。"
            lines.append(" ".join(parts))
        return "\n".join(lines) if lines else "(沒有輸出)"


class FstabConfigStrategy(BaseConfigStrategy):
    """/etc/fstab：省略來源裝置欄位並逐項遮罩掛載選項中的敏感鍵值。"""

    path = "/etc/fstab"
    _SENSITIVE_OPT_KEYWORDS = ("pass", "cred", "secret", "token", "user", "key", "auth")

    def build_remote_cmd(self, max_lines: int) -> str:
        return (
            f"awk -v lim={max_lines} "
            "'/^[[:space:]]*($|#)/ {next} "
            "NF >= 4 { "
            'm = split($4, opts, ","); out = ""; '
            "for (i = 1; i <= m; i++) { "
            'o = opts[i]; eq = index(o, "="); '
            "if (eq > 0) { "
            "k = tolower(substr(o, 1, eq - 1)); "
            'if (k ~ /(pass|cred|secret|token|user|key|auth)/) o = "[REDACTED_OPT]"; '
            "} "
            'out = (i == 1 ? o : out "," o); '
            "} "
            'print "[device-omitted]", $2, $3, out, ($5 ? $5 : "0"), ($6 ? $6 : "0"); '
            'n++; if (n>=lim) exit}\' "$real"'
        )

    def validate_output(self, raw_output: str, max_lines: int) -> str:
        if raw_output == "(沒有輸出)":
            return raw_output
        sanitized: list[str] = []
        for line in raw_output.splitlines()[:max_lines]:
            parts = line.split()
            if len(parts) < 4:
                return "[ERROR] fstab 摘要格式異常，已捨棄內容。"
            opts = parts[3].split(",")
            clean_opts: list[str] = []
            for opt in opts:
                if "=" in opt:
                    k, _ = opt.split("=", 1)
                    if any(kw in k.lower() for kw in self._SENSITIVE_OPT_KEYWORDS):
                        clean_opts.append("[REDACTED_OPT]")
                        continue
                clean_opts.append(opt)
            parts[3] = ",".join(clean_opts)
            sanitized.append(" ".join(parts))
        return "\n".join(sanitized) if sanitized else "(沒有輸出)"


_STRATEGIES: tuple[BaseConfigStrategy, ...] = (
    SelinuxConfigStrategy(),
    SshdConfigStrategy(),
    SysctlConfigStrategy(),
    LimitsConfigStrategy(),
    FstabConfigStrategy(),
)

CONFIG_STRATEGIES: dict[str, BaseConfigStrategy] = {s.path: s for s in _STRATEGIES}
SUPPORTED_CONFIG_PATHS: tuple[str, ...] = tuple(CONFIG_STRATEGIES.keys())


def get_config_strategy(file_path: str) -> BaseConfigStrategy | None:
    """依設定檔路徑取得對應的結構化安全摘要策略。"""
    return CONFIG_STRATEGIES.get(file_path.strip())
