"""위젯이 표시할 데이터와 표시용 계산(남은 시간, 색상 등).

tkinter에 의존하지 않아서 리눅스나 CI에서도 테스트할 수 있다.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROVIDER_ORDER = ["claude", "chatgpt", "gemini"]
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
}


def format_clock(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    return dt.astimezone(KST).strftime("%H:%M")


def compact_summary(
    providers: list[ProviderState], now: datetime | None = None
) -> list[tuple[str, str, str]]:
    """컴팩트 모드 한 줄 요약: (짧은 이름, 표시값, 색상 키) 목록."""
    items: list[tuple[str, str, str]] = []
    for p in providers:
        short = COMPACT_NAMES.get(p.provider, p.name[:4])
        dim = p.is_stale(now) or not p.ok
        if p.windows:
            peak = max(w.used_percent for w in p.windows)
            color = "muted" if dim else level(peak)
            items.append((short, f"{round(peak)}%", color))
        elif p.error:
            items.append((short, "!", "error"))
        else:
            items.append((short, "-", "muted"))
    return items


def compact_five_hour_summary(
    providers: list[ProviderState], now: datetime | None = None
) -> list[tuple[str, str, str]]:
    """컴팩트 모드 둘째 줄용 5시간 사용량 요약: (짧은 이름, 표시값, 색상 키) 목록."""
    items: list[tuple[str, str, str]] = []
    found_any = False
    for p in providers:
        short = COMPACT_NAMES.get(p.provider, p.name[:4])
        dim = p.is_stale(now) or not p.ok
        win = next(
            (w for w in p.windows if "5시간" in w.label or "5h" in w.label.lower()),
            None,
        )
        if win is not None:
            found_any = True
            color = "muted" if dim else level(win.used_percent)
            items.append((short, f"{round(win.used_percent)}%", color))
        else:
            items.append((short, "-", "muted"))
    return items if found_any else []


class Store:
    """HTTP 서버 스레드와 화면 스레드가 함께 쓰는 저장소."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refresh_cond = threading.Condition()
        self._refresh_seq = 0
        self._providers: dict[str, ProviderState] = {}
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
            self.version += 1
            self.received_at = datetime.now(timezone.utc)
            return len(parsed)

    def snapshot(self) -> list[ProviderState]:
        with self._lock:
            items = list(self._providers.values())
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


def default_position(win_w: int, win_h: int, primary: Rect) -> tuple[int, int]:
    """주 모니터 오른쪽 아래(작업 표시줄 위) 기본 위치."""
    left, top, right, bottom = primary
    x = right - win_w - 20
    y = bottom - win_h - 60
    return (
        max(left, min(x, right - win_w)),
        max(top, min(y, bottom - win_h)),
    )


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
    if x is None or y is None:
        return default_position(win_w, win_h, primary)

    ix, iy = int(x), int(y)
    rects = monitors or [primary]
    cx, cy = ix + win_w // 2, iy + win_h // 2

    target: Rect | None = None
    for r in rects:
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            target = r
            break

    if target is None:
        win_rect: Rect = (ix, iy, ix + win_w, iy + win_h)
        best_area = 0
        for r in rects:
            area = _intersection_area(win_rect, r)
            if area > best_area:
                best_area = area
                target = r

    if target is None:
        return default_position(win_w, win_h, primary)

    left, top, right, bottom = target
    max_x = max(left, right - win_w - margin_x)
    max_y = max(top, bottom - win_h - margin_y)
    return (
        max(left, min(ix, max_x)),
        max(top, min(iy, max_y)),
    )


# ---------------- 설정 파일 (창 위치, 투명도 등) ----------------
DEFAULT_CONFIG = {
    "x": None,
    "y": None,
    "alpha": 0.92,
    "topmost": True,
    "compact": False,
    "transparent_bg": True,
    "port": 47823,
    "auto_update_check": True,
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
