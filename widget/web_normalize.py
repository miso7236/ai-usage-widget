"""웹 서비스(Claude / ChatGPT / Gemini) 응답을 위젯 표준 형식으로 정규화한다.

extension/lib/normalize.js와 동일한 규칙을 Python 표준 라이브러리만으로 구현해
내장 브라우저(WebView2) 워커가 크롬 확장과 완전히 같은 데이터 형식을 만들도록 한다.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone, tzinfo

from state import clamp_percent, parse_iso

PROVIDER_NAMES = {
    "claude": "Claude",
    "chatgpt": "ChatGPT",
    "gemini": "Gemini",
}

CLAUDE_LABELS = {
    "five_hour": "5시간",
    "seven_day": "주간",
    "seven_day_sonnet": "주간 Sonnet",
    "seven_day_opus": "주간 Opus",
    "extra_usage": "추가 사용량",
}
CLAUDE_ORDER = ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus")

_REMAINING_RE = re.compile(r"(남음|남은|잔여|remaining|left)", re.IGNORECASE)
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_PATTERN = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_KO_CLOCK_RE = re.compile(r"(오전|오후)\s*(\d{1,2}):(\d{2})")
_EN_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)
_PLAIN_CLOCK_RE = re.compile(r"(?:^|[^\d:])(\d{1,2}):(\d{2})(?!\d)")
_KO_MD_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_EN_MD_FIRST_RE = re.compile(rf"\b{_MONTH_PATTERN}\s+(\d{{1,2}})(?![\d:])", re.IGNORECASE)
_EN_DAY_FIRST_RE = re.compile(rf"\b(\d{{1,2}})\s+{_MONTH_PATTERN}", re.IGNORECASE)
_REL_UNIT_RE = re.compile(
    r"(\d+)\s*(일|days?|d|시간|hours?|hrs?|hr|h|분|minutes?|mins?|min|m)(?![a-zA-Z가-힣])",
    re.IGNORECASE,
)


def round_percent(value: float) -> float:
    """소수 첫째 자리 반올림. 0.5는 올린다 (JS Math.round와 같은 결과, 42.25 → 42.3)."""
    return math.floor(value * 10 + 0.5) / 10


def _now_dt(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def to_iso(value) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0 or value != value:
            return None
        try:
            ts = value / 1000.0 if value > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        dt = parse_iso(value.strip())
        return dt.isoformat() if dt else None
    return None


def label_for_window_seconds(seconds) -> str | None:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return None
    if s != s or s <= 0:
        return None
    si = int(round(s))
    if si == 5 * 3600:
        return "5시간"
    if si == 7 * 24 * 3600:
        return "주간"
    if si == 24 * 3600:
        return "일간"
    if si == 30 * 24 * 3600:
        return "월간"
    if si % (24 * 3600) == 0:
        return f"{si // (24 * 3600)}일"
    if si % 3600 == 0:
        return f"{si // 3600}시간"
    return f"{round(si / 60)}분"


def make_result(provider: str, windows: list[dict], now: datetime | None = None) -> dict:
    return {
        "provider": provider,
        "name": PROVIDER_NAMES.get(provider, provider),
        "ok": True,
        "windows": windows,
        "error": None,
        "fetchedAt": _now_dt(now).isoformat(),
    }


def make_error(provider: str, message: str, now: datetime | None = None) -> dict:
    return {
        "provider": provider,
        "name": PROVIDER_NAMES.get(provider, provider),
        "ok": False,
        "windows": [],
        "error": str(message)[:120],
        "fetchedAt": _now_dt(now).isoformat(),
    }


# ---------------- Claude ----------------
def pick_claude_org(orgs, preferred_id: str | None = None) -> dict | None:
    if not isinstance(orgs, list) or not orgs:
        return None
    dict_orgs = [o for o in orgs if isinstance(o, dict)]
    if not dict_orgs:
        return None
    if preferred_id:
        for org in dict_orgs:
            if org.get("uuid") == preferred_id:
                return org
    for org in dict_orgs:
        caps = org.get("capabilities")
        if isinstance(caps, list) and "chat" in caps:
            return org
    return dict_orgs[0]


def normalize_claude(data, now: datetime | None = None) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Claude 응답 형식을 알 수 없어요")
    windows: list[dict] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if value.get("is_enabled") is False:
            continue
        used = clamp_percent(value.get("utilization"))
        if used is None:
            continue
        windows.append(
            {
                "key": key,
                "label": CLAUDE_LABELS.get(key, key),
                "usedPercent": round_percent(used),
                "resetsAt": to_iso(value.get("resets_at")),
                "resetText": None,
            }
        )
    windows.sort(key=lambda w: CLAUDE_ORDER.index(w["key"]) if w["key"] in CLAUDE_ORDER else len(CLAUDE_ORDER))
    if not windows:
        raise ValueError("Claude 응답에서 사용량을 찾지 못했어요 (형식이 바뀌었을 수 있어요)")
    return make_result("claude", windows, now)


# ---------------- ChatGPT ----------------
def _find_objects_with_key(root, key_name: str, path: str = "", out: list | None = None, depth: int = 0) -> list:
    if out is None:
        out = []
    if depth > 6 or not isinstance(root, (dict, list)):
        return out
    if isinstance(root, dict):
        if key_name in root:
            out.append((path or "root", root))
        for k, v in root.items():
            if isinstance(v, (dict, list)):
                _find_objects_with_key(v, key_name, f"{path}.{k}" if path else str(k), out, depth + 1)
    else:
        for i, v in enumerate(root):
            if isinstance(v, (dict, list)):
                _find_objects_with_key(v, key_name, f"{path}.{i}" if path else str(i), out, depth + 1)
    return out


def _chatgpt_window(key: str, w: dict, fallback_label: str, now_dt: datetime) -> dict | None:
    used = clamp_percent(w.get("used_percent"))
    if used is None:
        return None
    resets_at = to_iso(w.get("reset_at"))
    if not resets_at and w.get("reset_after_seconds") is not None:
        try:
            sec = float(w["reset_after_seconds"])
            if sec == sec:
                resets_at = (now_dt + timedelta(seconds=sec)).isoformat()
        except (TypeError, ValueError):
            pass
    return {
        "key": key,
        "label": label_for_window_seconds(w.get("limit_window_seconds")) or fallback_label,
        "usedPercent": round_percent(used),
        "resetsAt": resets_at,
        "resetText": None,
    }


def normalize_chatgpt(data, now: datetime | None = None) -> dict:
    if not isinstance(data, dict):
        raise ValueError("ChatGPT 응답 형식을 알 수 없어요")
    now_dt = _now_dt(now)
    windows: list[dict] = []
    rl = data.get("rate_limit")
    if isinstance(rl, dict):
        for key, fallback in (("primary_window", "5시간"), ("secondary_window", "주간")):
            w = rl.get(key)
            if isinstance(w, dict):
                win = _chatgpt_window(key, w, fallback, now_dt)
                if win:
                    windows.append(win)
    if not windows:
        for path, obj in _find_objects_with_key(data, "used_percent"):
            win = _chatgpt_window(path, obj, path, now_dt)
            if win:
                windows.append(win)
    if not windows:
        raise ValueError("ChatGPT 응답에서 사용량을 찾지 못했어요 (형식이 바뀌었을 수 있어요)")
    return make_result("chatgpt", windows, now_dt)


# ---------------- Gemini ----------------
def _parse_clock(text: str) -> tuple[int, int] | None:
    m = _KO_CLOCK_RE.search(text)
    if m:
        hour, minute = int(m.group(2)), int(m.group(3))
        if m.group(1) == "오후" and hour < 12:
            hour += 12
        if m.group(1) == "오전" and hour == 12:
            hour = 0
    else:
        m = _EN_CLOCK_RE.search(text)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            ampm = m.group(3).upper()
            if ampm == "PM" and hour < 12:
                hour += 12
            if ampm == "AM" and hour == 12:
                hour = 0
        else:
            m = _PLAIN_CLOCK_RE.search(text)
            if not m:
                return None
            hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute)


def _parse_month_day(text: str) -> tuple[int, int] | None:
    m = _KO_MD_RE.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _EN_MD_FIRST_RE.search(text)
    if m:
        return (_MONTHS[m.group(1).lower()], int(m.group(2)))
    m = _EN_DAY_FIRST_RE.search(text)
    if m:
        return (_MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def parse_gemini_reset_text(
    reset_text: str | None, now: datetime | None = None, local_tz: tzinfo | None = None
) -> str | None:
    """Gemini 화면의 리셋 문구를 ISO 시각으로 바꾼다.

    "오후 3:00", "11월 24일" 같은 시각·날짜는 화면에 보이는 그대로 PC의 시간대로 해석한다
    (크롬 확장의 normalize.js와 같음). local_tz를 주면 그 시간대를 쓴다 (테스트용).
    """
    if not reset_text or not isinstance(reset_text, str):
        return None
    text = reset_text.strip()
    if not text:
        return None
    now_dt = _now_dt(now)
    base_local = now_dt.astimezone(local_tz) if local_tz is not None else now_dt.astimezone()
    local_tz = base_local.tzinfo

    md = _parse_month_day(text)
    if md:
        month, day = md
        hour, minute = _parse_clock(text) or (0, 0)
        try:
            target = datetime(base_local.year, month, day, hour, minute, tzinfo=local_tz)
        except ValueError:
            return None
        if target < base_local - timedelta(hours=24):
            try:
                target = datetime(base_local.year + 1, month, day, hour, minute, tzinfo=local_tz)
            except ValueError:
                return None
        return target.astimezone(timezone.utc).isoformat()

    if re.search(r"(마다|every)", text, re.IGNORECASE):
        return None

    if ":" not in text:
        total_sec = 0
        for m in _REL_UNIT_RE.finditer(text):
            val = int(m.group(1))
            unit = m.group(2).lower()
            if unit == "일" or unit.startswith("day") or unit == "d":
                total_sec += val * 86400
            elif unit == "시간" or unit.startswith("hour") or unit.startswith("hr") or unit == "h":
                total_sec += val * 3600
            else:
                total_sec += val * 60
        if total_sec > 0:
            return (now_dt + timedelta(seconds=total_sec)).astimezone(timezone.utc).isoformat()

    clock = _parse_clock(text)
    if clock:
        hour, minute = clock
        target = base_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= base_local:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc).isoformat()

    return None


def shorten_gemini_label(label: str) -> str:
    if re.search(r"5\s*(시간|hour|hr)", label, re.IGNORECASE):
        return "5시간"
    if re.search(r"(주간|week)", label, re.IGNORECASE):
        return "주간"
    if re.search(r"(일간|daily|day)", label, re.IGNORECASE):
        return "일간"
    return label[:14] + "…" if len(label) > 14 else label


def normalize_gemini(items, now: datetime | None = None, local_tz: tzinfo | None = None) -> dict:
    if not isinstance(items, list) or not items:
        raise ValueError("Gemini 사용량 화면에서 숫자를 찾지 못했어요")
    now_dt = _now_dt(now)
    windows: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        p = clamp_percent(item.get("percent"))
        if p is None:
            continue
        label = str(item.get("label") or "").strip() or f"항목 {i + 1}"
        used = clamp_percent(100.0 - p) if (_REMAINING_RE.search(label) or item.get("isRemaining")) else p
        if used is None:
            continue
        dedupe_key = f"{label}|{used}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        reset_text = str(item["resetText"])[:60] if item.get("resetText") else None
        resets_at = to_iso(item.get("resetsAt")) or parse_gemini_reset_text(reset_text, now_dt, local_tz)
        windows.append(
            {
                "key": str(item.get("key") or f"item{i}"),
                "label": shorten_gemini_label(label),
                "usedPercent": round_percent(used),
                "resetsAt": resets_at,
                "resetText": reset_text,
            }
        )
    if not windows:
        raise ValueError("Gemini 사용량 화면에서 숫자를 찾지 못했어요")
    return make_result("gemini", windows, now_dt)
