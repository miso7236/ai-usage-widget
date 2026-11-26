"""Material Design 3 다크 스타일 설정 창 (순수 Tkinter).

외부 의존성 없이 `tk.Toplevel`과 Windows 11 DWM 다크 타이틀바를 사용해
위젯의 모든 표시·수집·시스템 설정을 한곳에서 실시간으로 변경한다.
순수 검증/레이아웃 계산 함수는 tkinter 없이도 리눅스 CI에서 단위 테스트할 수 있다.
"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from dataclasses import dataclass
from typing import Callable, Sequence

from state import FONT_PRESETS, Rect, clamp_scale

GOOGLE_DOTS = ("#4285f4", "#ea4335", "#fbbc04", "#34a853")

SETTINGS_PALETTE: dict[str, str] = {
    "bg": "#202124",
    "sidebar": "#1b1c1f",
    "card": "#292a2d",
    "card_hover": "#35363a",
    "border": "#3c4043",
    "divider": "#35363a",
    "fg": "#e8eaed",
    "muted": "#9aa0a6",
    "disabled": "#6e7278",
    "accent": "#8ab4f8",
    "accent_bg": "#2b3a4d",
    "accent_fg": "#202124",
    "danger": "#f28b82",
    "switch_off": "#5f6368",
    "switch_on": "#8ab4f8",
    "knob_on": "#17202e",
    "knob_off": "#e8eaed",
    "track": "#3c4043",
}

TABS: tuple[tuple[str, str, str], ...] = (
    ("display", "표시", "◐"),
    ("sources", "수집·계정", "⇄"),
    ("system", "시스템·정보", "⚙"),
)

BROWSER_INTERVAL_CHOICES: tuple[int, ...] = (1, 3, 5, 10, 15)
TRACK_FAST_CHOICES: tuple[int, ...] = (15, 30, 60)  # 사용량 변동 추적 중 확인 간격(초)

POSITION_GRID: tuple[tuple[tuple[str, str], ...], ...] = (
    (
        ("top_left", "왼쪽 상단"),
        ("top_center", "중앙 최상단"),
        ("top_right", "오른쪽 상단"),
    ),
    (
        ("bottom_left", "왼쪽 하단"),
        ("bottom_center", "중앙 하단"),
        ("bottom_right", "오른쪽 하단"),
    ),
)

MIN_ALPHA_PCT = 60
MAX_ALPHA_PCT = 100
MIN_SCALE_PCT = 60
MAX_SCALE_PCT = 200
MIN_BROWSER_INTERVAL = 1
MAX_BROWSER_INTERVAL = 60

WINDOW_WIDTH = 560
WINDOW_HEIGHT = 500
MIN_WINDOW_WIDTH = 440
MIN_WINDOW_HEIGHT = 360

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
GA_ROOT = 2


# ---------------- 순수 계산·검증 함수 (CI 테스트 대상) ----------------
def clamp_window_size(
    width: int,
    height: int,
    monitor: Rect,
    margin: int = 24,
) -> tuple[int, int]:
    """설정 창 크기를 최소 크기와 현재 모니터 작업 영역 범위로 보정한다."""
    left, top, right, bottom = monitor
    max_w = max(MIN_WINDOW_WIDTH, (right - left) - margin * 2)
    max_h = max(MIN_WINDOW_HEIGHT, (bottom - top) - margin * 2)
    w = max(MIN_WINDOW_WIDTH, min(int(width), max_w))
    h = max(MIN_WINDOW_HEIGHT, min(int(height), max_h))
    return w, h


def clamp_alpha_percent(value) -> int:
    """투명도 퍼센트를 60~100 범위 정수로 보정한다."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 92
    if n != n:  # NaN
        return 92
    return max(MIN_ALPHA_PCT, min(MAX_ALPHA_PCT, round(n)))


def clamp_scale_percent(value) -> int:
    """크기 배율 퍼센트를 60~200 범위 정수로 보정한다."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 100
    if n != n:
        return 100
    clamped = clamp_scale(n / 100.0)
    return max(MIN_SCALE_PCT, min(MAX_SCALE_PCT, round(clamped * 100)))


def normalize_track_fast_sec(value) -> int:
    """추적 중 확인 간격을 고를 수 있는 값 중 하나로 맞춘다. 알 수 없는 값이면 15초."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return TRACK_FAST_CHOICES[0]
    return n if n in TRACK_FAST_CHOICES else TRACK_FAST_CHOICES[0]


def normalize_browser_interval(value) -> int:
    """내장 브라우저 수집 주기(분)를 1~60 범위 정수로 보정한다."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return 3
    return max(MIN_BROWSER_INTERVAL, min(MAX_BROWSER_INTERVAL, n))


def center_window_rect(
    width: int,
    height: int,
    monitor: Rect,
    margin: int = 16,
) -> tuple[int, int]:
    """설정 창을 대상 모니터 작업 영역 중앙에 배치하되 경계를 벗어나지 않게 보정한다."""
    left, top, right, bottom = monitor
    min_x = left + margin
    min_y = top + margin
    max_x = max(min_x, right - width - margin)
    max_y = max(min_y, bottom - height - margin)
    x = left + (right - left - width) // 2
    y = top + (bottom - top - height) // 2
    return int(max(min_x, min(x, max_x))), int(max(min_y, min(y, max_y)))


def apply_win11_dark_titlebar(hwnd: int) -> bool:
    """Windows 10/11에서 설정 창 제목 표시줄에 다크 모드를 적용한다."""
    if sys.platform != "win32" or hwnd <= 0:
        return False
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        top_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        enabled = ctypes.c_int(1)
        for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            res = dwmapi.DwmSetWindowAttribute(
                top_hwnd,
                attr,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if res == 0:
                return True
        return False
    except Exception:
        return False


@dataclass(frozen=True)
class SettingsSnapshot:
    """설정 창 렌더링·동기화에 필요한 현재 상태 스냅샷."""

    compact: bool
    show_pace: bool
    usage_tracking: bool
    track_wave: bool
    track_gain: bool
    tracking_fast_sec: int
    show_antigravity: bool
    transparent_bg: bool
    transparent_bg_supported: bool
    adaptive_compact_color: bool
    topmost: bool
    alpha_pct: int
    scale_pct: int
    scale_mode_label: str
    font: str
    web_source: str
    browser_interval_min: int
    browser_available: bool
    claude_source: str
    autostart_enabled: bool
    autostart_supported: bool
    auto_update_check: bool
    version: str
    update_available_version: str | None = None
    adaptive_supported: bool = True  # 직접 그리는 방식에서는 글자 외곽선을 쓰므로 주변 색 보정이 필요 없다
    tracking_now: tuple[str, ...] = ()  # 지금 추적 중인 서비스 이름


@dataclass(frozen=True)
class SettingsCallbacks:
    """설정 창에서 위젯 본체로 전달하는 실시간 반영 콜백 모음."""

    get_snapshot: Callable[[], SettingsSnapshot]
    get_monitor: Callable[[], Rect]
    get_font_family: Callable[[], str]
    on_set_compact: Callable[[bool], None]
    on_set_show_pace: Callable[[bool], None]
    on_set_usage_tracking: Callable[[bool], None]
    on_set_track_wave: Callable[[bool], None]
    on_set_track_gain: Callable[[bool], None]
    on_set_tracking_fast_sec: Callable[[int], None]
    on_set_show_antigravity: Callable[[bool], None]
    on_set_transparent_bg: Callable[[bool], None]
    on_set_adaptive_compact_color: Callable[[bool], None]
    on_set_topmost: Callable[[bool], None]
    on_set_alpha_pct: Callable[[int, bool], None]  # (pct, save)
    on_set_scale_pct: Callable[[int, bool], None]  # (pct, save)
    on_set_font: Callable[[str], None]
    on_move_preset: Callable[[str], None]
    on_reset_position: Callable[[], None]
    on_set_web_source: Callable[[str], None]
    on_open_browser_login: Callable[[str], None]
    on_set_browser_interval: Callable[[int], None]
    on_set_claude_source: Callable[[str], None]
    on_open_extension_folder: Callable[[], None]
    on_set_autostart: Callable[[bool], None]
    on_set_auto_update_check: Callable[[bool], None]
    on_check_updates: Callable[[], None]
    on_start_update: Callable[[], None]
    on_copy_diagnostics: Callable[[], None]
    on_open_log_folder: Callable[[], None]


# ---------------- 커스텀 다크 위젯 ----------------
class _ToggleSwitch(tk.Canvas):
    """Material 3 스타일 알약형 토글 스위치."""

    WIDTH = 36
    HEIGHT = 20

    def __init__(
        self,
        parent: tk.Misc,
        pal: dict[str, str],
        on_toggle: Callable[[bool], None],
        *,
        bg: str,
        checked: bool = False,
        enabled: bool = True,
    ) -> None:
        super().__init__(
            parent,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2" if enabled else "",
        )
        self._pal = pal
        self._on_toggle = on_toggle
        self._checked = bool(checked)
        self._enabled = bool(enabled)
        self.bind("<Button-1>", self._handle_click)
        self._draw()

    def set_state(self, checked: bool, enabled: bool | None = None) -> None:
        if enabled is not None:
            self._enabled = bool(enabled)
            self.configure(cursor="hand2" if self._enabled else "")
        self._checked = bool(checked)
        self._draw()

    def trigger(self) -> None:
        if not self._enabled:
            return
        new_val = not self._checked
        self._checked = new_val
        self._draw()
        self._on_toggle(new_val)

    def _handle_click(self, _event) -> None:
        self.trigger()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.WIDTH, self.HEIGHT
        pal = self._pal
        if not self._enabled:
            track = pal["border"]
            knob = pal["disabled"]
        elif self._checked:
            track = pal["switch_on"]
            knob = pal["knob_on"]
        else:
            track = pal["switch_off"]
            knob = pal["knob_off"]
        # 양 끝이 둥근 트랙
        self.create_oval(0, 0, h, h, fill=track, outline="")
        self.create_oval(w - h, 0, w, h, fill=track, outline="")
        self.create_rectangle(h // 2, 0, w - h // 2, h, fill=track, outline="")
        # 손잡이 원
        pad = 3
        d = h - pad * 2
        kx = (w - pad - d) if self._checked else pad
        self.create_oval(kx, pad, kx + d, pad + d, fill=knob, outline="")


class _DarkSlider(tk.Canvas):
    """클릭·드래그로 실시간 값을 조절하는 Material 3 스타일 다크 슬라이더."""

    HEIGHT = 22
    THUMB_R = 7

    def __init__(
        self,
        parent: tk.Misc,
        pal: dict[str, str],
        min_val: int,
        max_val: int,
        on_change: Callable[[int, bool], None],
        *,
        bg: str,
        value: int = 100,
    ) -> None:
        super().__init__(
            parent,
            height=self.HEIGHT,
            bg=bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._pal = pal
        self._min = int(min_val)
        self._max = int(max_val)
        self._value = max(self._min, min(self._max, int(value)))
        self._on_change = on_change
        self._dragging = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_value(self, value: int) -> None:
        clamped = max(self._min, min(self._max, int(value)))
        if clamped != self._value:
            self._value = clamped
            self._draw()

    def _x_to_value(self, x: int) -> int:
        w = max(self.THUMB_R * 2 + 2, self.winfo_width())
        usable = max(1, w - self.THUMB_R * 2)
        ratio = max(0.0, min(1.0, (x - self.THUMB_R) / usable))
        return round(self._min + ratio * (self._max - self._min))

    def _on_press(self, event) -> None:
        self._dragging = True
        self._apply_from_x(event.x, save=False)

    def _on_motion(self, event) -> None:
        if self._dragging:
            self._apply_from_x(event.x, save=False)

    def _on_release(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._apply_from_x(event.x, save=True)

    def _apply_from_x(self, x: int, *, save: bool) -> None:
        new_val = self._x_to_value(x)
        changed = new_val != self._value
        self._value = new_val
        self._draw()
        if changed or save:
            self._on_change(new_val, save)

    def _draw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            return
        h = self.HEIGHT
        r = self.THUMB_R
        cy = h // 2
        track_h = 4
        y0, y1 = cy - track_h // 2, cy + track_h // 2
        usable = max(1, w - r * 2)
        ratio = (self._value - self._min) / max(1, self._max - self._min)
        cx = r + round(usable * ratio)
        pal = self._pal
        # 배경 트랙
        self.create_rectangle(r, y0, w - r, y1, fill=pal["track"], outline="")
        # 활성 트랙
        if cx > r:
            self.create_rectangle(r, y0, cx, y1, fill=pal["accent"], outline="")
        # 썸
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=pal["accent"], outline=pal["bg"], width=2)


# ---------------- 메인 설정 창 ----------------
class SettingsWindow:
    """좌측 3탭 사이드바를 가진 싱글톤 설정 창."""

    def __init__(
        self,
        root: tk.Tk,
        callbacks: SettingsCallbacks,
        palette: dict[str, str] | None = None,
    ) -> None:
        self.root = root
        self.cb = callbacks
        self.pal = dict(SETTINGS_PALETTE if palette is None else palette)
        self._win: tk.Toplevel | None = None
        self._active_tab = "display"
        self._syncing = False

        # 동적 UI 참조 (open 시 초기화, close 시 정리)
        self._nav_buttons: dict[str, tuple[tk.Frame, tk.Label, tk.Label]] = {}
        self._pages: dict[str, tk.Frame] = {}
        self._switches: dict[str, _ToggleSwitch] = {}
        self._content_canvas: tk.Canvas | None = None
        self._refresh_scroll: Callable[[], None] | None = None
        self._last_size: tuple[int, int] | None = None
        self._alpha_slider: _DarkSlider | None = None
        self._alpha_val_lbl: tk.Label | None = None
        self._scale_slider: _DarkSlider | None = None
        self._scale_title_lbl: tk.Label | None = None
        self._scale_val_lbl: tk.Label | None = None
        self._font_pills: dict[str, tk.Label] = {}
        self._web_source_pills: dict[str, tk.Label] = {}
        self._interval_pills: dict[int, tk.Label] = {}
        self._track_fast_pills: dict[int, tk.Label] = {}
        self._track_status_lbl: tk.Label | None = None
        self._claude_source_pills: dict[str, tk.Label] = {}
        self._browser_login_btns: list[tk.Label] = []
        self._browser_hint_lbl: tk.Label | None = None
        self._update_status_lbl: tk.Label | None = None
        self._update_action_btn: tk.Label | None = None
        self._version_lbl: tk.Label | None = None

    @property
    def is_open(self) -> bool:
        try:
            return self._win is not None and bool(self._win.winfo_exists())
        except tk.TclError:
            return False

    @property
    def window(self) -> tk.Toplevel | None:
        return self._win if self.is_open else None

    def open(self, tab: str | None = None) -> None:
        """설정 창을 연다. 이미 열려 있으면 포커스만 올리고 최신 상태로 동기화한다."""
        if tab and any(key == tab for key, _, _ in TABS):
            self._active_tab = tab
        if self.is_open and self._win is not None:
            self._select_tab(self._active_tab)
            self.sync()
            self._win.deiconify()
            self._win.lift()
            try:
                self._win.focus_force()
            except tk.TclError:
                pass
            return

        self._build_window()
        self._select_tab(self._active_tab)
        self.sync()

    def close(self) -> None:
        win = self._win
        self._win = None
        self._clear_refs()
        if win is not None:
            try:
                if win.winfo_exists():
                    w = win.winfo_width()
                    h = win.winfo_height()
                    if w >= MIN_WINDOW_WIDTH and h >= MIN_WINDOW_HEIGHT:
                        self._last_size = (w, h)
                    win.destroy()
            except tk.TclError:
                pass

    def _clear_refs(self) -> None:
        self._nav_buttons.clear()
        self._pages.clear()
        self._switches.clear()
        self._font_pills.clear()
        self._web_source_pills.clear()
        self._interval_pills.clear()
        self._track_fast_pills.clear()
        self._track_status_lbl = None
        self._claude_source_pills.clear()
        self._browser_login_btns.clear()
        self._alpha_slider = None
        self._alpha_val_lbl = None
        self._scale_slider = None
        self._scale_title_lbl = None
        self._scale_val_lbl = None
        self._browser_hint_lbl = None
        self._update_status_lbl = None
        self._update_action_btn = None
        self._version_lbl = None
        self._content_canvas = None
        self._refresh_scroll = None

    # ---------------- UI 생성 ----------------
    def _build_window(self) -> None:
        pal = self.pal
        fam = self.cb.get_font_family()
        snap = self.cb.get_snapshot()

        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title("AI 사용량 위젯 — 설정")
        win.configure(bg=pal["bg"])
        win.resizable(True, True)
        win.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda _e: self.close())
        self._win = win

        # 폰트 세트
        f_title = (fam, 11, "bold")
        f_section = (fam, 9, "bold")
        f_body = (fam, 9)
        f_sub = (fam, 8)
        f_nav = (fam, 9, "bold")

        root_wrap = tk.Frame(win, bg=pal["bg"])
        root_wrap.pack(fill="both", expand=True)

        # 좌측 사이드바
        sidebar = tk.Frame(root_wrap, bg=pal["sidebar"], width=148)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand_box = tk.Frame(sidebar, bg=pal["sidebar"], padx=14, pady=14)
        brand_box.pack(fill="x")
        dots_row = tk.Frame(brand_box, bg=pal["sidebar"])
        dots_row.pack(anchor="w")
        dot_d = 5
        gap = 3
        dots = tk.Canvas(
            dots_row,
            width=dot_d * 4 + gap * 3,
            height=dot_d,
            bg=pal["sidebar"],
            highlightthickness=0,
            bd=0,
        )
        for idx, color in enumerate(GOOGLE_DOTS):
            x0 = idx * (dot_d + gap)
            dots.create_oval(x0, 0, x0 + dot_d, dot_d, fill=color, outline="")
        dots.pack(side="left", padx=(0, 6))
        tk.Label(dots_row, text="AI 사용량", font=f_title, bg=pal["sidebar"], fg=pal["fg"]).pack(side="left")
        tk.Label(brand_box, text="설정", font=f_sub, bg=pal["sidebar"], fg=pal["muted"]).pack(anchor="w", pady=(2, 0))

        nav_wrap = tk.Frame(sidebar, bg=pal["sidebar"], padx=8)
        nav_wrap.pack(fill="x", pady=(4, 0))
        for key, label, icon in TABS:
            row = tk.Frame(nav_wrap, bg=pal["sidebar"], padx=10, pady=8, cursor="hand2")
            row.pack(fill="x", pady=2)
            ic_lbl = tk.Label(row, text=icon, font=f_nav, bg=pal["sidebar"], fg=pal["muted"], width=2, cursor="hand2")
            ic_lbl.pack(side="left")
            tx_lbl = tk.Label(row, text=label, font=f_nav, bg=pal["sidebar"], fg=pal["muted"], anchor="w", cursor="hand2")
            tx_lbl.pack(side="left", fill="x", expand=True)
            for w in (row, ic_lbl, tx_lbl):
                w.bind("<Button-1>", lambda _e, k=key: self._select_tab(k))
            self._nav_buttons[key] = (row, ic_lbl, tx_lbl)

        self._version_lbl = tk.Label(
            sidebar,
            text=f"v{snap.version}",
            font=f_sub,
            bg=pal["sidebar"],
            fg=pal["muted"],
            padx=14,
            pady=12,
            anchor="w",
        )
        self._version_lbl.pack(side="bottom", fill="x")

        # 세로 구분선
        tk.Frame(root_wrap, bg=pal["border"], width=1).pack(side="left", fill="y")

        # 우측 콘텐츠 컨테이너 (스크롤 가능)
        content_outer = tk.Frame(root_wrap, bg=pal["bg"])
        content_outer.pack(side="left", fill="both", expand=True)

        canvas = tk.Canvas(content_outer, bg=pal["bg"], highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(content_outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        content_inner = tk.Frame(canvas, bg=pal["bg"], padx=16, pady=14)
        inner_id = canvas.create_window((0, 0), window=content_inner, anchor="nw")

        def _on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            # 스크롤이 불필요하면 스크롤바를 숨기고 상단으로 맞춘다.
            bbox = canvas.bbox("all")
            if bbox and (bbox[3] - bbox[1]) <= canvas.winfo_height():
                if scrollbar.winfo_ismapped():
                    scrollbar.pack_forget()
                canvas.yview_moveto(0.0)
            elif not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y")

        def _on_canvas_configure(event):
            canvas.itemconfigure(inner_id, width=event.width)
            if self._browser_hint_lbl is not None:
                try:
                    self._browser_hint_lbl.configure(wraplength=max(180, event.width - 60))
                except tk.TclError:
                    pass
            _on_inner_configure()

        self._content_canvas = canvas
        self._refresh_scroll = _on_inner_configure
        content_inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            bbox = canvas.bbox("all")
            if not bbox or (bbox[3] - bbox[1]) <= canvas.winfo_height():
                return
            delta = getattr(event, "delta", 0)
            if delta == 0:
                num = getattr(event, "num", 0)
                step = -1 if num == 4 else (1 if num == 5 else 0)
            else:
                step = -1 if delta > 0 else 1
            if step:
                canvas.yview_scroll(step * 2, "units")

        win.bind("<MouseWheel>", _on_mousewheel)
        win.bind("<Button-4>", _on_mousewheel)
        win.bind("<Button-5>", _on_mousewheel)

        # 3개 탭 페이지 구성
        self._pages["display"] = self._build_display_page(content_inner, f_section, f_body, f_sub)
        self._pages["sources"] = self._build_sources_page(content_inner, f_section, f_body, f_sub)
        self._pages["system"] = self._build_system_page(content_inner, f_section, f_body, f_sub)

        # 모니터 중앙 배치 및 Windows 11 다크 타이틀바 적용
        monitor = self.cb.get_monitor()
        init_w, init_h = self._last_size if self._last_size is not None else (WINDOW_WIDTH, WINDOW_HEIGHT)
        init_w, init_h = clamp_window_size(init_w, init_h, monitor)
        px, py = center_window_rect(init_w, init_h, monitor)
        win.geometry(f"{init_w}x{init_h}+{px}+{py}")
        win.update_idletasks()
        apply_win11_dark_titlebar(win.winfo_id())
        win.deiconify()
        win.lift()
        try:
            win.focus_force()
        except tk.TclError:
            pass

    def _make_card(self, parent: tk.Misc, title: str, f_section: tuple) -> tk.Frame:
        pal = self.pal
        tk.Label(
            parent,
            text=title,
            font=f_section,
            bg=pal["bg"],
            fg=pal["accent"],
            anchor="w",
        ).pack(fill="x", pady=(0, 5))
        card = tk.Frame(
            parent,
            bg=pal["card"],
            padx=12,
            pady=8,
            highlightthickness=1,
            highlightbackground=pal["border"],
        )
        card.pack(fill="x", pady=(0, 12))
        return card

    def _add_toggle_row(
        self,
        card: tk.Frame,
        key: str,
        title: str,
        subtitle: str | None,
        on_toggle: Callable[[bool], None],
        f_body: tuple,
        f_sub: tuple,
        *,
        divider: bool = True,
    ) -> None:
        pal = self.pal
        if divider and card.winfo_children():
            tk.Frame(card, bg=pal["divider"], height=1).pack(fill="x", pady=6)
        row = tk.Frame(card, bg=pal["card"], cursor="hand2")
        row.pack(fill="x")
        text_box = tk.Frame(row, bg=pal["card"], cursor="hand2")
        text_box.pack(side="left", fill="x", expand=True, padx=(0, 10))
        t_lbl = tk.Label(text_box, text=title, font=f_body, bg=pal["card"], fg=pal["fg"], anchor="w", cursor="hand2")
        t_lbl.pack(fill="x")
        click_targets: list[tk.Widget] = [row, text_box, t_lbl]
        if subtitle:
            s_lbl = tk.Label(
                text_box, text=subtitle, font=f_sub, bg=pal["card"], fg=pal["muted"], anchor="w", cursor="hand2"
            )
            s_lbl.pack(fill="x", pady=(1, 0))
            click_targets.append(s_lbl)
        sw = _ToggleSwitch(row, pal, lambda val: self._fire(on_toggle, val), bg=pal["card"])
        sw.pack(side="right", anchor="center")
        self._switches[key] = sw
        for w in click_targets:
            w.bind("<Button-1>", lambda _e, s=sw: s.trigger())

    def _make_pill(
        self,
        parent: tk.Misc,
        text: str,
        font: tuple,
        command: Callable[[], None],
        *,
        padx: int = 10,
        pady: int = 5,
    ) -> tk.Label:
        pal = self.pal
        lbl = tk.Label(
            parent,
            text=text,
            font=font,
            bg=pal["bg"],
            fg=pal["fg"],
            padx=padx,
            pady=pady,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=pal["border"],
        )
        lbl.bind("<Button-1>", lambda _e: self._fire(command))
        return lbl

    def _make_action_button(
        self,
        parent: tk.Misc,
        text: str,
        font: tuple,
        command: Callable[[], None],
        *,
        accent: bool = False,
        padx: int = 12,
        pady: int = 6,
    ) -> tk.Label:
        pal = self.pal
        bg = pal["accent"] if accent else pal["bg"]
        fg = pal["accent_fg"] if accent else pal["fg"]
        hover_bg = "#aecbfa" if accent else pal["card_hover"]
        btn = tk.Label(
            parent,
            text=text,
            font=font,
            bg=bg,
            fg=fg,
            padx=padx,
            pady=pady,
            cursor="hand2",
            highlightthickness=0 if accent else 1,
            highlightbackground=pal["border"],
        )
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover_bg) if btn.cget("cursor") == "hand2" else None)
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg) if btn.cget("cursor") == "hand2" else None)
        btn.bind("<Button-1>", lambda _e: self._fire(command) if btn.cget("cursor") == "hand2" else None)
        return btn

    def _paint_pill(self, lbl: tk.Label, selected: bool, enabled: bool = True) -> None:
        pal = self.pal
        if not enabled:
            lbl.configure(bg=pal["bg"], fg=pal["disabled"], highlightbackground=pal["border"], cursor="")
        elif selected:
            lbl.configure(bg=pal["accent_bg"], fg=pal["accent"], highlightbackground=pal["accent"], cursor="hand2")
        else:
            lbl.configure(bg=pal["bg"], fg=pal["fg"], highlightbackground=pal["border"], cursor="hand2")

    # ---------------- 탭 1: 표시 ----------------
    def _build_display_page(
        self, parent: tk.Misc, f_section: tuple, f_body: tuple, f_sub: tuple
    ) -> tk.Frame:
        pal = self.pal
        page = tk.Frame(parent, bg=pal["bg"])

        # 모양 카드
        card_look = self._make_card(page, "모양", f_section)
        self._add_toggle_row(
            card_look, "compact", "컴팩트 모드", "서비스마다 링과 숫자만 한 줄로 작게 표시", self.cb.on_set_compact, f_body, f_sub
        )
        self._add_toggle_row(
            card_look, "transparent_bg", "투명 배경", "배경 판 없이 링과 글자만 표시 (Windows)", self.cb.on_set_transparent_bg, f_body, f_sub
        )
        if self.cb.get_snapshot().adaptive_supported:
            # 직접 그리는 기본 방식에서는 글자 외곽선을 쓰므로 필요 없다. 기존(classic) 렌더링에서만 보여 준다.
            self._add_toggle_row(
                card_look,
                "adaptive_compact_color",
                "주변 색에 맞춰 컴팩트 색 자동 보정",
                "컴팩트 + 투명 배경에서 배경 밝기에 따라 글자색 자동 조절",
                self.cb.on_set_adaptive_compact_color,
                f_body,
                f_sub,
            )
        self._add_toggle_row(
            card_look, "topmost", "항상 위", "다른 창 위에 위젯을 항상 고정", self.cb.on_set_topmost, f_body, f_sub
        )

        # 표시 항목 카드
        card_items = self._make_card(page, "표시 항목", f_section)
        self._add_toggle_row(
            card_items,
            "show_pace",
            "적정 속도 눈금",
            "지난 시간에 비해 얼마나 썼어야 적당한지 흰 눈금(상세 모드는 기준 숫자)으로 표시",
            self.cb.on_set_show_pace,
            f_body,
            f_sub,
        )
        self._add_toggle_row(
            card_items,
            "track_wave",
            "추적 물결",
            "사용량 변동 추적 중인 서비스의 링 둘레에 물결 표시 (컴팩트 모드)",
            self.cb.on_set_track_wave,
            f_body,
            f_sub,
        )
        self._add_toggle_row(
            card_items,
            "track_gain",
            "늘어난 양 표시",
            "추적을 시작한 뒤 늘어난 만큼을 링의 밝은 구간과 +N으로 표시 (컴팩트 모드)",
            self.cb.on_set_track_gain,
            f_body,
            f_sub,
        )

        # 투명도·크기 카드
        card_size = self._make_card(page, "투명도 · 크기", f_section)

        alpha_hdr = tk.Frame(card_size, bg=pal["card"])
        alpha_hdr.pack(fill="x")
        tk.Label(alpha_hdr, text="투명도", font=f_body, bg=pal["card"], fg=pal["fg"]).pack(side="left")
        self._alpha_val_lbl = tk.Label(alpha_hdr, text="92%", font=f_body, bg=pal["card"], fg=pal["accent"])
        self._alpha_val_lbl.pack(side="right")
        self._alpha_slider = _DarkSlider(
            card_size,
            pal,
            MIN_ALPHA_PCT,
            MAX_ALPHA_PCT,
            self._on_alpha_slider,
            bg=pal["card"],
            value=92,
        )
        self._alpha_slider.pack(fill="x", pady=(2, 6))

        tk.Frame(card_size, bg=pal["divider"], height=1).pack(fill="x", pady=4)

        scale_hdr = tk.Frame(card_size, bg=pal["card"])
        scale_hdr.pack(fill="x")
        self._scale_title_lbl = tk.Label(scale_hdr, text="크기", font=f_body, bg=pal["card"], fg=pal["fg"])
        self._scale_title_lbl.pack(side="left")
        self._scale_val_lbl = tk.Label(scale_hdr, text="100%", font=f_body, bg=pal["card"], fg=pal["accent"])
        self._scale_val_lbl.pack(side="right")
        self._scale_slider = _DarkSlider(
            card_size,
            pal,
            MIN_SCALE_PCT,
            MAX_SCALE_PCT,
            self._on_scale_slider,
            bg=pal["card"],
            value=100,
        )
        self._scale_slider.pack(fill="x", pady=(2, 2))
        tk.Label(
            card_size,
            text="위젯 위 마우스 휠 또는 오른쪽 아래 모서리 드래그로도 조절할 수 있어요.",
            font=f_sub,
            bg=pal["card"],
            fg=pal["muted"],
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        # 글꼴 카드
        card_font = self._make_card(page, "글꼴", f_section)
        font_grid = tk.Frame(card_font, bg=pal["card"])
        font_grid.pack(fill="x")
        for idx, (key, lbl_text) in enumerate(FONT_PRESETS):
            short = lbl_text.split("(", 1)[0].strip() if idx > 0 else "자동 (가독성 우선)"
            pill = self._make_pill(
                font_grid, short, f_sub, lambda k=key: self.cb.on_set_font(k), padx=8, pady=5
            )
            pill.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=3, pady=3)
            self._font_pills[key] = pill
        font_grid.columnconfigure(0, weight=1)
        font_grid.columnconfigure(1, weight=1)

        # 위치 카드
        card_pos = self._make_card(page, "위젯 위치", f_section)
        pos_grid = tk.Frame(card_pos, bg=pal["card"])
        pos_grid.pack(fill="x")
        for r_idx, row_presets in enumerate(POSITION_GRID):
            for c_idx, (p_key, p_lbl) in enumerate(row_presets):
                btn = self._make_action_button(
                    pos_grid,
                    p_lbl,
                    f_sub,
                    lambda k=p_key: self.cb.on_move_preset(k),
                    padx=6,
                    pady=6,
                )
                btn.grid(row=r_idx, column=c_idx, sticky="ew", padx=3, pady=3)
        for c_idx in range(3):
            pos_grid.columnconfigure(c_idx, weight=1)
        reset_btn = self._make_action_button(
            card_pos,
            "⌖ 주 모니터 기본 위치로 복귀",
            f_sub,
            self.cb.on_reset_position,
            padx=10,
            pady=5,
        )
        reset_btn.pack(fill="x", padx=3, pady=(6, 0))

        return page

    # ---------------- 탭 2: 수집·계정 ----------------
    def _build_sources_page(
        self, parent: tk.Misc, f_section: tuple, f_body: tuple, f_sub: tuple
    ) -> tk.Frame:
        pal = self.pal
        page = tk.Frame(parent, bg=pal["bg"])

        def note(card: tk.Frame, text: str, pady=(8, 0)) -> tk.Label:
            lbl = tk.Label(
                card, text=text, font=f_sub, bg=pal["card"], fg=pal["muted"], anchor="w", justify="left", wraplength=340
            )
            lbl.pack(fill="x", pady=pady)
            # 창 너비에 맞춰 줄바꿈 위치를 다시 잡는다
            card.bind("<Configure>", lambda e, target=lbl: target.configure(wraplength=max(180, e.width - 30)), add="+")
            return lbl

        def heading(card: tk.Frame, text: str) -> None:
            tk.Label(card, text=text, font=f_body, bg=pal["card"], fg=pal["fg"], anchor="w").pack(fill="x", pady=(0, 6))

        def divider(card: tk.Frame) -> None:
            tk.Frame(card, bg=pal["divider"], height=1).pack(fill="x", pady=10)

        # 서비스 카드
        card_services = self._make_card(page, "서비스", f_section)
        heading(card_services, "Claude 계정")
        claude_row = tk.Frame(card_services, bg=pal["card"])
        claude_row.pack(fill="x")
        for idx, (key, lbl_text) in enumerate(
            (("web", "웹 계정 (확장/내장)"), ("oauth", "로컬 OAuth (Claude Code)"))
        ):
            pill = self._make_pill(
                claude_row, lbl_text, f_sub, lambda k=key: self.cb.on_set_claude_source(k), padx=8, pady=7
            )
            pill.grid(row=0, column=idx, sticky="ew", padx=3)
            self._claude_source_pills[key] = pill
        claude_row.columnconfigure(0, weight=1)
        claude_row.columnconfigure(1, weight=1)
        self._add_toggle_row(
            card_services, "show_antigravity", "Antigravity 표시", "이 PC에서 실행 중인 Antigravity의 모델 사용량을 함께 표시", self.cb.on_set_show_antigravity, f_body, f_sub
        )

        # 웹 수집 방식 카드
        card_web = self._make_card(page, "웹 사용량 수집 방식", f_section)
        web_row = tk.Frame(card_web, bg=pal["card"])
        web_row.pack(fill="x")
        for idx, (key, lbl_text) in enumerate(
            (("extension", "크롬 확장 (기본)"), ("browser", "내장 브라우저 (WebView2)"))
        ):
            pill = self._make_pill(
                web_row, lbl_text, f_body, lambda k=key: self.cb.on_set_web_source(k), padx=10, pady=7
            )
            pill.grid(row=0, column=idx, sticky="ew", padx=3)
            self._web_source_pills[key] = pill
        web_row.columnconfigure(0, weight=1)
        web_row.columnconfigure(1, weight=1)
        self._browser_hint_lbl = note(card_web, "내장 브라우저를 쓰면 크롬을 켜지 않아도 주기적으로 사용량을 가져와요.")

        divider(card_web)
        heading(card_web, "내장 브라우저 로그인")
        login_row = tk.Frame(card_web, bg=pal["card"])
        login_row.pack(fill="x")
        for idx, (prov, lbl_text) in enumerate(
            (("claude", "Claude 로그인…"), ("chatgpt", "ChatGPT 로그인…"), ("gemini", "Gemini 로그인…"))
        ):
            btn = self._make_action_button(
                login_row,
                lbl_text,
                f_sub,
                lambda p=prov: self.cb.on_open_browser_login(p),
                padx=8,
                pady=6,
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=3)
            self._browser_login_btns.append(btn)
        for idx in range(3):
            login_row.columnconfigure(idx, weight=1)
        note(card_web, "각 서비스에 한 번만 로그인하면 로그인 상태가 유지돼요.")

        # 수집 주기 카드
        card_interval = self._make_card(page, "수집 주기", f_section)
        int_row = tk.Frame(card_interval, bg=pal["card"])
        int_row.pack(fill="x")
        for idx, mins in enumerate(BROWSER_INTERVAL_CHOICES):
            pill = self._make_pill(
                int_row, f"{mins}분", f_sub, lambda m=mins: self.cb.on_set_browser_interval(m), padx=6, pady=5
            )
            pill.grid(row=0, column=idx, sticky="ew", padx=2)
            self._interval_pills[mins] = pill
            int_row.columnconfigure(idx, weight=1)
        note(card_interval, "내장 브라우저와 Claude 로컬 OAuth에 적용돼요. 크롬 확장의 주기는 확장 팝업에서 정해요.")

        # 사용량 변동 추적 카드
        card_track = self._make_card(page, "사용량 변동 추적", f_section)
        self._add_toggle_row(
            card_track,
            "usage_tracking",
            "사용량이 늘면 더 자주 확인",
            "5분간 변동이 없으면 위의 수집 주기로 돌아가요. (크롬 확장·Gemini 제외)",
            self.cb.on_set_usage_tracking,
            f_body,
            f_sub,
        )
        divider(card_track)
        heading(card_track, "추적 중 확인 간격")
        fast_row = tk.Frame(card_track, bg=pal["card"])
        fast_row.pack(fill="x")
        for idx, sec in enumerate(TRACK_FAST_CHOICES):
            label = f"{sec}초" if sec < 60 else f"{sec // 60}분"
            pill = self._make_pill(
                fast_row, label, f_sub, lambda s=sec: self.cb.on_set_tracking_fast_sec(s), padx=6, pady=5
            )
            pill.grid(row=0, column=idx, sticky="ew", padx=2)
            self._track_fast_pills[sec] = pill
            fast_row.columnconfigure(idx, weight=1)
        self._track_status_lbl = note(card_track, "")

        # 크롬 확장 폴더 카드
        card_ext = self._make_card(page, "크롬 확장", f_section)
        ext_btn = self._make_action_button(
            card_ext,
            "❐ 크롬 확장 폴더 열기 (경로 자동 복사)",
            f_body,
            self.cb.on_open_extension_folder,
            padx=10,
            pady=7,
        )
        ext_btn.pack(fill="x")

        return page

    # ---------------- 탭 3: 시스템·정보 ----------------
    def _build_system_page(
        self, parent: tk.Misc, f_section: tuple, f_body: tuple, f_sub: tuple
    ) -> tk.Frame:
        pal = self.pal
        page = tk.Frame(parent, bg=pal["bg"])

        card_sys = self._make_card(page, "시작 및 업데이트", f_section)
        self._add_toggle_row(
            card_sys,
            "autostart",
            "Windows 시작 시 자동 실행",
            "로그인할 때 위젯을 자동으로 실행해요.",
            self.cb.on_set_autostart,
            f_body,
            f_sub,
        )
        self._add_toggle_row(
            card_sys,
            "auto_update_check",
            "자동 업데이트 확인",
            "실행 시 및 6시간마다 GitHub Releases에서 새 버전을 확인해요.",
            self.cb.on_set_auto_update_check,
            f_body,
            f_sub,
        )

        card_info = self._make_card(page, "앱 정보", f_section)
        info_row = tk.Frame(card_info, bg=pal["card"])
        info_row.pack(fill="x")
        self._update_status_lbl = tk.Label(
            info_row,
            text="",
            font=f_body,
            bg=pal["card"],
            fg=pal["fg"],
            anchor="w",
            justify="left",
        )
        self._update_status_lbl.pack(fill="x", pady=(0, 8))

        btn_row = tk.Frame(card_info, bg=pal["card"])
        btn_row.pack(fill="x")
        check_btn = self._make_action_button(
            btn_row,
            "⬆ 업데이트 확인",
            f_body,
            self.cb.on_check_updates,
            padx=12,
            pady=7,
        )
        check_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._update_action_btn = self._make_action_button(
            btn_row,
            "새 버전 설치",
            f_body,
            self.cb.on_start_update,
            accent=True,
            padx=12,
            pady=7,
        )
        # 새 버전이 있을 때만 표시

        card_diag = self._make_card(page, "문제 해결", f_section)
        tk.Label(
            card_diag,
            text="사용량이 안 보이거나 오류가 뜰 때, 진단 정보를 복사해 GitHub 이슈에 붙여 넣어 주세요.\n"
            "토큰, 계정 식별자, 사용자 폴더 이름은 가려져요.",
            font=f_sub,
            bg=pal["card"],
            fg=pal["muted"],
            anchor="w",
            justify="left",
            wraplength=360,
        ).pack(fill="x", pady=(0, 8))
        diag_row = tk.Frame(card_diag, bg=pal["card"])
        diag_row.pack(fill="x")
        copy_btn = self._make_action_button(
            diag_row,
            "⧉ 진단 정보 복사",
            f_body,
            self.cb.on_copy_diagnostics,
            padx=12,
            pady=7,
        )
        copy_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        log_btn = self._make_action_button(
            diag_row,
            "로그 폴더 열기",
            f_body,
            self.cb.on_open_log_folder,
            padx=12,
            pady=7,
        )
        log_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        return page

    # ---------------- 탭 전환 & 동기화 ----------------
    def _select_tab(self, key: str) -> None:
        if key not in self._pages:
            return
        self._active_tab = key
        pal = self.pal
        for tab_key, page in self._pages.items():
            if tab_key == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for tab_key, (row, ic_lbl, tx_lbl) in self._nav_buttons.items():
            active = tab_key == key
            bg = pal["accent_bg"] if active else pal["sidebar"]
            fg = pal["accent"] if active else pal["muted"]
            row.configure(bg=bg)
            ic_lbl.configure(bg=bg, fg=fg)
            tx_lbl.configure(bg=bg, fg=pal["fg"] if active else pal["muted"])
        if self._content_canvas is not None:
            self._content_canvas.yview_moveto(0.0)
        if self._refresh_scroll is not None:
            self._win.after_idle(self._refresh_scroll) if self._win is not None else None

    def _fire(self, fn: Callable, *args) -> None:
        if self._syncing:
            return
        fn(*args)
        # 콜백이 상태를 거부하거나 보정했을 수 있으므로 즉시 최신 상태로 맞춘다.
        self.sync()

    def _on_alpha_slider(self, pct: int, save: bool) -> None:
        if self._alpha_val_lbl is not None:
            self._alpha_val_lbl.configure(text=f"{pct}%")
        if self._syncing:
            return
        self.cb.on_set_alpha_pct(pct, save)

    def _on_scale_slider(self, pct: int, save: bool) -> None:
        if self._scale_val_lbl is not None:
            self._scale_val_lbl.configure(text=f"{pct}%")
        if self._syncing:
            return
        self.cb.on_set_scale_pct(pct, save)

    def sync(self) -> None:
        """위젯의 최신 설정 상태를 열린 설정 창 UI에 반영한다."""
        if not self.is_open:
            return
        snap = self.cb.get_snapshot()
        self._syncing = True
        try:
            self._switches["compact"].set_state(snap.compact, enabled=True)
            self._switches["show_pace"].set_state(snap.show_pace, enabled=True)
            self._switches["usage_tracking"].set_state(snap.usage_tracking, enabled=True)
            self._switches["show_antigravity"].set_state(snap.show_antigravity, enabled=True)
            self._switches["transparent_bg"].set_state(snap.transparent_bg, enabled=snap.transparent_bg_supported)
            if "adaptive_compact_color" in self._switches:
                self._switches["adaptive_compact_color"].set_state(
                    snap.adaptive_compact_color, enabled=snap.transparent_bg_supported
                )
            self._switches["track_wave"].set_state(snap.track_wave, enabled=snap.usage_tracking)
            self._switches["track_gain"].set_state(snap.track_gain, enabled=snap.usage_tracking)
            for sec, pill in self._track_fast_pills.items():
                self._paint_pill(pill, selected=(snap.tracking_fast_sec == sec), enabled=snap.usage_tracking)
            if self._track_status_lbl is not None:
                if not snap.usage_tracking:
                    status = "꺼져 있어요. 항상 수집 주기대로만 확인해요."
                elif snap.tracking_now:
                    status = "지금 추적 중: " + ", ".join(snap.tracking_now)
                else:
                    status = "지금 추적 중인 서비스가 없어요. 내장 브라우저는 30초보다 짧게 확인하지 않아요."
                self._track_status_lbl.configure(text=status)
            self._switches["topmost"].set_state(snap.topmost, enabled=True)
            self._switches["autostart"].set_state(snap.autostart_enabled, enabled=snap.autostart_supported)
            self._switches["auto_update_check"].set_state(snap.auto_update_check, enabled=True)

            alpha_pct = clamp_alpha_percent(snap.alpha_pct)
            if self._alpha_slider is not None:
                self._alpha_slider.set_value(alpha_pct)
            if self._alpha_val_lbl is not None:
                self._alpha_val_lbl.configure(text=f"{alpha_pct}%")

            scale_pct = clamp_scale_percent(snap.scale_pct)
            if self._scale_slider is not None:
                self._scale_slider.set_value(scale_pct)
            if self._scale_val_lbl is not None:
                self._scale_val_lbl.configure(text=f"{scale_pct}%")
            if self._scale_title_lbl is not None:
                self._scale_title_lbl.configure(text=snap.scale_mode_label)

            for f_key, pill in self._font_pills.items():
                self._paint_pill(pill, selected=(snap.font == f_key))

            for s_key, pill in self._web_source_pills.items():
                self._paint_pill(pill, selected=(snap.web_source == s_key))

            if self._browser_hint_lbl is not None:
                if not snap.browser_available:
                    self._browser_hint_lbl.configure(
                        text="현재 실행 환경에 pywebview가 없어요. Windows 배포판(.exe)에는 기본 포함되어 있습니다.",
                        fg=self.pal["danger"],
                    )
                elif snap.web_source == "browser":
                    self._browser_hint_lbl.configure(
                        text=f"내장 브라우저가 {snap.browser_interval_min}분마다 보이지 않게 사용량을 가져와요.",
                        fg=self.pal["muted"],
                    )
                else:
                    self._browser_hint_lbl.configure(
                        text="현재 크롬 확장에서 사용량을 받고 있어요. 내장 브라우저로 바꾸면 크롬 없이 동작해요.",
                        fg=self.pal["muted"],
                    )

            for mins, pill in self._interval_pills.items():
                self._paint_pill(pill, selected=(snap.browser_interval_min == mins))

            for c_key, pill in self._claude_source_pills.items():
                self._paint_pill(pill, selected=(snap.claude_source == c_key))

            if self._version_lbl is not None:
                self._version_lbl.configure(text=f"v{snap.version}")

            if self._update_status_lbl is not None:
                if snap.update_available_version:
                    self._update_status_lbl.configure(
                        text=f"현재 버전: v{snap.version}  →  새 버전 v{snap.update_available_version} 사용 가능",
                        fg=self.pal["accent"],
                    )
                else:
                    self._update_status_lbl.configure(
                        text=f"현재 버전: v{snap.version}",
                        fg=self.pal["fg"],
                    )

            if self._update_action_btn is not None:
                if snap.update_available_version:
                    self._update_action_btn.configure(text=f"v{snap.update_available_version} 업데이트")
                    if not self._update_action_btn.winfo_ismapped():
                        self._update_action_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
                elif self._update_action_btn.winfo_ismapped():
                    self._update_action_btn.pack_forget()
        finally:
            self._syncing = False
