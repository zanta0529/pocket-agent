"""統一機密遮罩與安全邊界裁切策略（Strategy Pattern）。"""

import re
from abc import ABC, abstractmethod

from mcp_servers._shared.bootstrap import CONSTANTS, t

SENSITIVE_COL_RE = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|token|passphrase|api[_-]?key"
    r"|private[_-]?key|preshared[_-]?key|credential|connectionstring|ssn)"
)

_PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{1,16384}?-----END [A-Z0-9 ]*PRIVATE KEY-----")
_URL_CREDS_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{0,31}://)[^\s/@\"':]+:[^\s/@\"']+@")
_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{20,})\b"
)
_AUTH_BEARER_RE = re.compile(r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|token)\s+)([^\s\"',;]+)")
_SENSITIVE_KV_KEYS = (
    r"(?:password|passwd|secret(?:[_-]?key)?|github_token|(?:access|auth|refresh|bearer|session|api)[_-]token"
    r"|api[_-]?key|apikey|client_secret|passphrase|private_key|preshared_key|connectionstring)"
)
_KV_RE = re.compile(
    rf"(?i)((?:['\"])?\b(?:token|{_SENSITIVE_KV_KEYS})\b['\"]?\s*[:=]\s*)"
    r"(?!\[REDACTED(?:_[A-Z_]+)?\](?:$|[\s#;\"',)}\]]))"
    r"([^\s#;\"',)}\]]+|\"[^\"]*\"|'[^']*')"
)


class RedactionPolicy(ABC):
    """輸出遮罩與安全裁切策略介面。"""

    @abstractmethod
    def redact(self, text: str, *, extra_secret: str = "") -> str:
        """對輸入文字執行機密遮罩。"""

    def safe_cut_index(self, text: str, limit: int) -> int:
        """計算不超過上限的安全裁切索引，避免截斷 [REDACTED...] 標記。"""
        if len(text) <= limit:
            return len(text)
        cut = limit
        bracket_start = text.rfind("[REDACTED", max(0, cut - 32), cut + 9)
        if bracket_start != -1 and bracket_start < cut:
            bracket_end = text.find("]", bracket_start, cut + 32)
            if bracket_end != -1 and bracket_end >= cut:
                cut = bracket_end + 1
        return cut

    def clip_text(self, text: str, limit: int, suffix: str = "...") -> str:
        """在不切斷 [REDACTED...] 標記的前提下將文字裁切至指定字元上限。"""
        if len(text) <= limit:
            return text
        cut = self.safe_cut_index(text, limit)
        if cut >= len(text):
            return text
        return text[:cut] + suffix

    def truncate_output(
        self,
        text: str,
        *,
        max_chars: int = CONSTANTS.max_output_chars,
        hint: str = "",
    ) -> tuple[str, bool]:
        """限制單次輸出不超過上限，超限時附加多語系截斷提示。"""
        if len(text) <= max_chars:
            return text, False
        cut = self.safe_cut_index(text, max_chars)
        if cut >= len(text):
            return text, False
        notice = t("mcp_shared.output_truncated", max_chars=max_chars, hint=hint)
        return text[:cut] + notice, True


class DefaultRedactionPolicy(RedactionPolicy):
    """預設跨伺服器通用機密遮罩策略（涵蓋私鑰、內嵌帳密 URL、常見 Token 與敏感鍵值對）。"""

    def redact(self, text: str, *, extra_secret: str = "") -> str:
        if not text:
            return text
        out = text.replace(extra_secret, "[REDACTED_TOKEN]") if extra_secret else text
        if "-----END " in out:
            out = _PEM_RE.sub("[REDACTED_PRIVATE_KEY]", out)
        if "://" in out and "@" in out:
            out = _URL_CREDS_RE.sub(r"\1[REDACTED]@", out)
        out = _TOKEN_RE.sub("[REDACTED_TOKEN]", out)
        out = _AUTH_BEARER_RE.sub(r"\1[REDACTED]", out)
        return _KV_RE.sub(r"\1[REDACTED]", out)


DEFAULT_REDACTION_POLICY = DefaultRedactionPolicy()
