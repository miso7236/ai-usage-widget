"""위젯이 표시할 데이터와 표시용 계산(남은 시간, 색상 등).

tkinter에 의존하지 않아서 리눅스나 CI에서도 테스트할 수 있다.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROVIDER_ORDER = ["claude", "chatgpt", "gemini", "antigravity"]
STALE_AFTER = timedelta(minutes=15)
KST = timezone(timedelta(hours=9))


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def clamp_percent(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return max(0.0, min(100.0, n))


@dataclass
class UsageWindow:
    label: str
    used_percent: float
    resets_at: datetime | None = None
    reset_text: str | None = None
    eta_at: datetime | None = None


@dataclass
class ProviderState:
    provider: str
    name: str
    ok: bool
    windows: list[UsageWindow] = field(default_factory=list)
    error: str | None = None
    fetched_at: datetime | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderState":
        if not isinstance(d, dict) or not isinstance(d.get("provider"), str):
            raise ValueError("provider 항목이 없어요")
        windows = []
        for w in d.get("windows") or []:
            if not isinstance(w, dict):
                continue
            used = clamp_percent(w.get("usedPercent"))
            if used is None:
                continue
            windows.append(
                UsageWindow(
                    label=str(w.get("label") or "")[:20],
                    used_percent=used,
                    resets_at=parse_iso(w.get("resetsAt")),
                    reset_text=(str(w["resetText"])[:60] if w.get("resetText") else None),
                    eta_at=parse_iso(w.get("etaAt")),
                )
            )
        return cls(
            provider=d["provider"][:20],
            name=str(d.get("name") or d["provider"])[:20],
            ok=bool(d.get("ok")),
            windows=windows,
            error=(str(d["error"])[:120] if d.get("error") else None),
            fetched_at=parse_iso(d.get("fetchedAt")),
        )

    def is_stale(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self.fetched_at is None:
            return True
        return now - self.fetched_at > STALE_AFTER


def level(used_percent: float) -> str:
    """색상 단계: ok(초록) / warn(주황) / danger(빨강)"""
    if used_percent >= 90:
        return "danger"
    if used_percent >= 70:
        return "warn"
    return "ok"


def format_reset(window: UsageWindow, now: datetime | None = None) -> str:
    """'2시간 13분 후' 같은 남은 시간. 시간을 모르면 화면에서 읽은 글자를 쓴다."""
    if window.resets_at is not None:
        now = now or datetime.now(timezone.utc)
        seconds = (window.resets_at - now).total_seconds()
        if seconds <= 0:
            return "곧 리셋"
        minutes = int(seconds // 60)
        days, rem = divmod(minutes, 1440)
        hours, mins = divmod(rem, 60)
        if days > 0:
            text = f"{days}일 {hours}시간"
        elif hours > 0:
            text = f"{hours}시간 {mins}분"
        else:
            text = f"{max(mins, 1)}분"
        return f"{text} 후"
    return window.reset_text or ""


def format_reset_compact(window: UsageWindow, now: datetime | None = None) -> str:
    """위젯 가로 폭을 줄이기 위한 짧은 남은 시간 ('2h13m', '3d4h', '25m')."""
    if window.resets_at is not None:
        now = now or datetime.now(timezone.utc)
        seconds = (window.resets_at - now).total_seconds()
        if seconds <= 0:
            return "곧"
        minutes = int(seconds // 60)
        days, rem = divmod(minutes, 1440)
        hours, mins = divmod(rem, 60)
        if days > 0:
            return f"{days}d{hours}h" if hours else f"{days}d"
        if hours > 0:
            return f"{hours}h{mins}m" if mins else f"{hours}h"
        return f"{max(mins, 1)}m"
    return window.reset_text or ""


def format_eta(window: UsageWindow, now: datetime | None = None) -> str:
    """현재 소진 속도 기준 예상 한도 도달 문구 ('약 1시간 20분 후 소진')."""
    if window.eta_at is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if window.resets_at is not None and window.eta_at >= window.resets_at:
        return ""
    seconds = (window.eta_at - now).total_seconds()
    if seconds <= 0:
        return ""
    minutes = max(1, int(seconds // 60))
    hours, mins = divmod(minutes, 60)
    if hours > 0:
        return f"약 {hours}시간 {mins}분 후 소진"
    return f"약 {minutes}분 후 소진"


COMPACT_NAMES = {
    "claude": "C",
    "chatgpt": "GPT",
    "gemini": "Gem",
    "antigravity": "AG",
}


def format_clock(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(KST).strftime("%H:%M")


_SHORT_WINDOW_RE = re.compile(
    r"(5\s*시간|\d+\s*h\b|\d+\s*시간|현재|세션|session|일간|daily|hour|분)", re.IGNORECASE
)
_LONG_WINDOW_RE = re.compile(r"(주간|week|월간|month|\d+\s*일)", re.IGNORECASE)
SHORT_WINDOW_MAX = timedelta(hours=5, minutes=10)


def is_short_window(window: UsageWindow, now: datetime | None = None) -> bool:
    """5시간·세션·'현재 사용량'처럼 짧은 주기 윈도우인지 판단한다.

    라벨이 우선이고, 라벨로 알 수 없으면 리셋까지 남은 시간(5시간 이내)으로 추정한다.
    """
    label = window.label or ""
    if _LONG_WINDOW_RE.search(label) and not re.search(r"5\s*시간|5\s*h\b", label, re.IGNORECASE):
        return False
    if _SHORT_WINDOW_RE.search(label):
        return True
    if window.resets_at is not None:
        now = now or datetime.now(timezone.utc)
        return window.resets_at - now <= SHORT_WINDOW_MAX
    return False


_FIVE_HOUR_RE = re.compile(r"(5\s*시간|5\s*h\b)", re.IGNORECASE)
_WEEKLY_RE = re.compile(r"(주간|week|7\s*일|7\s*d\b)", re.IGNORECASE)
_DAILY_RE = re.compile(r"(일간|daily|24\s*시간|1\s*일)", re.IGNORECASE)


def window_duration(window: UsageWindow) -> timedelta | None:
    """라벨에서 윈도우 전체 주기(5시간 / 주간 7일 / 일간 24시간)를 추정한다."""
    label = window.label or ""
    if _FIVE_HOUR_RE.search(label):
        return timedelta(hours=5)
    if _WEEKLY_RE.search(label):
        return timedelta(days=7)
    if _DAILY_RE.search(label):
        return timedelta(days=1)
    return None


def window_pace_percent(window: UsageWindow, now: datetime | None = None) -> float | None:
    """경과 시간 기준 적정 사용량(페이스 기준값, 0~100%)을 계산한다.

    예: 주간 리셋까지 3.5일 남았다면 절반이 지났으므로 50.0%를 반환한다.
    주기나 리셋 시각을 알 수 없으면 None.
    """
    if window.resets_at is None:
        return None
    duration = window_duration(window)
    if duration is None:
        return None
    now = now or datetime.now(timezone.utc)
    total = duration.total_seconds()
    remaining = (window.resets_at - now).total_seconds()
    if remaining < -60 or remaining > total + 60:
        return None
    elapsed_pct = (1.0 - max(0.0, min(total, remaining)) / total) * 100.0
    return round(max(0.0, min(100.0, elapsed_pct)), 1)


def pace_level(used_percent: float, pace_percent: float) -> str:
    """경과 기준값 대비 사용 속도 색상 키: ok(여유) / muted(적정) / warn(빠름) / danger(매우 빠름)."""
    delta = used_percent - pace_percent
    if delta >= 25:
        return "danger"
    if delta >= 10:
        return "warn"
    if delta <= -10:
        return "ok"
    return "muted"


def _compact_item(
    short: str,
    windows: list[UsageWindow],
    dim: bool,
    now: datetime | None = None,
    show_pace: bool = True,
) -> tuple[str, str, str]:
    peak_win = max(windows, key=lambda w: w.used_percent)
    pace = window_pace_percent(peak_win, now) if show_pace else None
    text = f"{round(peak_win.used_percent)}/{round(pace)}%" if pace is not None else f"{round(peak_win.used_percent)}%"
    return (short, text, "muted" if dim else level(peak_win.used_percent))


def compact_summary(
    providers: list[ProviderState],
    now: datetime | None = None,
    show_pace: bool = True,
) -> list[tuple[str, str, str]]:
    """컴팩트 모드 첫 줄(주간 등 긴 주기) 요약: (짧은 이름, 표시값, 색상 키) 목록.

    짧은 주기(5시간·현재 사용량) 값은 둘째 줄에만 보여 주고, 여기에는 섞지 않는다.
    """
    items: list[tuple[str, str, str]] = []
    for p in providers:
        short = COMPACT_NAMES.get(p.provider, p.name[:4])
        dim = p.is_stale(now) or not p.ok
        long_windows = [w for w in p.windows if not is_short_window(w, now)]
        if long_windows:
            items.append(_compact_item(short, long_windows, dim, now=now, show_pace=show_pace))
        elif p.windows:
            items.append((short, "-", "muted"))
        elif p.error:
            items.append((short, "!", "error"))
        else:
            items.append((short, "-", "muted"))
    return items


def compact_five_hour_summary(
    providers: list[ProviderState],
    now: datetime | None = None,
    show_pace: bool = True,
) -> list[tuple[str, str, str]]:
    """컴팩트 모드 둘째 줄용 5시간(짧은 주기) 사용량 요약: (짧은 이름, 표시값, 색상 키) 목록."""
    items: list[tuple[str, str, str]] = []
    found_any = False
    for p in providers:
        short = COMPACT_NAMES.get(p.provider, p.name[:4])
        dim = p.is_stale(now) or not p.ok
        short_windows = [w for w in p.windows if is_short_window(w, now)]
        if short_windows:
            found_any = True
            items.append(_compact_item(short, short_windows, dim, now=now, show_pace=show_pace))
        else:
            items.append((short, "-", "muted"))
    return items if found_any else []


class Store:
    """HTTP 서버 스레드와 화면 스레드가 함께 쓰는 저장소."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refresh_cond = threading.Condition()
        self._refresh_seq = 0
        self._web_source: str = "extension"  # "extension" | "browser"
        self._providers: dict[str, ProviderState] = {}
        self._browser_providers: dict[str, ProviderState] = {}
        self._local_providers: dict[str, ProviderState] = {}
        self.version = 0
        self.received_at: datetime | None = None

    @property
    def refresh_seq(self) -> int:
        with self._refresh_cond:
            return self._refresh_seq

    def request_refresh(self) -> int:
        """위젯에서 즉시 새로고침을 요청하면 대기 중인 확장을 깨운다."""
        with self._refresh_cond:
            self._refresh_seq += 1
            seq = self._refresh_seq
            self._refresh_cond.notify_all()
            return seq

    def wait_for_refresh(self, after_seq: int, timeout: float = 20.0) -> tuple[bool, int]:
        """after_seq 이후 새 새로고침 요청이 올 때까지 최대 timeout초 대기한다."""
        with self._refresh_cond:
            if self._refresh_seq > after_seq:
                return True, self._refresh_seq
            self._refresh_cond.wait_for(lambda: self._refresh_seq > after_seq, timeout=max(0.0, timeout))
            return self._refresh_seq > after_seq, self._refresh_seq

    def set_web_source(self, source: str) -> None:
        normalized = "browser" if source == "browser" else "extension"
        with self._lock:
            if self._web_source != normalized:
                self._web_source = normalized
                self.version += 1

    def update(self, payload: dict) -> int:
        if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
            raise ValueError("providers 목록이 필요해요")
        parsed = {}
        for item in payload["providers"]:
            state = ProviderState.from_dict(item)
            parsed[state.provider] = state
        with self._lock:
            # 확장이 보낸 목록이 전체 상태이므로 통째로 바꾼다 (꺼진 서비스는 사라짐).
            self._providers = parsed
            if self._web_source == "extension":
                self.version += 1
                self.received_at = datetime.now(timezone.utc)
            return len(parsed)

    def update_browser_providers(self, items: list[dict]) -> int:
        """내장 브라우저(WebView2) 워커가 수집한 결과를 반영한다."""
        parsed: dict[str, ProviderState] = {}
        for item in items:
            state = ProviderState.from_dict(item)
            parsed[state.provider] = state
        with self._lock:
            for key, state in parsed.items():
                prev = self._browser_providers.get(key)
                if not state.ok and not state.windows and prev is not None and prev.windows:
                    # 일시 오류 시 마지막으로 알던 막대/숫자는 흐리게 유지한다.
                    state = ProviderState(
                        provider=state.provider,
                        name=state.name,
                        ok=False,
                        windows=list(prev.windows),
                        error=state.error,
                        fetched_at=prev.fetched_at or state.fetched_at,
                    )
                self._browser_providers[key] = state
            if self._web_source == "browser":
                self.version += 1
                self.received_at = datetime.now(timezone.utc)
            return len(parsed)

    def set_local_provider(self, state: ProviderState) -> None:
        """위젯이 직접 조회한 로컬 프로바이더(예: Antigravity) 상태를 저장한다."""
        with self._lock:
            self._local_providers[state.provider] = state
            self.version += 1
            if state.ok or self.received_at is None:
                self.received_at = datetime.now(timezone.utc)

    def remove_local_provider(self, provider: str) -> None:
        with self._lock:
            if provider in self._local_providers:
                del self._local_providers[provider]
                self.version += 1

    def snapshot(self) -> list[ProviderState]:
        with self._lock:
            base = self._browser_providers if self._web_source == "browser" else self._providers
            merged = dict(base)
            merged.update(self._local_providers)
            items = list(merged.values())
        return sorted(
            items,
            key=lambda s: PROVIDER_ORDER.index(s.provider) if s.provider in PROVIDER_ORDER else 99,
        )


def demo_payload(now: datetime | None = None) -> dict:
    """확장 없이 화면을 확인할 때 쓰는 예시 데이터 (python widget.py --demo)."""
    now = now or datetime.now(timezone.utc)
    iso = lambda dt: dt.isoformat()
    return {
        "providers": [
            {
                "provider": "claude", "name": "Claude", "ok": True, "fetchedAt": iso(now),
                "windows": [
                    {"label": "5시간", "usedPercent": 42, "resetsAt": iso(now + timedelta(hours=2, minutes=13))},
                    {"label": "주간", "usedPercent": 71, "resetsAt": iso(now + timedelta(days=3, hours=4))},
                ],
            },
            {
                "provider": "chatgpt", "name": "ChatGPT", "ok": True, "fetchedAt": iso(now),
                "windows": [
                    {"label": "5시간", "usedPercent": 12, "resetsAt": iso(now + timedelta(hours=4))},
                    {"label": "주간", "usedPercent": 93, "resetsAt": iso(now + timedelta(days=1))},
                ],
            },
            {
                "provider": "gemini", "name": "Gemini", "ok": False, "fetchedAt": iso(now),
                "error": "Gemini 사용량 화면을 읽지 못했어요",
                "windows": [{"label": "5시간", "usedPercent": 30, "resetText": "오후 3:00에 재설정"}],
            },
        ]
    }


# ---------------- 다중 모니터 위치 보정 ----------------
Rect = tuple[int, int, int, int]  # (left, top, right, bottom)


def _intersection_area(a: Rect, b: Rect) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def point_in_rect(px: int, py: int, rect: Rect, pad: int = 2) -> bool:
    """좌표 (px, py)가 여유 여백(pad)을 포함한 사각형 rect 안에 있는지 확인한다."""
    left, top, right, bottom = rect
    return (left - pad) <= px <= (right + pad) and (top - pad) <= py <= (bottom + pad)


POSITION_PRESETS: tuple[tuple[str, str], ...] = (
    ("top_center", "중앙 최상단"),
    ("top_left", "왼쪽 상단"),
    ("top_right", "오른쪽 상단"),
    ("bottom_center", "중앙 하단"),
    ("bottom_left", "왼쪽 하단"),
    ("bottom_right", "오른쪽 하단 (기본)"),
)


def preset_position(preset: str, win_w: int, win_h: int, monitor: Rect) -> tuple[int, int]:
    """모니터 사각형(monitor) 기준 추천 프리셋 좌표를 계산한다."""
    left, top, right, bottom = monitor
    center_x = left + (right - left - win_w) // 2
    if preset == "top_center":
        x, y = center_x, top + 6
    elif preset == "top_left":
        x, y = left + 16, top + 12
    elif preset == "top_right":
        x, y = right - win_w - 16, top + 12
    elif preset == "bottom_center":
        x, y = center_x, bottom - win_h - 60
    elif preset == "bottom_left":
        x, y = left + 20, bottom - win_h - 60
    else:  # "bottom_right" 및 기본값
        x, y = right - win_w - 20, bottom - win_h - 60
    return (
        max(left, min(x, right - win_w)),
        max(top, min(y, bottom - win_h)),
    )


def default_position(win_w: int, win_h: int, primary: Rect) -> tuple[int, int]:
    """주 모니터 오른쪽 아래(작업 표시줄 위) 기본 위치."""
    return preset_position("bottom_right", win_w, win_h, primary)


def current_monitor_rect(
    x: int | None,
    y: int | None,
    win_w: int,
    win_h: int,
    monitors: list[Rect],
    primary: Rect,
) -> Rect | None:
    """창이 현재 속해 있는 모니터 사각형을 찾는다. 어느 모니터와도 겹치지 않으면 None."""
    if x is None or y is None:
        return None
    ix, iy = int(x), int(y)
    rects = monitors or [primary]
    cx, cy = ix + win_w // 2, iy + win_h // 2

    for r in rects:
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            return r

    win_rect: Rect = (ix, iy, ix + win_w, iy + win_h)
    best: Rect | None = None
    best_area = 0
    for r in rects:
        area = _intersection_area(win_rect, r)
        if area > best_area:
            best_area = area
            best = r
    return best


def clamp_position(
    x: int | None,
    y: int | None,
    win_w: int,
    win_h: int,
    monitors: list[Rect],
    primary: Rect,
    margin_x: int = 10,
    margin_y: int = 10,
) -> tuple[int, int]:
    """저장된(또는 현재) 창 좌표를 속한 모니터 안쪽으로 보정한다.

    보조 모니터(음수 좌표 또는 주 모니터 바깥)를 유지하며,
    모니터가 분리되어 어느 모니터와도 겹치지 않을 때만 주 모니터 기본 위치로 복귀한다.
    """
    target = current_monitor_rect(x, y, win_w, win_h, monitors, primary)
    if target is None or x is None or y is None:
        return default_position(win_w, win_h, primary)

    ix, iy = int(x), int(y)
    left, top, right, bottom = target
    max_x = max(left, right - win_w - margin_x)
    max_y = max(top, bottom - win_h - margin_y)
    return (
        max(left, min(ix, max_x)),
        max(top, min(iy, max_y)),
    )


# ---------------- 크기 조절 (비율 스케일) ----------------
MIN_SCALE = 0.6
MAX_SCALE = 2.5


def clamp_scale(value) -> float:
    try:
        s = float(value)
    except (TypeError, ValueError):
        return 1.0
    if s != s:  # NaN
        return 1.0
    return round(max(MIN_SCALE, min(MAX_SCALE, s)), 2)


def scaled_metrics(scale: float) -> dict[str, int]:
    """현재 배율에 맞춘 글꼴·막대·여백 치수를 계산한다."""
    s = clamp_scale(scale)
    return {
        "font_main": max(6, round(8 * s)),
        "font_small": max(6, round(7 * s)),
        "bar_w": max(32, round(54 * s)),
        "bar_h": max(3, round(5 * s)),
        "pad_x": max(2, round(4 * s)),
        "pad_y": max(1, round(3 * s)),
        "compact_pad_x": max(2, round(3 * s)),
        "compact_pad_y": max(1, round(2 * s)),
        "wrap_w": max(120, round(180 * s)),
    }


def scale_from_drag(start_scale: float, start_w: int, start_h: int, dx: int, dy: int) -> float:
    """오른쪽 아래 모서리 드래그 이동량(dx, dy)을 새 배율로 변환한다."""
    base_w = max(40, int(start_w))
    base_h = max(20, int(start_h))
    ratio_x = (base_w + dx) / base_w
    ratio_y = (base_h + dy) / base_h
    ratio = (ratio_x + ratio_y) / 2.0
    return clamp_scale(start_scale * ratio)


# ---------------- 글꼴 선택 (작은 크기 가독성 우선) ----------------
AUTO_FONT_PRIORITY = (
    "Pretendard",
    "SUIT",
    "Segoe UI Variable Small",
    "Segoe UI",
    "Malgun Gothic",
)
FONT_PRESETS = (
    ("auto", "자동 (가독성 우선)"),
    ("Segoe UI", "Segoe UI"),
    ("Dotum", "돋움 (작은 글씨 선명)"),
    ("Malgun Gothic", "맑은 고딕"),
    ("Consolas", "Consolas (고정폭)"),
)


def pick_font_family(preference: str | None, available: list[str] | set[str]) -> str:
    """설치된 글꼴 목록에서 선호 글꼴(또는 가독성 우선 글꼴)을 고른다."""
    by_lower = {name.lower(): name for name in available if isinstance(name, str)}
    pref = (preference or "auto").strip()
    if pref and pref.lower() != "auto":
        found = by_lower.get(pref.lower())
        if found:
            return found
    for candidate in AUTO_FONT_PRIORITY:
        found = by_lower.get(candidate.lower())
        if found:
            return found
    return "Malgun Gothic"


# ---------------- 설정 파일 (창 위치, 투명도 등) ----------------
DEFAULT_CONFIG = {
    "x": None,
    "y": None,
    "alpha": 0.92,
    "topmost": True,
    "compact": False,
    "transparent_bg": True,
    "scale": 1.0,
    "compact_scale": 1.0,
    "font": "auto",
    "port": 47823,
    "auto_update_check": True,
    "show_antigravity": True,
    "show_pace": True,
    "web_source": "extension",  # "extension" (크롬 확장) | "browser" (내장 WebView2)
    "browser_interval_min": 3,
    "claude_source": "web",  # "web" (크롬 확장 / 내장 브라우저) | "oauth" (로컬 Hermes / Claude Code)
    "claude_oauth_path": "",
}


def config_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) / "AIUsageWidget" if base else Path.home() / ".ai-usage-widget"
    return root / "config.json"


def load_config(path: Path | None = None) -> dict:
    path = path or config_path()
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict, path: Path | None = None) -> None:
    path = path or config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
