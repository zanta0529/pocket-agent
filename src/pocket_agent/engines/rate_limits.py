"""共用 HTTP 回應標頭限流快照解析與多語系額度格式化模組。"""

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pocket_agent.engines.base import BaseEngine, QuotaInfo

_RATE_LIMIT_HEADER_RE = re.compile(r"^x-ratelimit-(limit|remaining|reset)(?:-(requests|tokens))?(?:-(.+))?$")
_METRIC_ORDER = {"requests": 0, "tokens": 1, "general": 2}
_WINDOW_ORDER = {"day": 0, "month": 1, "hour": 2, "minute": 3, "": 4}


@dataclass(frozen=True)
class RateLimitBucket:
    """單一限流維度（例如每日請求數或每分鐘 Token 數）之配額快照。"""

    metric: str
    window: str
    limit: int | None = None
    remaining: int | None = None
    resets_at: float | None = None


@dataclass(frozen=True)
class RateLimitSnapshot:
    """單次 API 回應標頭擷取的完整限流快照。"""

    buckets: tuple[RateLimitBucket, ...]
    captured_at: float


def _parse_non_negative_int(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    try:
        val = int(text)
        return val if val >= 0 else None
    except ValueError:
        try:
            fval = float(text)
            if fval >= 0 and fval.is_integer():
                return int(fval)
        except ValueError:
            pass
    return None


def _parse_reset_timestamp(raw: str, captured_at: float) -> float | None:
    dur = BaseEngine.parse_duration_seconds(raw)
    if dur is not None:
        return captured_at + dur
    try:
        val = float(raw)
    except ValueError, TypeError:
        return None
    if val > 1_000_000_000_000:
        return val / 1000.0
    if val > 1_000_000_000:
        return val
    return None


def parse_rate_limit_snapshot(
    headers: Any,
    *,
    captured_at: float | None = None,
) -> RateLimitSnapshot | None:
    """從回應標頭解析 x-ratelimit-* 限流配額；無有效維度時回傳 None。"""
    if not headers or not hasattr(headers, "items"):
        return None

    ts = captured_at if captured_at is not None else time.time()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for raw_key, raw_val in headers.items():
        key = str(raw_key).strip().lower()
        match = _RATE_LIMIT_HEADER_RE.match(key)
        if not match:
            continue
        kind, metric, window = match.groups()
        bucket_key = (metric or "general", (window or "").strip())
        entry = grouped.setdefault(bucket_key, {})
        val_str = str(raw_val).strip()

        if kind in ("limit", "remaining"):
            parsed_int = _parse_non_negative_int(val_str)
            if parsed_int is not None:
                entry[kind] = parsed_int
        elif kind == "reset":
            parsed_ts = _parse_reset_timestamp(val_str, ts)
            if parsed_ts is not None:
                entry["resets_at"] = parsed_ts

    buckets: list[RateLimitBucket] = []
    for (metric, window), data in grouped.items():
        if data.get("limit") is None and data.get("remaining") is None:
            continue
        buckets.append(
            RateLimitBucket(
                metric=metric,
                window=window,
                limit=data.get("limit"),
                remaining=data.get("remaining"),
                resets_at=data.get("resets_at"),
            )
        )

    if not buckets:
        return None

    buckets.sort(
        key=lambda b: (
            _METRIC_ORDER.get(b.metric, 99),
            _WINDOW_ORDER.get(b.window, 99),
            b.window,
        )
    )
    return RateLimitSnapshot(buckets=tuple(buckets), captured_at=ts)


def _format_bucket_label(metric: str, window: str) -> str:
    from pocket_agent.system.i18n import t

    metric_text = t(f"quota.rl_metric_{metric}")
    if not window:
        return metric_text
    if window in ("minute", "hour", "day", "month"):
        window_text = t(f"quota.rl_window_{window}")
        return f"{window_text} {metric_text}"
    return f"{metric_text} ({window})"


def _format_reset_suffix(resets_at: float | None, now_ts: float) -> str:
    if resets_at is None:
        return ""
    from pocket_agent.system.i18n import t

    reset_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
    time_str = reset_dt.strftime("%Y-%m-%d %H:%M:%S")
    if resets_at <= now_ts:
        return t("quota.rl_reset_passed", time=time_str)

    secs = int(max(0.0, resets_at - now_ts))
    hours, remainder = divmod(secs, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        countdown = t("quota.countdown_hms", hours=hours, minutes=minutes, seconds=seconds)
    elif minutes:
        countdown = t("quota.countdown_ms", minutes=minutes, seconds=seconds)
    else:
        countdown = t("quota.countdown_s", seconds=seconds)
    return t("quota.rl_reset_in", time=time_str, countdown=countdown)


def format_rate_limit_quota(snapshot: RateLimitSnapshot, *, now: float | None = None) -> QuotaInfo:
    """將 RateLimitSnapshot 格式化為與 /usage 相容的 QuotaInfo。"""
    from pocket_agent.system.i18n import t

    now_ts = now if now is not None else time.time()
    lines: list[str] = []

    for bucket in snapshot.buckets:
        label = _format_bucket_label(bucket.metric, bucket.window)
        reset_suffix = _format_reset_suffix(bucket.resets_at, now_ts)
        if bucket.limit is not None and bucket.remaining is not None:
            used = max(0, bucket.limit - bucket.remaining)
            lines.append(
                t(
                    "quota.rl_bucket_full",
                    label=label,
                    remaining=bucket.remaining,
                    limit=bucket.limit,
                    used=used,
                    reset_suffix=reset_suffix,
                )
            )
        elif bucket.remaining is not None:
            lines.append(
                t(
                    "quota.rl_bucket_remaining_only",
                    label=label,
                    remaining=bucket.remaining,
                    reset_suffix=reset_suffix,
                )
            )
        elif bucket.limit is not None:
            lines.append(
                t(
                    "quota.rl_bucket_limit_only",
                    label=label,
                    limit=bucket.limit,
                    reset_suffix=reset_suffix,
                )
            )

    captured_dt = datetime.fromtimestamp(snapshot.captured_at, tz=timezone.utc).astimezone()
    lines.append(t("quota.rl_captured_at", time=captured_dt.strftime("%Y-%m-%d %H:%M:%S")))
    return QuotaInfo(is_supported=True, extra_lines=lines)
