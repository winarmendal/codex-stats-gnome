#!/usr/bin/env python
"""Local Codex usage aggregator for the Codex Stats GNOME extension."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_LOG_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_CACHE_FILE = Path.home() / ".cache" / "codex-stats" / "cache.json"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TokenEvent:
    ts: float
    total_tokens: int
    primary_used: float | None = None
    primary_window: int | None = None
    primary_resets_at: float | None = None
    secondary_used: float | None = None
    secondary_window: int | None = None
    secondary_resets_at: float | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate local Codex token usage")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    parser.add_argument("--log-root", default=str(DEFAULT_LOG_ROOT), help="Codex sessions directory")
    parser.add_argument("--cache-file", default=str(DEFAULT_CACHE_FILE), help="cache JSON path")
    parser.add_argument("--no-cache", action="store_true", help="disable cache reads and writes")
    parser.add_argument("--now", default="", help="override current time as ISO-8601, for tests")
    return parser.parse_args()


def parse_datetime(value: str, local_tz: timezone) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def parse_now(value: str) -> datetime:
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    if not value:
        return datetime.now(local_tz)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"Invalid --now value: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed


def iter_log_files(log_root: Path) -> list[Path]:
    if not log_root.exists():
        return []
    return sorted(path for path in log_root.rglob("*.jsonl") if path.is_file())


def load_cache(cache_file: Path) -> dict[str, Any]:
    try:
        with cache_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "files": {}}
    if payload.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": SCHEMA_VERSION, "files": {}}
    if not isinstance(payload.get("files"), dict):
        return {"schema_version": SCHEMA_VERSION, "files": {}}
    return payload


def save_cache(cache_file: Path, cache: dict[str, Any]) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
        with tmp_file.open("w", encoding="utf-8") as handle:
            json.dump(cache, handle, separators=(",", ":"))
        os.replace(tmp_file, cache_file)
    except OSError:
        pass


def extract_event(payload: dict[str, Any], local_tz: timezone) -> TokenEvent | None:
    if payload.get("type") != "event_msg":
        return None
    event_payload = payload.get("payload")
    if not isinstance(event_payload, dict) or event_payload.get("type") != "token_count":
        return None

    timestamp = parse_datetime(str(payload.get("timestamp", "")), local_tz)
    if timestamp is None:
        return None

    info = event_payload.get("info") if isinstance(event_payload.get("info"), dict) else {}
    last_usage = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
    try:
        total_tokens = int(last_usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        total_tokens = 0

    rate_limits = event_payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        rate_limits = {}

    primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else {}

    return TokenEvent(
        ts=timestamp.timestamp(),
        total_tokens=max(0, total_tokens),
        primary_used=number_or_none(primary.get("used_percent")),
        primary_window=int_or_none(primary.get("window_minutes")),
        primary_resets_at=number_or_none(primary.get("resets_at")),
        secondary_used=number_or_none(secondary.get("used_percent")),
        secondary_window=int_or_none(secondary.get("window_minutes")),
        secondary_resets_at=number_or_none(secondary.get("resets_at")),
    )


def number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_file(path: Path, local_tz: timezone) -> tuple[list[TokenEvent], int]:
    events: list[TokenEvent] = []
    malformed = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(payload, dict):
                    continue
                event = extract_event(payload, local_tz)
                if event is not None:
                    events.append(event)
    except OSError:
        malformed += 1
    return events, malformed


def collect_events(log_root: Path, cache_file: Path, use_cache: bool, local_tz: timezone) -> tuple[list[TokenEvent], dict[str, int]]:
    stats = {"files_scanned": 0, "files_parsed": 0, "malformed_lines": 0}
    files = iter_log_files(log_root)
    stats["files_scanned"] = len(files)

    cache = load_cache(cache_file) if use_cache else {"schema_version": SCHEMA_VERSION, "files": {}}
    cached_files = cache.setdefault("files", {})
    seen_paths: set[str] = set()
    events: list[TokenEvent] = []

    for path in files:
        key = str(path)
        seen_paths.add(key)
        try:
            stat = path.stat()
        except OSError:
            continue

        cached = cached_files.get(key) if isinstance(cached_files.get(key), dict) else None
        if (
            use_cache
            and cached
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("size") == stat.st_size
        ):
            events.extend(TokenEvent(**event) for event in cached.get("events", []))
            stats["malformed_lines"] += int(cached.get("malformed_lines", 0) or 0)
            continue

        parsed_events, malformed = parse_file(path, local_tz)
        events.extend(parsed_events)
        stats["files_parsed"] += 1
        stats["malformed_lines"] += malformed
        cached_files[key] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "malformed_lines": malformed,
            "events": [asdict(event) for event in parsed_events],
        }

    for key in list(cached_files.keys()):
        if key not in seen_paths:
            del cached_files[key]

    if use_cache:
        save_cache(cache_file, cache)

    return events, stats


def start_of_month(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(value: datetime, delta: int) -> datetime:
    month_index = (value.year * 12 + value.month - 1) + delta
    year = month_index // 12
    month = month_index % 12 + 1
    return value.replace(year=year, month=month, day=1)


def fmt_month(value: datetime) -> str:
    return value.strftime("%b")


def bucket_label_for_window(window_minutes: int | None) -> str:
    if window_minutes == 300:
        return "5h"
    if window_minutes == 10080:
        return "Week"
    if not window_minutes:
        return "--"
    if window_minutes < 60:
        return f"{window_minutes}m"
    if window_minutes < 1440:
        hours = round(window_minutes / 60)
        return f"{hours}h"
    days = round(window_minutes / 1440)
    return f"{days}d"


def limit_payload(prefix: str, event: TokenEvent | None, local_tz: timezone) -> dict[str, Any]:
    if event is None:
        return {"label": "--", "remaining_percent": None, "used_percent": None, "resets_at": None}

    used = getattr(event, f"{prefix}_used")
    window = getattr(event, f"{prefix}_window")
    resets_at = getattr(event, f"{prefix}_resets_at")
    if used is None:
        return {"label": bucket_label_for_window(window), "remaining_percent": None, "used_percent": None, "resets_at": None}

    used = max(0.0, min(100.0, used))
    reset_iso = None
    if resets_at:
        reset_iso = datetime.fromtimestamp(resets_at, tz=local_tz).isoformat()

    return {
        "label": bucket_label_for_window(window),
        "remaining_percent": round(max(0.0, 100.0 - used), 1),
        "used_percent": round(used, 1),
        "resets_at": reset_iso,
    }


def aggregate(events: list[TokenEvent], now: datetime, stats: dict[str, int], log_root: Path) -> dict[str, Any]:
    local_tz = now.tzinfo or timezone.utc
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_date = (now.date() - timedelta(days=6))
    month_start = start_of_month(now)
    three_month_start = add_months(month_start, -2)

    hourly = [0 for _ in range(24)]
    week_buckets = {week_start_date + timedelta(days=i): 0 for i in range(7)}
    month_days = [month_start.date() + timedelta(days=i) for i in range((now.date() - month_start.date()).days + 1)]
    month_buckets = {day: 0 for day in month_days}
    three_month_buckets: dict[str, int] = {}
    for i in range(3):
        month = add_months(three_month_start, i)
        three_month_buckets[month.strftime("%Y-%m")] = 0

    latest_limit_event: TokenEvent | None = None

    for event in events:
        event_dt = datetime.fromtimestamp(event.ts, tz=local_tz)
        if event_dt > now:
            continue

        if latest_limit_event is None or event.ts > latest_limit_event.ts:
            if event.primary_used is not None or event.secondary_used is not None:
                latest_limit_event = event

        if day_start <= event_dt <= now:
            hourly[event_dt.hour] += event.total_tokens

        event_date = event_dt.date()
        if event_date in week_buckets:
            week_buckets[event_date] += event.total_tokens
        if event_date in month_buckets:
            month_buckets[event_date] += event.total_tokens

        if three_month_start <= event_dt <= now:
            key = event_dt.strftime("%Y-%m")
            if key in three_month_buckets:
                three_month_buckets[key] += event.total_tokens

    message = ""
    ok = log_root.exists()
    if not ok:
        message = f"Log root not found: {log_root}"
    elif stats["malformed_lines"]:
        message = f"Skipped {stats['malformed_lines']} malformed JSONL line(s)"

    return {
        "generated_at": now.isoformat(),
        "status": {
            "ok": ok,
            "message": message,
            "files_scanned": stats["files_scanned"],
            "files_parsed": stats["files_parsed"],
            "malformed_lines": stats["malformed_lines"],
        },
        "today": {
            "total_tokens": sum(hourly),
            "hourly": hourly,
        },
        "limits": {
            "primary": limit_payload("primary", latest_limit_event, local_tz),
            "secondary": limit_payload("secondary", latest_limit_event, local_tz),
        },
        "history": {
            "week": [
                {"date": day.isoformat(), "label": day.strftime("%a"), "total_tokens": tokens}
                for day, tokens in week_buckets.items()
            ],
            "month": [
                {"date": day.isoformat(), "label": str(day.day), "total_tokens": tokens}
                for day, tokens in month_buckets.items()
            ],
            "three_months": [
                {
                    "month": key,
                    "label": fmt_month(datetime.strptime(key, "%Y-%m").replace(tzinfo=local_tz)),
                    "total_tokens": tokens,
                }
                for key, tokens in three_month_buckets.items()
            ],
        },
    }


def build_payload(log_root: Path, cache_file: Path, use_cache: bool, now: datetime) -> dict[str, Any]:
    local_tz = now.tzinfo or timezone.utc
    events, stats = collect_events(log_root, cache_file, use_cache, local_tz)
    return aggregate(events, now, stats, log_root)


def main() -> int:
    args = parse_args()
    now = parse_now(args.now)
    payload = build_payload(Path(args.log_root).expanduser(), Path(args.cache_file).expanduser(), not args.no_cache, now)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
