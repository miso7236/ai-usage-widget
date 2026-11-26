"""AI 구독 사용량 위젯 (Windows).

화면 구석에 항상 위에 떠 있는 작은 창을 띄우고,
크롬 확장이 http://127.0.0.1:47823/update 로 보내는 사용량을 표시한다.

실행:  pythonw widget.py          (콘솔 창 없이)
       python widget.py --demo    (확장 없이 예시 데이터로 화면 확인)
조작:  드래그로 이동, 오른쪽 클릭으로 메뉴(항상 위, 투명도, 업데이트, 종료)
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import webbrowser
from pathlib import Path
from tkinter import messagebox

import antigravity
import autostart
import browser_worker
import claude_oauth
import diagnostics
import layered
import local_token
import tracking
import tray
import updater
from adaptive_color import AdaptiveColorController
from context_menu import MaterialContextMenu, MenuItem
from layout import CompactItem, Gauge, ProviderBlock, Scene, Style, WindowRow, argb, layout_compact, layout_detail
from screen_sampler import AsyncScreenSampler
from server import start_server
from settings_window import (
    ServiceStatus,
    SettingsCallbacks,
    SettingsSnapshot,
    SettingsWindow,
    clamp_alpha_percent,
    clamp_scale_percent,
    normalize_browser_interval,
    normalize_track_fast_sec,
)
from version import __version__
from state import (
    FONT_PRESETS,
    POSITION_PRESETS,
    Rect,
    Store,
    clamp_position,
    clamp_scale,
    compact_five_hour_summary,
    compact_gauges,
    compact_peak_labels,
    compact_summary,
    current_monitor_rect,
    demo_payload,
    format_ago,
    format_clock,
    format_eta,
    format_reset_compact,
    level,
    load_config,
    pace_level,
    pick_font_family,
    point_in_rect,
    preset_position,
    save_config,
    scale_from_drag,
    scaled_metrics,
    window_pace_percent,
)

TRANSPARENT_KEY = "#010101"  # Windows -transparentcolor 크로마키 (PNG처럼 완전 투명 처리)
GOOGLE_DOTS = ("#4285f4", "#ea4335", "#fbbc04", "#34a853")
COLORS = {
    "bg": "#202124",
    "grip_bg": "#202124",
    "border": "#3c4043",
    "fg": "#e8eaed",
    "muted": "#9aa0a6",
    "bar_bg": "#3c4043",
    "ok": "#81c995",
    "warn": "#fdd663",
    "danger": "#f28b82",
    "error": "#f28b82",
    "accent": "#8ab4f8",
}
COLORS_TRANSPARENT = {
    "bg": TRANSPARENT_KEY,
    "grip_bg": TRANSPARENT_KEY,
    "border": TRANSPARENT_KEY,
    "fg": "#ffffff",
    "muted": "#dadce0",
    "bar_bg": "#3c4043",
    "ok": "#81c995",
    "warn": "#fdd663",
    "danger": "#f28b82",
    "error": "#f28b82",
    "accent": "#8ab4f8",
}
UPDATE_CHECK_MS = 6 * 60 * 60 * 1000  # 6시간마다 업데이트 확인
ANTIGRAVITY_POLL_MS = 60_000  # 1분마다 로컬 Antigravity 용량 확인
TRACK_BROWSER_FAST_SEC = 30.0  # 내장 브라우저는 조회마다 프로세스를 띄우므로 추적 중에도 30초까지만
TRACK_ANIM_MS = 66  # 추적 물결 애니메이션 한 장면 간격 (초당 약 15장)
TRACK_ANIM_CYCLE_SEC = 2.4  # 물결이 한 번 퍼지는 데 걸리는 시간
ADAPTIVE_POLL_MS = 250  # 컴팩트 + 투명 배경 주변색 감지 주기 (초경량 GDI 띠 캡처)
ADAPTIVE_ANIM_MS = 33  # ~30fps 부드러운 색 보간 간격
ADAPTIVE_RESULT_POLL_MS = 25  # 백그라운드 캡처 결과 회수 간격
ADAPTIVE_DRAG_THROTTLE_S = 0.09  # 드래그 중 샘플 요청 최소 간격
FONT = ("Malgun Gothic", 8)
FONT_BOLD = ("Malgun Gothic", 8, "bold")
FONT_SMALL = ("Malgun Gothic", 7)
BAR_W, BAR_H = 54, 5


def enable_dpi_awareness() -> None:
    """고해상도 모니터에서 글자가 흐리게 보이지 않도록 한다 (Windows 전용)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def get_monitor_rects(root: tk.Tk) -> tuple[list[Rect], Rect]:
    """연결된 모든 모니터의 작업 영역 목록과 주 모니터 영역을 반환한다."""
    primary: Rect = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
    if sys.platform != "win32":
        return [primary], primary
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        monitors: list[Rect] = []
        found_primary: Rect | None = None
        user32 = ctypes.windll.user32
        MONITORINFOF_PRIMARY = 1

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(RECT),
            wintypes.LPARAM,
        )

        def _cb(hmon, _hdc, _lprect, _lparam):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcWork
                rect: Rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                monitors.append(rect)
                if info.dwFlags & MONITORINFOF_PRIMARY:
                    nonlocal found_primary
                    found_primary = rect
            return True

        user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
        if monitors:
            return monitors, found_primary or monitors[0]

        # EnumDisplayMonitors가 비어 있으면 가상 화면 전체 영역으로 폴백
        vx = user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        vw = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        vh = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if vw > 0 and vh > 0:
            return [(vx, vy, vx + vw, vy + vh)], primary
    except Exception:
        pass
    return [primary], primary


def _screen_locked() -> bool:
    """화면이 잠겨 있으면 True. 알 수 없으면 잠기지 않은 것으로 본다."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        desktop = user32.OpenInputDesktop(0, False, 0x0100)  # 잠금 화면의 데스크톱은 일반 프로그램이 열 수 없다
        if not desktop:
            return True
        user32.CloseDesktop(desktop)
    except Exception:
        return False
    return False


def _animations_enabled() -> bool:
    """Windows 설정의 '애니메이션 효과'가 켜져 있으면 True."""
    if sys.platform != "win32":
        return False
    try:
        enabled = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)  # SPI_GETCLIENTAREAANIMATION
        return bool(enabled.value) if ok else True
    except Exception:
        return True


class UsageWidget:
    def __init__(self, root: tk.Tk, store: Store, config: dict, demo: bool = False) -> None:
        self.root = root
        self.store = store
        self.config = config
        self._demo = demo
        self._ag_fetcher = antigravity.AntigravityFetcher()
        self._ag_busy = False
        self._claude_oauth_fetcher = claude_oauth.ClaudeOAuthFetcher()
        self._claude_oauth_busy = False
        self._browser_collector = browser_worker.BrowserCollector()
        self._browser_busy = False
        self._browser_full_at = 0.0  # 내장 브라우저로 전체 서비스를 마지막으로 조회한 시각
        self._tracker = tracking.Tracker()
        self._poll_timers: dict[str, str] = {}
        self._track_anim_id: str | None = None
        self._provider_log = diagnostics.ProviderChangeLogger()
        self.store.set_web_source(self._web_source())
        self.shown_version = -1
        self._drag = (0, 0)
        self.events: queue.Queue = queue.Queue()  # 작업 스레드 → 화면 스레드
        self.update_info: updater.ReleaseInfo | None = None
        self.update_busy = False
        self.visible = True
        self.click_through = False
        self._dragging = False
        self._resizing = False
        self._resize_origin = (0, 0, 1.0, 100, 40)
        self._pending_click_action = None
        self._last_backdrop_state = None
        self._last_render_key = None
        self._last_layout_key = None
        self._detail_refs: list = []
        self._compact_refs = None
        self._compact_slots: list[dict] = []
        self._adaptive = AdaptiveColorController()
        self._adaptive_sampler = AsyncScreenSampler()
        self._adaptive_poll_id: str | None = None
        self._adaptive_anim_id: str | None = None
        self._adaptive_res_id: str | None = None
        self._last_drag_sample_at: float = 0.0
        try:
            self._available_fonts = set(tkfont.families(root))
        except tk.TclError:
            self._available_fonts = set()
        # 픽셀별 투명 창에 직접 그리는 방식. 쓸 수 없으면 None이고 기존 tkinter 위젯으로 그린다.
        self._layered: layered.Renderer | None = self._create_layered_renderer()
        self._surface: layered.Surface | None = None
        self._scene: Scene | None = None
        self._shown_update_text = ""
        self._present_failed = False
        try:
            self._px_per_pt = float(root.winfo_fpixels("1p"))
        except tk.TclError:
            self._px_per_pt = 96 / 72
        self.backdrop: tk.Toplevel | None = self._create_backdrop()

        root.title("AI 사용량")
        root.overrideredirect(True)  # 제목 표시줄 없는 창
        root.attributes("-topmost", bool(config["topmost"]))
        if self._layered is None:
            # 직접 그리는 방식에서는 창 전체 투명도를 그릴 때 함께 넘긴다 (-alpha와 같이 쓸 수 없다).
            root.attributes("-alpha", float(config["alpha"]))

        pal = self._palette()
        root.configure(bg=pal["bg"])

        self.frame = tk.Frame(root, bg=pal["bg"], padx=6, pady=4, highlightthickness=1, highlightbackground=pal["border"])
        # 직접 그리는 방식에서는 tkinter 위젯을 화면에 올리지 않고, 창 크기와 마우스 입력만 맡는 빈 틀을 둔다.
        self._sizer: tk.Frame | None = None
        if self._layered is None:
            self.frame.pack(fill="both", expand=True)
        else:
            self._sizer = tk.Frame(root, width=1, height=1, bg="#000000")
            self._sizer.pack()
            self._sizer.bind("<ButtonPress-1>", self._on_backdrop_press)
            self._sizer.bind("<B1-Motion>", self._on_backdrop_motion)
            self._sizer.bind("<ButtonRelease-1>", self._on_backdrop_release)
            self._sizer.bind("<Double-Button-1>", self._on_layered_double_click)
            self._sizer.bind("<Motion>", self._on_hover_motion)
            self._sizer.bind("<MouseWheel>", self._on_wheel)
            self._sizer.bind("<Button-3>", self._show_menu)

        self.header = tk.Frame(self.frame, bg=pal["bg"])
        self.header.pack(fill="x")
        self.compact_btn = tk.Label(self.header, text="▾", font=FONT_SMALL, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=1, pady=0)
        self.compact_btn.pack(side="left", padx=(0, 3))
        self.compact_btn.bind("<Button-1>", lambda e: self._toggle_compact())
        self.brand_canvas = tk.Canvas(self.header, width=22, height=6, bg=pal["bg"], highlightthickness=0, bd=0)
        self.brand_canvas.pack(side="left", padx=(0, 4))
        self.brand_canvas.bind("<Double-Button-1>", lambda e: self._toggle_compact())
        self.title_label = tk.Label(self.header, text="AI", font=FONT_BOLD, bg=pal["bg"], fg=pal["fg"], padx=0, pady=0)
        self.title_label.pack(side="left")
        self.title_label.bind("<Double-Button-1>", lambda e: self._toggle_compact())
        self.update_label = tk.Label(self.header, text="", font=FONT_SMALL, bg=pal["bg"], fg=pal["accent"], cursor="hand2", padx=0, pady=0)
        self.update_label.pack(side="left", padx=(4, 0))
        self.update_label.bind("<Button-1>", lambda e: self.start_update())
        close = tk.Label(self.header, text="×", font=FONT_BOLD, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=1, pady=0)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.quit())
        self.settings_btn = tk.Label(self.header, text="⚙", font=FONT_BOLD, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=1, pady=0)
        self.settings_btn.pack(side="right", padx=(0, 3))
        self.settings_btn.bind("<Button-1>", lambda e: self.open_settings())
        self.refresh_btn = tk.Label(self.header, text="↻", font=FONT_BOLD, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=1, pady=0)
        self.refresh_btn.pack(side="right", padx=(0, 3))
        self.refresh_btn.bind("<Button-1>", lambda e: self.request_refresh())
        self.clock = tk.Label(self.header, text="", font=FONT_SMALL, bg=pal["bg"], fg=pal["muted"], padx=0, pady=0)
        self.clock.pack(side="right", padx=(0, 5))
        self.close_button = close
        for btn in (self.compact_btn, self.refresh_btn, self.settings_btn, close):
            btn.bind("<Enter>", lambda e, b=btn: b.configure(fg=self._palette()["fg"]))
            btn.bind("<Leave>", lambda e, b=btn: b.configure(fg=self._palette()["muted"]))
        self.clickable_actions = self._base_clickable_actions()
        self.clickables = set(self.clickable_actions)

        self.divider = tk.Frame(self.frame, bg=pal["border"], height=1)
        self.body = tk.Frame(self.frame, bg=pal["bg"])
        self.body.pack(fill="both", expand=True, pady=(3, 0))

        self.tray = tray.SystemTray(
            tooltip="AI 사용량 위젯",
            on_toggle_visible=lambda: self.events.put(("tray_toggle_visible",)),
            on_toggle_click_through=lambda: self.events.put(("tray_toggle_click_through",)),
            on_toggle_compact=lambda: self.events.put(("tray_toggle_compact",)),
            on_toggle_transparent_bg=lambda: self.events.put(("tray_toggle_transparent_bg",)),
            on_refresh=lambda: self.events.put(("tray_refresh",)),
            on_open_settings=lambda: self.events.put(("tray_open_settings",)),
            on_quit=lambda: self.events.put(("tray_quit",)),
            get_state=lambda: {
                "visible": self.visible,
                "click_through": self.click_through,
                "compact": bool(self.config.get("compact", False)),
                "transparent_bg": self._is_transparent_bg(),
            },
        )
        self.tray.start()

        self.context_menu = MaterialContextMenu(self.root)
        self.settings_window = SettingsWindow(self.root, self._build_settings_callbacks())
        self._apply_background()
        self.render()
        self._place_initial()
        self._hide_from_taskbar(self.root)
        if self.visible:
            self.root.deiconify()
            self._hide_from_taskbar(self.root)
        self._sync_backdrop()
        self._tick_fast()
        self._tick_slow()
        self._sync_adaptive_loop(sample_now=True)
        if not self._demo:
            self._tick_antigravity()
            self._tick_claude_oauth()
            self._tick_browser()
        elif self._claude_source() == "oauth":
            self.store.set_local_provider(claude_oauth.demo_claude_oauth_state())
        self.root.after(5000, self._auto_update_check)

    # ---------------- 배경·테마 ----------------
    def _base_clickable_actions(self) -> dict[tk.Widget, object]:
        return {
            self.close_button: self.quit,
            self.settings_btn: self.open_settings,
            self.refresh_btn: self.request_refresh,
            self.update_label: self.start_update,
            self.compact_btn: self._toggle_compact,
        }

    def _create_layered_renderer(self) -> layered.Renderer | None:
        if not layered.is_supported() or str(self.config.get("renderer", "layered")) != "layered":
            return None
        try:
            return layered.Renderer()
        except Exception as e:
            diagnostics.log.warning("직접 그리기를 쓸 수 없어 기존 방식으로 그립니다: %s", e)
            return None

    def _layered_actions(self) -> dict[str, object]:
        return {
            "compact": self._toggle_compact,
            "refresh": self.request_refresh,
            "settings": self.open_settings,
            "close": self.quit,
            "update": self.start_update,
        }

    def _on_layered_double_click(self, event) -> None:
        action = self._hit_clickable(event.x_root, event.y_root)
        if action is not None:
            self._pending_click_action = action  # 버튼 위 두 번째 클릭은 버튼 동작으로 처리
        elif not self._is_resize_corner(event.x_root, event.y_root):
            self._toggle_compact()

    def _hit_clickable(self, x_root: int, y_root: int):
        """투명 배경 뒤 레이어가 클릭을 받았을 때, 그 위 버튼 영역이면 해당 동작을 반환한다."""
        if self._layered is not None:
            if self._scene is None:
                return None
            lx, ly = x_root - self.root.winfo_rootx(), y_root - self.root.winfo_rooty()
            for name, (x0, y0, x1, y1) in self._scene.hits.items():
                if x0 <= lx <= x1 and y0 <= ly <= y1:
                    return self._layered_actions().get(name)
            return None
        for widget, action in list(self.clickable_actions.items()):
            try:
                if not widget.winfo_exists() or not widget.winfo_ismapped():
                    continue
                if widget is self.update_label and not widget.cget("text"):
                    continue
                wx, wy = widget.winfo_rootx(), widget.winfo_rooty()
                ww, wh = widget.winfo_width(), widget.winfo_height()
                if ww <= 1 or wh <= 1:
                    continue
                if point_in_rect(x_root, y_root, (wx, wy, wx + ww, wy + wh), pad=2):
                    return action
            except tk.TclError:
                continue
        return None

    def _create_backdrop(self) -> tk.Toplevel | None:
        """투명 배경(-transparentcolor)에서도 위젯 영역 전체의 클릭·우클릭·드래그·리사이즈를 받는 투명 레이어."""
        if sys.platform != "win32" or self._layered is not None:
            return None  # 직접 그리는 방식은 배경에 최소 불투명도를 깔아 클릭을 받으므로 따로 필요 없다
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.configure(bg="#000000")
        win.attributes("-alpha", 0.01)  # 육안으로는 완전히 투명하지만 Windows가 클릭을 전달한다
        win.attributes("-topmost", bool(self.config["topmost"]))
        win.bind("<ButtonPress-1>", self._on_backdrop_press)
        win.bind("<B1-Motion>", self._on_backdrop_motion)
        win.bind("<ButtonRelease-1>", self._on_backdrop_release)
        win.bind("<Motion>", self._on_hover_motion)
        win.bind("<MouseWheel>", self._on_wheel)
        win.bind("<Button-3>", self._show_menu)
        win.bind("<Double-Button-1>", lambda e: self._toggle_compact())
        return win

    def _is_resize_corner(self, x_root: int, y_root: int) -> bool:
        """오른쪽 아래 모서리(12x12) 영역인지 확인한다."""
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        rw = max(1, self.root.winfo_width())
        rh = max(1, self.root.winfo_height())
        return (rx + rw - 12) <= x_root <= (rx + rw + 2) and (ry + rh - 12) <= y_root <= (ry + rh + 2)

    def _on_hover_motion(self, event) -> None:
        if self._is_resize_corner(event.x_root, event.y_root):
            cursor = "size_nw_se"
        elif self._layered is not None and self._hit_clickable(event.x_root, event.y_root) is not None:
            cursor = "hand2"
        else:
            cursor = ""
        try:
            event.widget.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_backdrop_press(self, event) -> None:
        if self.context_menu.is_open:
            self.context_menu.close()
        if self._is_resize_corner(event.x_root, event.y_root):
            self._pending_click_action = None
            self._start_resize(event)
            return
        action = self._hit_clickable(event.x_root, event.y_root)
        if action is not None:
            self._pending_click_action = action
            self._dragging = False
            self._resizing = False
            return
        self._pending_click_action = None
        self._start_drag(event)

    def _on_backdrop_motion(self, event) -> None:
        if self._pending_click_action is not None:
            return
        if self._resizing:
            self._on_resize(event)
        elif self._dragging:
            self._on_drag(event)

    def _on_backdrop_release(self, event) -> None:
        action = self._pending_click_action
        self._pending_click_action = None
        if action is not None:
            action()
            return
        if self._resizing:
            self._end_resize(event)
        elif self._dragging:
            self._end_drag(event)

    def _hide_from_taskbar(self, win: tk.Misc | None) -> None:
        if win is None or sys.platform != "win32":
            return
        try:
            win.update_idletasks()
            tray.hide_from_taskbar(win.winfo_id())
        except Exception:
            pass

    def _sync_backdrop(self) -> None:
        if self.backdrop is None:
            return
        need = self._is_transparent_bg() and self.visible and not self.click_through
        if not need:
            if self._last_backdrop_state != (False,):
                self.backdrop.withdraw()
                self._last_backdrop_state = (False,)
            return
        self.root.update_idletasks()
        w = max(1, self.root.winfo_reqwidth())
        h = max(1, self.root.winfo_reqheight())
        x, y = self.root.winfo_x(), self.root.winfo_y()
        topmost = bool(self.config["topmost"])
        state = (True, w, h, x, y, topmost)
        if state == self._last_backdrop_state and self.backdrop.state() != "withdrawn":
            return
        self._last_backdrop_state = state
        self.backdrop.attributes("-topmost", topmost)
        self.backdrop.geometry(f"{w}x{h}+{x}+{y}")
        if self.backdrop.state() == "withdrawn":
            self._hide_from_taskbar(self.backdrop)
            self.backdrop.deiconify()
            self._hide_from_taskbar(self.backdrop)
        self.root.lift(self.backdrop)

    def _is_transparent_bg(self) -> bool:
        return sys.platform == "win32" and bool(self.config.get("transparent_bg", True))

    def _adaptive_mode_enabled(self) -> bool:
        return (
            self._layered is None  # 직접 그리는 방식은 글자 외곽선으로 가독성을 확보한다
            and bool(self.config.get("compact", False))
            and self._is_transparent_bg()
            and bool(self.config.get("adaptive_compact_color", True))
        )

    def _adaptive_active(self) -> bool:
        return self._adaptive_mode_enabled() and self.visible

    def _chroma_key(self) -> str:
        return self._adaptive.chroma_key if self._adaptive_mode_enabled() else TRANSPARENT_KEY

    def _palette(self) -> dict[str, str]:
        if self._adaptive_mode_enabled():
            return self._adaptive.global_palette
        return COLORS_TRANSPARENT if self._is_transparent_bg() else COLORS

    def _apply_background(self) -> None:
        if self._layered is not None:
            return  # 배경은 render()가 그림에 포함해 그린다
        pal = self._palette()
        if sys.platform == "win32":
            try:
                self.root.attributes("-transparentcolor", self._chroma_key() if self._is_transparent_bg() else "")
            except tk.TclError:
                pass
        self.root.configure(bg=pal["bg"])
        border_w = 0 if self._is_transparent_bg() else 1
        self.frame.configure(bg=pal["bg"], highlightthickness=border_w, highlightbackground=pal["border"])
        self.header.configure(bg=pal["bg"])
        self.brand_canvas.configure(bg=pal["bg"])
        self.divider.configure(bg=pal["border"])
        self.body.configure(bg=pal["bg"])
        self.compact_btn.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self.title_label.configure(bg=pal["grip_bg"], fg=pal["fg"])
        self.update_label.configure(bg=pal["bg"], fg=pal["accent"])
        self.clock.configure(bg=pal["bg"], fg=pal["muted"])
        self.refresh_btn.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self.settings_btn.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self.close_button.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self._sync_backdrop()

    # ---------------- 컴팩트 + PNG 주변색 자동 보정 ----------------
    def _cancel_adaptive_timers(self) -> None:
        for attr in ("_adaptive_poll_id", "_adaptive_anim_id", "_adaptive_res_id"):
            tid = getattr(self, attr, None)
            if tid is not None:
                try:
                    self.root.after_cancel(tid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def _stop_adaptive_anim(self) -> None:
        if self._adaptive_anim_id is not None:
            try:
                self.root.after_cancel(self._adaptive_anim_id)
            except tk.TclError:
                pass
            self._adaptive_anim_id = None

    def _collect_compact_slot_ratios(self) -> list[tuple[float, float]]:
        slots = self._compact_slots
        if not slots:
            return []
        n = len(slots)
        try:
            root_x = self.root.winfo_rootx()
            root_w = self.root.winfo_width()
        except tk.TclError:
            root_x, root_w = 0, 0
        if root_w <= 1:
            return [(i / n, (i + 1) / n) for i in range(n)]
        ratios: list[tuple[float, float]] = []
        for i, slot in enumerate(slots):
            anchor = slot.get("anchor")
            try:
                if anchor is not None and anchor.winfo_exists():
                    aw = anchor.winfo_width()
                    if aw > 1:
                        ax = anchor.winfo_rootx()
                        r0 = max(0.0, min(1.0, (ax - root_x) / root_w))
                        r1 = max(r0, min(1.0, (ax + aw - root_x) / root_w))
                        ratios.append((r0, r1))
                        continue
            except tk.TclError:
                pass
            ratios.append((i / n, (i + 1) / n))
        return ratios

    def _apply_compact_adaptive_colors(self) -> None:
        if not self._adaptive_mode_enabled() or not self._compact_slots:
            return
        for slot_idx, slot in enumerate(self._compact_slots):
            for widget, role in slot["items"]:
                try:
                    if widget.winfo_exists():
                        widget.configure(fg=self._adaptive.color_for(slot_idx, role))
                except tk.TclError:
                    pass

    def _start_adaptive_anim(self) -> None:
        if self._adaptive_anim_id is not None or not self._adaptive_active():
            return
        self._adaptive_anim_id = self.root.after(ADAPTIVE_ANIM_MS, self._step_adaptive_anim)

    def _step_adaptive_anim(self) -> None:
        self._adaptive_anim_id = None
        if not self._adaptive_active():
            return
        _hex_map, still = self._adaptive.step_animation()
        self._apply_compact_adaptive_colors()
        if still:
            self._adaptive_anim_id = self.root.after(ADAPTIVE_ANIM_MS, self._step_adaptive_anim)

    def _apply_adaptive_samples(self, samples, *, immediate: bool = False) -> None:
        if not self._adaptive_active() or not samples:
            return
        upd = self._adaptive.apply_samples(samples, immediate=immediate)
        if upd.chroma_changed:
            self._apply_background()
            self.render()
        elif upd.colors_changed:
            self._apply_compact_adaptive_colors()
        if upd.needs_animation:
            self._start_adaptive_anim()

    def _request_adaptive_sample(self, *, immediate: bool = False) -> None:
        if not self._adaptive_active() or not self._compact_slots:
            return
        try:
            hwnd = self.root.winfo_id()
        except tk.TclError:
            return
        ratios = self._collect_compact_slot_ratios()
        if not ratios:
            return
        self._adaptive_sampler.request(hwnd, ratios, immediate=immediate)
        if self._adaptive_res_id is None:
            self._adaptive_res_id = self.root.after(ADAPTIVE_RESULT_POLL_MS, lambda: self._poll_adaptive_result(8))

    def _poll_adaptive_result(self, retries: int = 8) -> None:
        self._adaptive_res_id = None
        if not self._adaptive_active():
            return
        res = self._adaptive_sampler.poll_result()
        if res is not None:
            samples, immediate = res
            self._apply_adaptive_samples(samples, immediate=immediate)
            return
        if retries > 0:
            self._adaptive_res_id = self.root.after(
                ADAPTIVE_RESULT_POLL_MS, lambda: self._poll_adaptive_result(retries - 1)
            )

    def _tick_adaptive(self) -> None:
        self._adaptive_poll_id = None
        if not self._adaptive_active():
            return
        if not self._resizing and not self._dragging:
            self._request_adaptive_sample(immediate=False)
        self._adaptive_poll_id = self.root.after(ADAPTIVE_POLL_MS, self._tick_adaptive)

    def _sync_adaptive_loop(self, *, sample_now: bool = False) -> None:
        if self._adaptive_active():
            if self._adaptive_poll_id is None:
                self._adaptive_poll_id = self.root.after(ADAPTIVE_POLL_MS, self._tick_adaptive)
            if sample_now:
                self.root.after(60, lambda: self._request_adaptive_sample(immediate=False))
        else:
            self._cancel_adaptive_timers()
            if not self._adaptive_mode_enabled():
                self._adaptive.reset()

    def _set_adaptive_compact_color(self, value: bool) -> None:
        self.config["adaptive_compact_color"] = bool(value)
        save_config(self.config)
        self._sync_adaptive_loop(sample_now=True)
        self._apply_background()
        self.render()
        self._sync_settings()

    # ---------------- 메뉴 & 설정 창 ----------------
    def _dialog_parent(self):
        return self.settings_window.window or self.root

    def _settings_monitor(self) -> Rect:
        self.root.update_idletasks()
        w = max(1, self.root.winfo_reqwidth())
        h = max(1, self.root.winfo_reqheight())
        monitors, primary = get_monitor_rects(self.root)
        return current_monitor_rect(self.root.winfo_x(), self.root.winfo_y(), w, h, monitors, primary) or primary

    def _settings_snapshot(self) -> SettingsSnapshot:
        compact = bool(self.config.get("compact", False))
        return SettingsSnapshot(
            compact=compact,
            show_pace=bool(self.config.get("show_pace", True)),
            usage_tracking=self._tracking_enabled(),
            track_wave=bool(self.config.get("track_wave", True)),
            track_gain=bool(self.config.get("track_gain", True)),
            tracking_fast_sec=self._track_fast_sec(),
            tracking_now=self._tracking_names(),
            show_antigravity=bool(self.config.get("show_antigravity", True)),
            transparent_bg=self._is_transparent_bg(),
            transparent_bg_supported=(sys.platform == "win32"),
            adaptive_compact_color=bool(self.config.get("adaptive_compact_color", True)),
            topmost=bool(self.config.get("topmost", True)),
            alpha_pct=clamp_alpha_percent(float(self.config.get("alpha", 0.92)) * 100),
            scale_pct=clamp_scale_percent(self._current_scale() * 100),
            scale_mode_label="크기 (컴팩트 모드)" if compact else "크기 (일반 모드)",
            font=str(self.config.get("font") or "auto"),
            web_source=self._web_source(),
            browser_interval_min=normalize_browser_interval(self.config.get("browser_interval_min", 3)),
            browser_available=browser_worker.is_available(),
            claude_source=self._claude_source(),
            autostart_enabled=autostart.is_enabled(),
            autostart_supported=autostart.is_supported(),
            auto_update_check=bool(self.config.get("auto_update_check", True)),
            version=__version__,
            update_available_version=(self.update_info.version if self.update_info else None),
            adaptive_supported=self._layered is None,
        )

    def _build_settings_callbacks(self) -> SettingsCallbacks:
        return SettingsCallbacks(
            get_snapshot=self._settings_snapshot,
            get_monitor=self._settings_monitor,
            get_font_family=self._font_family,
            get_services=self._service_statuses,
            get_preview=self._settings_preview,
            on_set_compact=self._set_compact,
            on_set_show_pace=self._set_show_pace,
            on_set_usage_tracking=self._set_usage_tracking,
            on_set_track_wave=lambda v: self._set_display_flag("track_wave", v),
            on_set_track_gain=lambda v: self._set_display_flag("track_gain", v),
            on_set_tracking_fast_sec=self._set_tracking_fast_sec,
            on_set_show_antigravity=self._set_show_antigravity,
            on_set_transparent_bg=self._set_transparent_bg,
            on_set_adaptive_compact_color=self._set_adaptive_compact_color,
            on_set_topmost=self._set_topmost,
            on_set_alpha_pct=lambda pct, save: self._set_alpha(clamp_alpha_percent(pct) / 100.0, save=save),
            on_set_scale_pct=lambda pct, save: self._set_scale(clamp_scale_percent(pct) / 100.0, save=save),
            on_set_font=self._set_font,
            on_move_preset=self._move_to_preset,
            on_reset_position=self._reset_position,
            on_set_web_source=self._set_web_source,
            on_open_browser_login=self._open_browser_login,
            on_set_browser_interval=self._set_browser_interval,
            on_set_claude_source=self._set_claude_source,
            on_open_extension_folder=self.open_extension_folder,
            on_set_autostart=self._set_autostart,
            on_set_auto_update_check=self._set_auto_update_check,
            on_check_updates=lambda: self.check_updates(manual=True),
            on_start_update=self.start_update,
            on_copy_diagnostics=self.copy_diagnostics,
            on_open_log_folder=self.open_log_folder,
        )

    def open_settings(self, tab: str | None = None) -> None:
        self.context_menu.close()
        self.settings_window.open(tab=tab)

    def _service_statuses(self) -> list[ServiceStatus]:
        """설정 창의 서비스 쪽에 보여 줄 서비스별 상태."""
        now_s = time.time()
        tracked = self._tracker.tracking(now_s) if self._tracking_enabled() else frozenset()
        out = []
        for p in self.store.snapshot():
            if p.provider == "antigravity":
                source = "이 PC에서 직접 확인"
            elif p.provider == "claude" and self._claude_source() == "oauth":
                source = "로컬 OAuth"
            else:
                source = "내장 브라우저" if self._web_source() == "browser" else "크롬 확장"
            checked = f"{source} · {format_ago(p.fetched_at)} 확인"
            if p.error and not p.ok:
                tag = "로그인 필요" if "로그인" in p.error else "오류"
                out.append(ServiceStatus(p.name, f"{source} · {p.error}", tag, "error"))
            elif p.is_stale():
                out.append(ServiceStatus(p.name, checked, "오래됨", "warn"))
            elif p.provider in tracked:
                start = self._tracker.baseline(p.provider, now_s)
                label = compact_peak_labels(p)[1] or compact_peak_labels(p)[0]
                current = next((w.used_percent for w in p.windows if w.label == label), None)
                gain = round(current) - round(start[label]) if current is not None and label in start else 0
                out.append(ServiceStatus(p.name, checked, f"추적 중 +{gain}" if gain >= 1 else "추적 중", "track"))
            else:
                out.append(ServiceStatus(p.name, checked, "정상", "ok"))
        return out

    def _settings_preview(self) -> tuple[int, int, bytes, str] | None:
        """설정 창 미리보기용으로 지금 위젯 화면을 불투명한 바탕 위에 그린다."""
        if self._layered is None or self._scene is None:
            return None
        # 투명 배경은 밝은 바탕 위에 놓여야 글자 외곽선까지 보인다.
        bg = "#5f6368" if self._is_transparent_bg() else "#1b1c1f"
        try:
            rgb = self._layered.snapshot_rgb(self._scene, argb(bg))
        except OSError:
            return None
        return (self._scene.width, self._scene.height, rgb, bg)

    def _refresh_settings_live(self, services: bool = True) -> None:
        window = getattr(self, "settings_window", None)
        if window is None or not window.is_open:
            return
        if services:
            window.refresh_services()
        window.update_preview()

    def _sync_settings(self) -> None:
        if hasattr(self, "settings_window"):
            self.settings_window.sync()

    def _build_menu_items(self) -> list[MenuItem]:
        items: list[MenuItem] = [
            MenuItem(kind="command", label="지금 새로고침", icon="↻", command=self.request_refresh),
            MenuItem(kind="separator"),
            MenuItem(
                kind="check",
                label="컴팩트 모드",
                checked=bool(self.config.get("compact", False)),
                command=self._toggle_compact,
            ),
            MenuItem(
                kind="check",
                label="클릭 통과 (트레이에서 해제)",
                checked=self.click_through,
                enabled=self.tray.active,
                command=self._toggle_click_through,
            ),
        ]
        if self.tray.active:
            items.append(MenuItem(kind="command", label="트레이로 숨기기", icon="▾", command=self._toggle_visible))
        items.extend(
            [
                MenuItem(kind="separator"),
                MenuItem(kind="command", label="설정…", icon="⚙", command=self.open_settings),
                MenuItem(kind="separator"),
                MenuItem(kind="command", label="종료", icon="✕", danger=True, command=self.quit),
            ]
        )
        return items

    def _toggle_transparent_bg(self) -> None:
        self._set_transparent_bg(not bool(self.config.get("transparent_bg", True)))

    def _set_transparent_bg(self, value: bool) -> None:
        self.config["transparent_bg"] = bool(value)
        save_config(self.config)
        self._sync_adaptive_loop(sample_now=True)
        self._apply_background()
        self.render()
        self._sync_settings()

    def request_refresh(self) -> None:
        self.store.request_refresh()
        if not self._demo and self.config.get("show_antigravity", True):
            self._poll_antigravity()
        if not self._demo and self._claude_source() == "oauth":
            self._poll_claude_oauth(force=True)
        if not self._demo and self._web_source() == "browser":
            self._poll_browser()
        self.clock.config(text="갱신 요청…")

    def _web_source(self) -> str:
        return "browser" if self.config.get("web_source") == "browser" else "extension"

    def _browser_poll_ms(self) -> int:
        return normalize_browser_interval(self.config.get("browser_interval_min", 3)) * 60_000

    def _set_browser_interval(self, minutes: int) -> None:
        mins = normalize_browser_interval(minutes)
        self.config["browser_interval_min"] = mins
        save_config(self.config)
        for source in ("browser", "claude_oauth"):  # 수집 주기는 두 경로에 함께 적용된다
            if source in self._poll_timers:
                self._reschedule_source(source)
        self._sync_settings()

    # ---------------- 사용량 변동 추적 ----------------
    def _tracking_enabled(self) -> bool:
        return bool(self.config.get("usage_tracking", True))

    def _set_usage_tracking(self, value: bool) -> None:
        self.config["usage_tracking"] = bool(value)
        save_config(self.config)
        if not value:
            self._tracker.stop_all()
            for source in ("claude_oauth", "antigravity", "browser"):
                if source in self._poll_timers:
                    self._reschedule_source(source)
        self.render()
        self._sync_settings()

    def _track_fast_sec(self) -> int:
        return normalize_track_fast_sec(self.config.get("tracking_fast_sec", 15))

    def _set_tracking_fast_sec(self, seconds: int) -> None:
        self.config["tracking_fast_sec"] = normalize_track_fast_sec(seconds)
        save_config(self.config)
        for source in ("claude_oauth", "antigravity", "browser"):
            if source in self._poll_timers:
                self._reschedule_source(source)
        self._sync_settings()

    def _set_display_flag(self, key: str, value: bool) -> None:
        self.config[key] = bool(value)
        save_config(self.config)
        self.render()
        self._sync_settings()

    def _tracking_names(self) -> tuple[str, ...]:
        if not self._tracking_enabled():
            return ()
        tracked = self._tracker.tracking(time.time())
        return tuple(p.name for p in self.store.snapshot() if p.provider in tracked)

    def _schedule_poll(self, name: str, ms: int, fn) -> None:
        old = self._poll_timers.get(name)
        if old is not None:
            try:
                self.root.after_cancel(old)
            except tk.TclError:
                pass
        self._poll_timers[name] = self.root.after(max(1000, int(ms)), fn)

    def _next_poll_ms(self, providers: list[str], base_ms: int, min_fast_sec: float = 0.0) -> int:
        """추적 중인 서비스가 있으면 짧아진 간격을, 없으면 평소 간격을 돌려준다."""
        if not self._tracking_enabled() or not providers:
            return base_ms
        now = time.time()
        fast_sec = max(float(self._track_fast_sec()), min_fast_sec)
        return int(min(self._tracker.interval(p, base_ms / 1000, now, fast_sec=fast_sec) for p in providers) * 1000)

    def _browser_tracked_ids(self) -> list[str]:
        # Gemini는 화면을 읽는 방식이라 자주 조회하지 않는다.
        return ["chatgpt"] if self._claude_source() == "oauth" else ["claude", "chatgpt"]

    def _browser_next_ms(self) -> int:
        base_ms = self._browser_poll_ms()
        until_full = base_ms - int((time.time() - self._browser_full_at) * 1000)
        fast_ms = self._next_poll_ms(self._browser_tracked_ids(), base_ms, TRACK_BROWSER_FAST_SEC)
        return max(5000, min(fast_ms, until_full))

    def _source_base_ms(self, source: str) -> int:
        if source == "antigravity":
            return ANTIGRAVITY_POLL_MS
        return self._browser_poll_ms()

    def _reschedule_source(self, source: str) -> None:
        if source == "claude_oauth":
            self._schedule_poll(source, self._next_poll_ms(["claude"], self._browser_poll_ms()), self._tick_claude_oauth)
        elif source == "antigravity":
            self._schedule_poll(source, self._next_poll_ms(["antigravity"], ANTIGRAVITY_POLL_MS), self._tick_antigravity)
        elif source == "browser":
            self._schedule_poll(source, self._browser_next_ms(), self._tick_browser)

    def _on_usage_polled(self, source: str, provider_ids: list[str], states) -> None:
        """위젯이 직접 조회한 결과를 추적기에 알리고, 다음 조회 시각을 다시 잡는다. states가 None이면 조회 실패."""
        if not self._tracking_enabled():
            return
        now = time.time()
        base_sec = self._source_base_ms(source) / 1000
        tracked_ids = set(self._browser_tracked_ids()) if source == "browser" else None
        allow_start = not _screen_locked()
        before = self._tracker.tracking(now)
        by_id = {st.provider: st for st in (states or [])}
        for provider in provider_ids:
            if tracked_ids is not None and provider not in tracked_ids:
                continue
            st = by_id.get(provider)
            values = {w.label: float(w.used_percent) for w in st.windows} if st is not None and st.ok and st.windows else None
            if self._tracker.observe(provider, values, now, base_sec=base_sec, allow_start=allow_start):
                diagnostics.log.info("사용량 변동 추적 시작: %s", provider)
        if source in self._poll_timers:
            self._reschedule_source(source)
        if self._tracker.tracking(now) != before:
            self.render()
            self._sync_settings()
        self._refresh_settings_live()

    def _ensure_browser_available(self) -> bool:
        if browser_worker.is_available():
            return True
        messagebox.showinfo(
            "내장 브라우저 (WebView2)",
            "현재 실행 환경에 pywebview가 설치되어 있지 않아요.\n\n"
            "Windows 배포판(.exe)에는 기본 포함되어 있으며,\n"
            "소스 코드로 실행 중이라면 `pip install pywebview`를 설치해 주세요.",
            parent=self._dialog_parent(),
        )
        return False

    def _set_web_source(self, source: str) -> None:
        normalized = "browser" if source == "browser" else "extension"
        if normalized == "browser" and not self._demo and not self._ensure_browser_available():
            return
        if self._web_source() == normalized:
            return
        self.config["web_source"] = normalized
        save_config(self.config)
        self.store.set_web_source(normalized)
        if normalized == "browser" and not self._demo:
            self._poll_browser()
        self.render()
        self._sync_settings()

    def _open_browser_login(self, provider: str) -> None:
        if not self._ensure_browser_available():
            return
        started = self._browser_collector.open_login(
            provider,
            on_closed=lambda: self.events.put(("browser_login_closed", provider, False)),
        )
        if not started:
            messagebox.showinfo(
                "내장 브라우저 로그인",
                "이미 로그인 창이 열려 있어요. 기존 로그인 창을 먼저 닫아 주세요.",
                parent=self._dialog_parent(),
            )

    def _poll_browser(self, providers: list[str] | None = None) -> None:
        if self._browser_busy or self._demo or not browser_worker.is_available():
            return
        if providers is None:
            providers = (
                ["chatgpt", "gemini"]
                if self._claude_source() == "oauth"
                else ["claude", "chatgpt", "gemini"]
            )
        if not providers:
            return
        self._browser_busy = True

        def work():
            states = None
            try:
                items = self._browser_collector.fetch_providers(providers)
                if items is not None:
                    self.store.update_browser_providers(items)
                    states = [p for p in self.store.snapshot() if p.provider in providers]
                self.events.put(("usage_polled", ("browser", list(providers), states)))
            except Exception:
                diagnostics.log.exception("내장 브라우저 수집 중 예외")
                self.events.put(("usage_polled", ("browser", list(providers), None)))
            finally:
                self._browser_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _tick_browser(self) -> None:
        if self._web_source() == "browser" and not self._browser_busy:
            now = time.time()
            if now - self._browser_full_at >= self._browser_poll_ms() / 1000 - 1:
                self._browser_full_at = now
                self._poll_browser()
            elif self._tracking_enabled():
                tracked = [p for p in self._browser_tracked_ids() if self._tracker.is_tracking(p, now)]
                if tracked:
                    self._poll_browser(tracked)
        self._schedule_poll("browser", self._browser_next_ms(), self._tick_browser)

    def _claude_source(self) -> str:
        return "oauth" if self.config.get("claude_source") == "oauth" else "web"

    def _set_claude_source(self, source: str) -> None:
        normalized = "oauth" if source == "oauth" else "web"
        if self._claude_source() == normalized:
            return
        self.config["claude_source"] = normalized
        save_config(self.config)
        if normalized == "web":
            self.store.remove_local_provider("claude")
            if not self._demo and self._web_source() == "browser":
                self._poll_browser(["claude"])
            self.render()
        elif self._demo:
            self.store.set_local_provider(claude_oauth.demo_claude_oauth_state())
            self.render()
        else:
            self._poll_claude_oauth(force=True)
        self._sync_settings()

    def _poll_claude_oauth(self, force: bool = False) -> None:
        if self._claude_oauth_busy or self._claude_source() != "oauth":
            return
        self._claude_oauth_busy = True
        custom_path = str(self.config.get("claude_oauth_path") or "").strip() or None

        def work():
            try:
                state = self._claude_oauth_fetcher.fetch(force=force, custom_path=custom_path)
                if self._claude_source() == "oauth":
                    self.store.set_local_provider(state)
                    self.events.put(("usage_polled", ("claude_oauth", ["claude"], [state])))
            finally:
                self._claude_oauth_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _tick_claude_oauth(self) -> None:
        if self._claude_source() == "oauth":
            self._poll_claude_oauth(force=False)
        self._schedule_poll(
            "claude_oauth", self._next_poll_ms(["claude"], self._browser_poll_ms()), self._tick_claude_oauth
        )

    def _toggle_pace(self) -> None:
        self._set_show_pace(not bool(self.config.get("show_pace", True)))

    def _set_show_pace(self, value: bool) -> None:
        self.config["show_pace"] = bool(value)
        save_config(self.config)
        self.render()
        self._sync_settings()

    def _toggle_antigravity(self) -> None:
        self._set_show_antigravity(not bool(self.config.get("show_antigravity", True)))

    def _set_show_antigravity(self, value: bool) -> None:
        enabled = bool(value)
        self.config["show_antigravity"] = enabled
        save_config(self.config)
        if not enabled:
            self.store.remove_local_provider("antigravity")
            self.render()
        elif not self._demo:
            self._poll_antigravity()
        self._sync_settings()

    def _poll_antigravity(self) -> None:
        if self._ag_busy or not self.config.get("show_antigravity", True):
            return
        self._ag_busy = True

        def work():
            try:
                state = self._ag_fetcher.fetch()
                if self.config.get("show_antigravity", True):
                    self.store.set_local_provider(state)
                    self.events.put(("usage_polled", ("antigravity", [state.provider], [state])))
            except Exception:
                diagnostics.log.exception("Antigravity 조회 중 예외")
            finally:
                self._ag_busy = False

        threading.Thread(target=work, daemon=True).start()

    def _tick_antigravity(self) -> None:
        if self.config.get("show_antigravity", True):
            self._poll_antigravity()
        self._schedule_poll(
            "antigravity", self._next_poll_ms(["antigravity"], ANTIGRAVITY_POLL_MS), self._tick_antigravity
        )

    def _toggle_visible(self) -> None:
        if self.visible:
            self.root.withdraw()
            self.visible = False
        else:
            self.root.deiconify()
            self._hide_from_taskbar(self.root)
            self.root.attributes("-topmost", bool(self.config["topmost"]))
            self.visible = True
            self._present()
            self.render()  # 숨긴 동안 멈춘 추적 물결을 다시 움직인다
        self._sync_backdrop()
        self._sync_adaptive_loop(sample_now=self.visible)

    def _toggle_click_through(self) -> None:
        self._set_click_through(not self.click_through)

    def _set_click_through(self, enabled: bool) -> None:
        ok = tray.set_click_through(self.root.winfo_id(), bool(enabled))
        self.click_through = bool(enabled) if ok else False
        self._sync_backdrop()

    def _toggle_autostart(self) -> None:
        self._set_autostart(not autostart.is_enabled())

    def _set_autostart(self, want: bool) -> None:
        try:
            autostart.set_enabled(bool(want))
        except OSError as e:
            messagebox.showwarning(
                "AI 사용량 위젯",
                f"자동 실행 설정을 바꾸지 못했어요.\n{e}",
                parent=self._dialog_parent(),
            )
        self._sync_settings()

    def _set_auto_update_check(self, value: bool) -> None:
        self.config["auto_update_check"] = bool(value)
        save_config(self.config)
        self._sync_settings()

    def _toggle_compact(self) -> None:
        self._set_compact(not bool(self.config.get("compact", False)))

    def _set_compact(self, value: bool) -> None:
        self.config["compact"] = bool(value)
        save_config(self.config)
        self._sync_adaptive_loop(sample_now=True)
        self._apply_background()
        self.render()
        self._sync_settings()

    def _toggle_topmost(self) -> None:
        self._set_topmost(not bool(self.config["topmost"]))

    def _set_topmost(self, value: bool) -> None:
        self.config["topmost"] = bool(value)
        self.root.attributes("-topmost", self.config["topmost"])
        self._present()
        save_config(self.config)
        self._sync_backdrop()
        self._sync_settings()

    def _set_alpha(self, alpha: float, save: bool = True) -> None:
        self.config["alpha"] = alpha
        if self._layered is None:
            self.root.attributes("-alpha", alpha)
        else:
            self._present()
        if save:
            save_config(self.config)
        self._sync_settings()

    def _scale_key(self) -> str:
        return "compact_scale" if self.config.get("compact", False) else "scale"

    def _current_scale(self) -> float:
        return clamp_scale(self.config.get(self._scale_key(), 1.0))

    def _set_scale(self, value: float, save: bool = True) -> None:
        new_scale = clamp_scale(value)
        key = self._scale_key()
        if new_scale == clamp_scale(self.config.get(key, 1.0)):
            if save:
                save_config(self.config)
            return
        self.config[key] = new_scale
        if save:
            save_config(self.config)
        self.render()
        self._sync_settings()

    def _on_wheel(self, event) -> None:
        delta = getattr(event, "delta", 0)
        if delta == 0:
            num = getattr(event, "num", 0)
            delta = 120 if num == 4 else (-120 if num == 5 else 0)
        if delta == 0:
            return
        ctrl = bool(getattr(event, "state", 0) & 0x0004)
        step = 0.05 if ctrl else 0.1
        self._set_scale(self._current_scale() + (step if delta > 0 else -step), save=True)

    def _start_resize(self, event) -> None:
        self._resizing = True
        self._dragging = False
        self._resize_origin = (
            event.x_root,
            event.y_root,
            self._current_scale(),
            max(40, self.root.winfo_width()),
            max(20, self.root.winfo_height()),
        )

    def _on_resize(self, event) -> None:
        sx, sy, start_scale, start_w, start_h = self._resize_origin
        new_scale = scale_from_drag(start_scale, start_w, start_h, event.x_root - sx, event.y_root - sy)
        if abs(new_scale - self._current_scale()) >= 0.03:
            self._set_scale(new_scale, save=False)

    def _end_resize(self, event) -> None:
        self._resizing = False
        save_config(self.config)
        self._sync_backdrop()

    def _move_to_preset(self, preset: str) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        monitors, primary = get_monitor_rects(self.root)
        monitor = (
            current_monitor_rect(self.root.winfo_x(), self.root.winfo_y(), w, h, monitors, primary)
            or primary
        )
        x, y = preset_position(preset, w, h, monitor)
        self.config["x"] = x
        self.config["y"] = y
        self.root.geometry(f"+{x}+{y}")
        self._sync_backdrop()
        save_config(self.config)
        if self._adaptive_active():
            self.root.after(60, lambda: self._request_adaptive_sample(immediate=False))

    def _reset_position(self) -> None:
        self.config["x"] = self.config["y"] = None
        self._place_initial()
        save_config(self.config)
        if self._adaptive_active():
            self.root.after(60, lambda: self._request_adaptive_sample(immediate=False))

    # ---------------- 위치·드래그 ----------------
    def _place_initial(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        monitors, primary = get_monitor_rects(self.root)
        x, y = clamp_position(self.config.get("x"), self.config.get("y"), w, h, monitors, primary)
        self.root.geometry(f"+{x}+{y}")
        self._sync_backdrop()

    def _bind_interaction(self, widget: tk.Misc) -> None:
        if widget not in self.clickables:
            widget.bind("<ButtonPress-1>", self._on_widget_press)
            widget.bind("<B1-Motion>", self._on_widget_motion)
            widget.bind("<ButtonRelease-1>", self._on_widget_release)
            widget.bind("<Motion>", self._on_hover_motion)
        widget.bind("<Button-3>", self._show_menu)
        widget.bind("<MouseWheel>", self._on_wheel)
        widget.bind("<Button-4>", self._on_wheel)
        widget.bind("<Button-5>", self._on_wheel)
        for child in widget.winfo_children():
            self._bind_interaction(child)

    def _on_widget_press(self, event) -> None:
        if self.context_menu.is_open:
            self.context_menu.close()
        if self._is_resize_corner(event.x_root, event.y_root):
            self._start_resize(event)
            return
        self._start_drag(event)

    def _on_widget_motion(self, event) -> None:
        if self._resizing:
            self._on_resize(event)
        elif self._dragging:
            self._on_drag(event)

    def _on_widget_release(self, event) -> None:
        if self._resizing:
            self._end_resize(event)
        elif self._dragging:
            self._end_drag(event)

    def _start_drag(self, event) -> None:
        self._dragging = True
        self._resizing = False
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event) -> None:
        dx, dy = self._drag
        nx, ny = event.x_root - dx, event.y_root - dy
        self.root.geometry(f"+{nx}+{ny}")
        if self.backdrop is not None and self.backdrop.state() != "withdrawn":
            self.backdrop.geometry(f"+{nx}+{ny}")
        if self._adaptive_active():
            now = time.monotonic()
            if now - self._last_drag_sample_at >= ADAPTIVE_DRAG_THROTTLE_S:
                self._last_drag_sample_at = now
                self._request_adaptive_sample(immediate=False)

    def _end_drag(self, event) -> None:
        self._dragging = False
        self.config["x"], self.config["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.config)
        self._sync_backdrop()
        if self._adaptive_active():
            self.root.after(60, lambda: self._request_adaptive_sample(immediate=False))

    def _show_menu(self, event) -> None:
        if self.backdrop is not None and self.backdrop.state() != "withdrawn":
            self.root.lift(self.backdrop)
        monitors, primary = get_monitor_rects(self.root)
        self.context_menu.popup(
            event.x_root,
            event.y_root,
            self._build_menu_items(),
            self._font_family(),
            monitors,
            primary,
        )

    def _font_family(self) -> str:
        return pick_font_family(self.config.get("font"), self._available_fonts)

    def _set_font(self, pref: str) -> None:
        self.config["font"] = pref
        save_config(self.config)
        self.render()
        self._sync_settings()

    # ---------------- 화면 그리기 ----------------
    def _scaled_ui(self) -> tuple[dict[str, int], tuple, tuple, tuple]:
        family = self._font_family()
        m = scaled_metrics(self._current_scale())
        font = (family, m["font_main"])
        font_bold = (family, m["font_main"], "bold")
        font_small = (family, m["font_small"])
        return m, font, font_bold, font_small

    def _draw_brand_dots(self, dot_size: int) -> None:
        d = max(3, dot_size)
        gap = max(2, d // 2)
        total_w = d * 4 + gap * 3
        self.brand_canvas.config(width=total_w, height=d)
        self.brand_canvas.delete("all")
        for idx, color in enumerate(GOOGLE_DOTS):
            x0 = idx * (d + gap)
            self.brand_canvas.create_oval(x0, 0, x0 + d, d, fill=color, outline="")

    def _paint_pill_bar(
        self,
        c: tk.Canvas,
        width: int,
        height: int,
        pct: float,
        track_color: str,
        fill_color: str,
        pace_pct: float | None = None,
        marker_color: str = "#ffffff",
    ) -> None:
        """Material 3 스타일의 양 끝이 둥근 알약형 프로그레스 바를 기존 Canvas에 그린다."""
        h = max(4, height)
        w = max(h + 2, width)
        c.delete("all")

        def pill(x1: int, color: str) -> None:
            if x1 <= 0:
                return
            if x1 <= h:
                c.create_oval(0, 0, x1, h, fill=color, outline="")
                return
            r = h
            c.create_oval(0, 0, r, h, fill=color, outline="")
            c.create_oval(x1 - r, 0, x1, h, fill=color, outline="")
            c.create_rectangle(r // 2, 0, x1 - r // 2, h, fill=color, outline="")

        pill(w, track_color)
        fill_w = int(round(w * max(0.0, min(100.0, pct)) / 100.0))
        if fill_w > 0:
            pill(max(fill_w, min(h, w)), fill_color)
        if pace_pct is not None:
            mx = int(round(w * max(0.0, min(100.0, pace_pct)) / 100.0))
            mx = max(1, min(w - 2, mx))
            c.create_rectangle(mx, 0, mx + 2, h, fill=marker_color, outline="")

    def _draw_pill_bar(
        self,
        parent: tk.Misc,
        width: int,
        height: int,
        pct: float,
        track_color: str,
        fill_color: str,
        bg: str,
        pace_pct: float | None = None,
        marker_color: str = "#ffffff",
    ) -> tk.Canvas:
        h = max(4, height)
        w = max(h + 2, width)
        c = tk.Canvas(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0)
        self._paint_pill_bar(c, w, h, pct, track_color, fill_color, pace_pct=pace_pct, marker_color=marker_color)
        return c

    def _layered_style(self) -> Style:
        scale = self._current_scale()
        m = scaled_metrics(scale)
        ppp = self._px_per_pt  # 1pt가 몇 픽셀인지 (화면 DPI 반영)
        dpi = ppp / (96 / 72)
        transparent = self._is_transparent_bg()
        pal = COLORS_TRANSPARENT if transparent else COLORS
        colors = {role: argb(value) for role, value in pal.items()}
        colors["marker"] = 0xFFFFFFFF
        return Style(
            main=m["font_main"] * ppp,
            small=m["font_small"] * ppp,
            bar_w=m["bar_w"] * dpi,
            bar_h=m["bar_h"] * dpi,
            pad_x=m["pad_x"] * dpi,
            pad_y=m["pad_y"] * dpi,
            compact_pad_x=m["compact_pad_x"] * dpi,
            compact_pad_y=m["compact_pad_y"] * dpi,
            wrap_w=m["wrap_w"] * dpi,
            unit=dpi * max(0.7, scale),
            transparent=transparent,
            colors=colors,
            pct_bold=m["font_main"] <= 8,
        )

    def _detail_block(self, p, show_pace: bool) -> ProviderBlock:
        stale = p.is_stale()
        dim = stale or not p.ok
        if not dim and p.windows:
            dot_role = level(max(w.used_percent for w in p.windows))
        else:
            dot_role = "error" if p.error else "muted"
        rows = []
        for w in p.windows:
            pace = window_pace_percent(w) if show_pace else None
            rows.append(
                WindowRow(
                    label=w.label,
                    used=float(round(w.used_percent)),
                    pace=float(round(pace)) if pace is not None else None,
                    pace_role=pace_level(w.used_percent, pace) if pace is not None else "muted",
                    reset=format_reset_compact(w),
                    eta="" if dim else format_eta(w),
                )
            )
        return ProviderBlock(p.name, dot_role, stale, dim, p.error or "", tuple(rows))

    def _render_layered(self) -> None:
        renderer = self._layered
        providers = self.store.snapshot()
        self._provider_log.observe(providers, self.store.fetch_stats)
        self.shown_version = self.store.version
        compact = bool(self.config.get("compact", False))
        show_pace = bool(self.config.get("show_pace", True))
        family = self._font_family()
        update_text = str(self.update_label.cget("text"))
        self._shown_update_text = update_text

        wave: float | None = None
        if compact:
            now_s = time.time()
            tracked = self._tracker.tracking(now_s) if self._tracking_enabled() else frozenset()

            def gauge(g, gain: float) -> Gauge | None:
                if not g:
                    return None
                return Gauge(g[0], float(round(g[1])) if g[1] is not None else None, g[2], g[3], gain)

            show_wave = bool(self.config.get("track_wave", True))
            show_gain = bool(self.config.get("track_gain", True))
            built = []
            for p, (short, long, five, mark, mark_role) in zip(providers, compact_gauges(providers, show_pace=show_pace)):
                gains = [0.0, 0.0]
                if p.provider in tracked and show_gain:
                    # 추적을 시작한 뒤 늘어난 양 (화면에 보이는 정수 기준)
                    start = self._tracker.baseline(p.provider, now_s)
                    for i, (g, label) in enumerate(zip((long, five), compact_peak_labels(p))):
                        if g and label in start:
                            gains[i] = max(0.0, g[0] - round(start[label]))
                built.append(
                    CompactItem(
                        short, gauge(long, gains[0]), gauge(five, gains[1]), mark, mark_role,
                        tracking=p.provider in tracked,
                    )
                )
            items = tuple(built)
            animate = show_wave and any(it.tracking for it in items) and self.visible and _animations_enabled()
            if animate:
                frames = round(TRACK_ANIM_CYCLE_SEC * 1000 / TRACK_ANIM_MS)
                wave = int(now_s % TRACK_ANIM_CYCLE_SEC / TRACK_ANIM_CYCLE_SEC * frames) / frames
                if self._track_anim_id is None:
                    self._track_anim_id = self.root.after(TRACK_ANIM_MS, self._tick_track_anim)
            content = ("compact", items, bool(self.update_info), wave, show_wave)
        else:
            clock_text = format_clock(self.store.received_at) if self.store.received_at else ""
            if self.store.needs_extension_reload():
                clock_text = "⚠ 확장 다시 로드"  # 토큰 파일이 없는 옛 크롬 확장 (server.py 하위 호환)
            blocks = tuple(self._detail_block(p, show_pace) for p in providers)
            content = ("detail", blocks, clock_text, update_text)

        # 표시 내용이 완전히 같으면 다시 그리지 않는다.
        render_key = (content, self._is_transparent_bg(), self._current_scale(), family)
        if render_key == self._last_render_key:
            return
        self._last_render_key = render_key

        try:
            renderer.set_family(family)
            style = self._layered_style()
            if compact:
                scene = layout_compact(
                    items, style, renderer, has_update=bool(self.update_info), wave=wave, ripples=show_wave
                )
            else:
                scene = layout_detail(blocks, style, renderer, update_text=update_text, clock_text=clock_text)
            surface = renderer.paint(scene)
        except OSError as e:
            diagnostics.log.error("화면을 그리지 못함: %s", e)
            return
        old, self._surface, self._scene = self._surface, surface, scene
        self._sizer.configure(width=scene.width, height=scene.height)
        self.root.update_idletasks()
        self._present()
        if old is not None:
            old.close()
        self._keep_on_screen()
        self._refresh_settings_live(services=False)

    def _tick_track_anim(self) -> None:
        """추적 물결의 다음 장면을 그린다. 그릴 것이 없어지면 render()가 타이머를 다시 걸지 않아 저절로 멈춘다."""
        self._track_anim_id = None
        if self._layered is not None and self.visible and bool(self.config.get("compact", False)):
            self.render()

    def _present(self) -> None:
        """마지막으로 그린 그림을 창에 올린다. 창 스타일이 바뀐 뒤에도 다시 부른다."""
        if self._layered is None or self._surface is None:
            return
        try:
            hwnd = self._layered.toplevel_hwnd(self.root.winfo_id())
            ok = self._layered.present(hwnd, self._surface, float(self.config.get("alpha", 0.92)))
        except (OSError, tk.TclError) as e:
            ok = False
            diagnostics.log.error("화면을 창에 올리지 못함: %s", e)
        if not ok and not self._present_failed:
            diagnostics.log.error("UpdateLayeredWindow 실패. config.json의 renderer를 classic으로 바꾸면 기존 방식으로 그립니다.")
        self._present_failed = not ok

    def render(self) -> None:
        if self._layered is not None:
            self._render_layered()
            return
        pal = self._palette()
        m, font, font_bold, font_small = self._scaled_ui()
        providers = self.store.snapshot()
        self._provider_log.observe(providers, self.store.fetch_stats)
        self.shown_version = self.store.version
        compact = bool(self.config.get("compact", False))
        show_pace = bool(self.config.get("show_pace", True))
        transparent_bg = self._is_transparent_bg()
        scale = self._current_scale()
        font_family = self._font_family()
        has_update = self.update_info is not None
        clock_text = "" if compact else (format_clock(self.store.received_at) if self.store.received_at else "")
        if not compact and self.store.needs_extension_reload():
            clock_text = "⚠ 확장 다시 로드"  # 토큰 파일이 없는 옛 크롬 확장 (server.py 하위 호환)

        if compact:
            summary = compact_summary(providers, show_pace=show_pace)
            five_h = compact_five_hour_summary(providers, show_pace=show_pace)
            adaptive_on = self._adaptive_mode_enabled()
            chroma_key = self._chroma_key() if transparent_bg else ""
            render_key = (
                "compact", transparent_bg, adaptive_on, chroma_key, scale, font_family, has_update, show_pace,
                tuple(summary), tuple(five_h),
            )
            layout_key = (
                "compact", transparent_bg, adaptive_on, chroma_key, scale, font_family, has_update,
                len(summary), len(five_h),
            )
        else:
            detail_rows = []
            layout_rows = []
            for p in providers:
                stale = p.is_stale()
                dim = stale or not p.ok
                win_data = []
                win_layout = []
                for w in p.windows:
                    reset_str = format_reset_compact(w)
                    eta_str = "" if dim else format_eta(w)
                    pace = window_pace_percent(w) if show_pace else None
                    pace_val = round(pace) if pace is not None else None
                    win_data.append((w.label, round(w.used_percent), pace_val, reset_str, eta_str))
                    win_layout.append((pace is not None, bool(reset_str), bool(eta_str)))
                detail_rows.append((p.provider, p.name, stale, dim, p.error or "", tuple(win_data)))
                layout_rows.append((p.provider, bool(p.error), tuple(win_layout)))
            render_key = (
                "detail", transparent_bg, scale, font_family, has_update, show_pace, clock_text,
                tuple(detail_rows),
            )
            layout_key = (
                "detail", transparent_bg, scale, font_family, has_update,
                tuple(layout_rows),
            )

        # 표시 내용이 완전히 같으면 아무것도 다시 그리지 않아 점멸을 방지한다.
        if render_key == self._last_render_key:
            return
        self._last_render_key = render_key

        # 구조(프로바이더·행 수·모드·크기)가 같으면 위젯을 파괴하지 않고 값만 제자리 갱신한다.
        if layout_key == self._last_layout_key:
            self.clock.config(text=clock_text)
            if compact:
                self._update_compact_in_place(summary, five_h, pal)
            else:
                self._update_detail_in_place(providers, pal, m, show_pace)
            self._keep_on_screen()
            return

        self._last_layout_key = layout_key
        self._detail_refs = []
        self._compact_refs = None
        self._compact_slots = []
        for child in self.body.winfo_children():
            child.destroy()
        self.clickable_actions = self._base_clickable_actions()
        self.clickables = set(self.clickable_actions)

        self.compact_btn.config(text="▸" if compact else "▾", font=font_small)
        self.title_label.config(font=font_bold)
        self.update_label.config(font=font_small)
        self.refresh_btn.config(font=font_bold)
        self.settings_btn.config(font=font_bold)
        self.close_button.config(font=font_bold)
        self.clock.config(text=clock_text, font=font_small)
        self._draw_brand_dots(max(3, m["bar_h"] - 1))

        # 컴팩트 모드에서는 별도 헤더를 숨기고 2줄 그리드로 점유 면적을 최소화한다.
        if compact:
            self.header.pack_forget()
            self.divider.pack_forget()
            self.frame.configure(padx=m["compact_pad_x"], pady=m["compact_pad_y"])
            self.body.pack_configure(pady=(0, 0))
            self._render_compact(providers, pal, font_bold, font_small, show_pace)
        else:
            if not self.header.winfo_manager():
                self.header.pack(fill="x", before=self.body)
            if transparent_bg:
                self.divider.pack_forget()
            elif not self.divider.winfo_manager():
                self.divider.pack(fill="x", pady=(3, 2), before=self.body)
            self.frame.configure(padx=m["pad_x"] + 2, pady=m["pad_y"] + 1)
            self.body.pack_configure(pady=(1 if not transparent_bg else max(2, m["pad_y"]), 0))
            if not providers:
                tk.Label(
                    self.body,
                    text="데이터 대기 중…",
                    font=font_small, bg=pal["bg"], fg=pal["muted"], justify="left", padx=0, pady=0,
                ).grid(row=0, column=0, sticky="w")
            else:
                row = 0
                for p in providers:
                    stale = p.is_stale()
                    dim = stale or not p.ok
                    dot_color = self._provider_dot_color(p, dim, pal)
                    hdr_row = tk.Frame(self.body, bg=pal["bg"])
                    hdr_row.grid(row=row, column=0, columnspan=5, sticky="w", pady=(3 if row else 0, 1))
                    dot_d = max(4, m["bar_h"])
                    dot = tk.Canvas(hdr_row, width=dot_d, height=dot_d, bg=pal["bg"], highlightthickness=0, bd=0)
                    dot_item = dot.create_oval(0, 0, dot_d, dot_d, fill=dot_color, outline="")
                    dot.pack(side="left", padx=(0, 4))
                    name = p.name + (" (오래됨)" if stale else "")
                    name_lbl = tk.Label(
                        hdr_row, text=name, font=font_bold, bg=pal["bg"],
                        fg=pal["muted"] if stale else pal["fg"], padx=0, pady=0,
                    )
                    name_lbl.pack(side="left")
                    row += 1
                    win_refs = []
                    for w in p.windows:
                        wref = self._window_row(row, w, dim, pal, m, font, font_small, show_pace)
                        row += 1
                        eta = "" if dim else format_eta(w)
                        eta_lbl = None
                        if eta:
                            eta_lbl = tk.Label(
                                self.body, text=f"↳ {eta}", font=font_small, bg=pal["bg"], fg=pal["warn"],
                                anchor="w", padx=0, pady=0,
                            )
                            eta_lbl.grid(row=row, column=0, columnspan=5, sticky="w")
                            row += 1
                        win_refs.append((*wref, eta_lbl))
                    err_lbl = None
                    if p.error:
                        err_lbl = tk.Label(
                            self.body, text=p.error, font=font_small, bg=pal["bg"], fg=pal["error"],
                            wraplength=m["wrap_w"], justify="left", padx=0, pady=0,
                        )
                        err_lbl.grid(row=row, column=0, columnspan=5, sticky="w")
                        row += 1
                    self._detail_refs.append((dot, dot_item, name_lbl, win_refs, err_lbl))

        self._bind_interaction(self.frame)
        self._keep_on_screen()

    def _provider_dot_color(self, p, dim: bool, pal: dict[str, str]) -> str:
        if not dim and p.windows:
            return pal[level(max(w.used_percent for w in p.windows))]
        if p.error:
            return pal["error"]
        return pal["muted"]

    def _update_detail_in_place(self, providers, pal: dict[str, str], m: dict[str, int], show_pace: bool) -> None:
        for p, (dot, dot_item, name_lbl, win_refs, err_lbl) in zip(providers, self._detail_refs):
            stale = p.is_stale()
            dim = stale or not p.ok
            dot.itemconfig(dot_item, fill=self._provider_dot_color(p, dim, pal))
            name_lbl.config(
                text=p.name + (" (오래됨)" if stale else ""),
                fg=pal["muted"] if stale else pal["fg"],
            )
            fg = pal["muted"] if dim else pal["fg"]
            marker_color = pal["muted"] if dim else "#ffffff"
            for w, (lbl_w, bar_c, pct_lbl, pace_lbl, reset_lbl, eta_lbl) in zip(p.windows, win_refs):
                fill_color = pal["muted"] if dim else pal[level(w.used_percent)]
                pace = window_pace_percent(w) if show_pace else None
                lbl_w.config(text=w.label, fg=fg)
                self._paint_pill_bar(
                    bar_c, m["bar_w"], m["bar_h"], w.used_percent, pal["bar_bg"], fill_color,
                    pace_pct=pace, marker_color=marker_color,
                )
                pct_lbl.config(text=f"{round(w.used_percent)}%", fg=fg)
                if pace_lbl is not None and pace is not None:
                    pace_fg = pal["muted"] if dim else pal[pace_level(w.used_percent, pace)]
                    pace_lbl.config(text=f"기준{round(pace)}%", fg=pace_fg)
                if reset_lbl is not None:
                    reset_lbl.config(text=format_reset_compact(w))
                if eta_lbl is not None:
                    eta = "" if dim else format_eta(w)
                    eta_lbl.config(text=f"↳ {eta}" if eta else "")
            if err_lbl is not None and p.error:
                err_lbl.config(text=p.error)

    def _update_compact_in_place(self, summary, five_h, pal: dict[str, str]) -> None:
        if not self._compact_refs:
            return
        adaptive = self._adaptive_mode_enabled()
        top_refs, five_refs = self._compact_refs
        for (short, value, color_key), (s_lbl, v_lbl, slot_idx, v_item) in zip(summary, top_refs):
            v_item[1] = color_key
            s_lbl.config(text=f"{short} ")
            fg = self._adaptive.color_for(slot_idx, color_key) if adaptive else pal[color_key]
            v_lbl.config(text=value, fg=fg)
        for (_short, value, color_key), (v_lbl, slot_idx, f_item) in zip(five_h, five_refs):
            f_item[1] = color_key
            fg = self._adaptive.color_for(slot_idx, color_key) if adaptive else pal[color_key]
            v_lbl.config(text=value, fg=fg)

    def _render_compact(
        self, providers, pal: dict[str, str], font_bold: tuple, font_small: tuple, show_pace: bool = True
    ) -> None:
        summary = compact_summary(providers, show_pace=show_pace)
        five_h = compact_five_hour_summary(providers, show_pace=show_pace)
        adaptive = self._adaptive_mode_enabled()

        def _fg(slot_idx: int, role: str) -> str:
            return self._adaptive.color_for(slot_idx, role) if adaptive else pal[role]

        box = tk.Frame(self.body, bg=pal["bg"])
        box.pack(fill="x")

        expand = tk.Label(box, text="▸", font=font_small, bg=pal["grip_bg"], fg=_fg(0, "muted"), cursor="hand2", padx=2, pady=0)
        expand.grid(row=0, column=0, sticky="w", padx=(0, 2))
        expand.bind("<Button-1>", lambda e: self._toggle_compact())
        self.clickable_actions[expand] = self._toggle_compact
        self.clickables.add(expand)

        slots: list[dict] = [{"anchor": expand, "items": [[expand, "muted"]]}]

        if not summary:
            lbl = tk.Label(box, text="대기 중…", font=font_small, bg=pal["bg"], fg=_fg(1, "muted"), padx=0, pady=0)
            lbl.grid(row=0, column=1, sticky="w")
            lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            slots.append({"anchor": lbl, "items": [[lbl, "muted"]]})
            self._compact_slots = slots
            self._compact_refs = ([], [])
            return

        top_refs = []
        for idx, (short, value, color_key) in enumerate(summary):
            col = idx * 2 + 1
            slot_idx = idx + 1
            slot_items: list[list] = []
            if idx > 0:
                sep_top = tk.Label(box, text="·", font=font_small, bg=pal["bg"], fg=_fg(slot_idx, "muted"), padx=2, pady=0)
                sep_top.grid(row=0, column=col - 1)
                slot_items.append([sep_top, "muted"])
            cell = tk.Frame(box, bg=pal["bg"])
            cell.grid(row=0, column=col, sticky="e")
            s_lbl = tk.Label(cell, text=f"{short} ", font=font_small, bg=pal["bg"], fg=_fg(slot_idx, "muted"), padx=0, pady=0)
            s_lbl.pack(side="left")
            v_lbl = tk.Label(cell, text=value, font=font_bold, bg=pal["bg"], fg=_fg(slot_idx, color_key), padx=0, pady=0)
            v_lbl.pack(side="left")
            s_lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            v_lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            slot_items.append([s_lbl, "muted"])
            v_item = [v_lbl, color_key]
            slot_items.append(v_item)
            slots.append({"anchor": cell, "items": slot_items})
            top_refs.append((s_lbl, v_lbl, slot_idx, v_item))

        five_refs = []
        if five_h:
            lbl_5h = tk.Label(box, text="5h", font=font_small, bg=pal["bg"], fg=_fg(0, "muted"), padx=1, pady=0)
            lbl_5h.grid(row=1, column=0, sticky="w", padx=(0, 2))
            lbl_5h.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            slots[0]["items"].append([lbl_5h, "muted"])
            for idx, (_short, value, color_key) in enumerate(five_h):
                col = idx * 2 + 1
                slot_idx = min(idx + 1, len(slots) - 1)
                if idx > 0:
                    sep_5h = tk.Label(box, text="·", font=font_small, bg=pal["bg"], fg=_fg(slot_idx, "muted"), padx=2, pady=0)
                    sep_5h.grid(row=1, column=col - 1)
                    slots[slot_idx]["items"].append([sep_5h, "muted"])
                v_font = (font_small[0], font_small[1], "bold")
                v_lbl = tk.Label(box, text=value, font=v_font, bg=pal["bg"], fg=_fg(slot_idx, color_key), padx=0, pady=0)
                v_lbl.grid(row=1, column=col, sticky="e")
                v_lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())
                f_item = [v_lbl, color_key]
                slots[slot_idx]["items"].append(f_item)
                five_refs.append((v_lbl, slot_idx, f_item))

        if self.update_info:
            upd_slot_idx = len(slots)
            upd = tk.Label(box, text=" ⬆", font=font_small, bg=pal["bg"], fg=_fg(upd_slot_idx, "accent"), cursor="hand2", padx=0, pady=0)
            upd.grid(row=0, column=len(summary) * 2, sticky="w")
            upd.bind("<Button-1>", lambda e: self.start_update())
            self.clickable_actions[upd] = self.start_update
            self.clickables.add(upd)
            slots.append({"anchor": upd, "items": [[upd, "accent"]]})

        self._compact_slots = slots
        self._compact_refs = (top_refs, five_refs)

    def _keep_on_screen(self) -> None:
        """내용이 늘어나 창이 현재 모니터 밖으로 잘리면 그 모니터 안쪽으로 당긴다."""
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        monitors, primary = get_monitor_rects(self.root)
        nx, ny = clamp_position(x, y, w, h, monitors, primary)
        if (nx, ny) != (x, y):
            self.root.geometry(f"+{nx}+{ny}")
        self._sync_backdrop()

    def _window_row(
        self, row: int, w, dim: bool, pal: dict[str, str], m: dict[str, int], font: tuple, font_small: tuple, show_pace: bool = True
    ):
        fg = pal["muted"] if dim else pal["fg"]
        fill_color = pal["muted"] if dim else pal[level(w.used_percent)]
        pace = window_pace_percent(w) if show_pace else None
        marker_color = pal["muted"] if dim else "#ffffff"
        lbl_w = tk.Label(self.body, text=w.label, font=font, bg=pal["bg"], fg=fg, anchor="w", padx=0, pady=0)
        lbl_w.grid(row=row, column=0, sticky="w", padx=(0, 4))
        bar = self._draw_pill_bar(
            self.body, m["bar_w"], m["bar_h"], w.used_percent, pal["bar_bg"], fill_color, pal["bg"],
            pace_pct=pace, marker_color=marker_color,
        )
        bar.grid(row=row, column=1, padx=3)
        pct_font = (font[0], font[1], "bold") if m["font_main"] <= 8 else font
        pct_lbl = tk.Label(self.body, text=f"{round(w.used_percent)}%", font=pct_font, bg=pal["bg"], fg=fg, anchor="e", padx=0, pady=0)
        pct_lbl.grid(row=row, column=2, sticky="e", padx=(2, 0))
        pace_lbl = None
        if pace is not None:
            pace_fg = pal["muted"] if dim else pal[pace_level(w.used_percent, pace)]
            pace_lbl = tk.Label(self.body, text=f"기준{round(pace)}%", font=font_small, bg=pal["bg"], fg=pace_fg, anchor="w", padx=0, pady=0)
            pace_lbl.grid(row=row, column=3, sticky="w", padx=(3, 0))
        reset_str = format_reset_compact(w)
        reset_lbl = None
        if reset_str:
            reset_lbl = tk.Label(self.body, text=reset_str, font=font_small, bg=pal["bg"], fg=pal["muted"], anchor="w", padx=0, pady=0)
            reset_lbl.grid(row=row, column=4, sticky="w", padx=(4, 0))
        return (lbl_w, bar, pct_lbl, pace_lbl, reset_lbl)

    # ---------------- 주기 작업 ----------------
    def _tick_fast(self) -> None:
        # 서버 스레드가 새 데이터를 받았는지 0.5초마다 확인 (tkinter는 화면 스레드에서만 그려야 함)
        if self.store.version != self.shown_version or (
            self._layered is not None and str(self.update_label.cget("text")) != self._shown_update_text
        ):
            self.render()
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(*event)
        self.root.after(500, self._tick_fast)

    def _tick_slow(self) -> None:
        # 남은 시간과 '오래됨' 표시를 갱신
        self.render()
        self._refresh_settings_live()
        self.root.after(30_000, self._tick_slow)

    # ---------------- 업데이트 ----------------
    def _auto_update_check(self) -> None:
        if self.config.get("auto_update_check", True):
            self.check_updates(manual=False)
        self.root.after(UPDATE_CHECK_MS, self._auto_update_check)

    def check_updates(self, manual: bool = False) -> None:
        if self.update_busy:
            return

        def work():
            try:
                self.events.put(("checked", updater.fetch_latest(), manual))
            except updater.UpdateError as e:
                self.events.put(("check_error", str(e), manual))

        threading.Thread(target=work, daemon=True).start()

    def start_update(self) -> None:
        info = self.update_info
        if self.update_busy:
            return
        if info is None:
            self.check_updates(manual=True)
            return
        if not updater.is_frozen():
            if messagebox.askyesno(
                "AI 사용량 위젯",
                f"새 버전 v{info.version}이 있어요.\n\n소스 코드로 실행 중이라 자동 업데이트는 안 돼요.\n"
                "저장소 폴더에서 git pull 하세요.\n\n릴리스 페이지를 열까요?",
                parent=self._dialog_parent(),
            ):
                webbrowser.open(info.page_url)
            return
        notes = info.notes[:400] + ("…" if len(info.notes) > 400 else "")
        if not messagebox.askyesno(
            "AI 사용량 위젯",
            f"새 버전 v{info.version}이 있어요. (현재 v{__version__})\n\n{notes}\n\n"
            "지금 업데이트할까요? 위젯이 잠깐 꺼졌다가 다시 켜져요.",
            parent=self._dialog_parent(),
        ):
            return
        self.update_busy = True
        self.update_label.config(text="다운로드 0%")
        dest = updater.update_dir() / updater.EXE_ASSET_NAME

        def work():
            try:
                path = updater.download(info, dest, progress=lambda p: self.events.put(("progress", p)))
                self.events.put(("downloaded", path))
            except updater.UpdateError as e:
                self.events.put(("download_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _handle_event(self, kind: str, value=None, manual: bool = False) -> None:
        if kind == "checked":
            if updater.is_newer(value.version):
                self.update_info = value
                self.update_label.config(text=f"⬆ v{value.version}")
                self._sync_settings()
                if manual:
                    self.start_update()
            elif manual:
                messagebox.showinfo("AI 사용량 위젯", f"최신 버전이에요. (v{__version__})", parent=self._dialog_parent())
        elif kind == "check_error":
            if manual:
                messagebox.showwarning("AI 사용량 위젯", value, parent=self._dialog_parent())
        elif kind == "progress":
            self.update_label.config(text=f"다운로드 {value}%")
        elif kind == "downloaded":
            try:
                updater.launch_finisher(value, updater.current_exe())
            except OSError as e:
                self.update_busy = False
                self.update_label.config(text=f"⬆ v{self.update_info.version}")
                messagebox.showerror("AI 사용량 위젯", f"업데이트를 시작하지 못했어요.\n{e}", parent=self._dialog_parent())
                return
            self.update_label.config(text="업데이트 중…")
            self.root.after(100, self.quit)  # 새 exe가 이 파일을 덮어쓸 수 있도록 종료
        elif kind == "download_error":
            self.update_busy = False
            self.update_label.config(text=f"⬆ v{self.update_info.version}" if self.update_info else "")
            messagebox.showerror("AI 사용량 위젯", value, parent=self._dialog_parent())
        elif kind == "tray_toggle_visible":
            self._toggle_visible()
        elif kind == "tray_toggle_click_through":
            self._toggle_click_through()
        elif kind == "tray_toggle_compact":
            self._toggle_compact()
        elif kind == "tray_toggle_transparent_bg":
            self._toggle_transparent_bg()
        elif kind == "tray_refresh":
            self.request_refresh()
        elif kind == "tray_open_settings":
            self.open_settings()
        elif kind == "tray_quit":
            self.quit()
        elif kind == "usage_polled":
            self._on_usage_polled(*value)
        elif kind == "browser_login_closed":
            if self._web_source() == "browser":
                self._poll_browser()
            elif value in browser_worker.FETCH_URLS:
                self._poll_browser([value])

    def diagnostics_report(self) -> str:
        return diagnostics.build_report(
            self.config,
            self.store.snapshot(),
            stats=self.store.fetch_stats.summary(),
            extra={
                "실행 방식": "데모" if self._demo else "일반",
                "내장 브라우저 사용 가능": "예" if browser_worker.is_available() else "아니오",
                "트레이 사용 가능": "예" if self.tray.active else "아니오",
                # 토큰 없이 보내는 옛 확장이면 "토큰 없음(옛 확장)" (server.py 하위 호환)
                "크롬 확장 인증": {"token": "토큰", "legacy": "토큰 없음(옛 확장, 다시 로드 필요)"}.get(
                    self.store.extension_auth or "", "아직 받은 요청 없음"
                ),
                "확장 폴더 토큰 파일": "있음"
                if (updater.chrome_extension_dir() / local_token.EXTENSION_TOKEN_FILE).is_file()
                else "없음",
            },
        )

    def copy_diagnostics(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.diagnostics_report())
        self.root.update_idletasks()
        messagebox.showinfo(
            "진단 정보",
            "진단 정보를 클립보드에 복사했어요.\n"
            "GitHub 이슈에 그대로 붙여 넣을 수 있어요.\n\n"
            "토큰, 계정 식별자, 사용자 폴더 이름은 가려져 있어요.",
            parent=self._dialog_parent(),
        )

    def open_log_folder(self) -> None:
        path = diagnostics.log_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.as_uri())
        except OSError:
            diagnostics.log.warning("로그 폴더를 열 수 없음")

    def open_extension_folder(self) -> None:
        updater.sync_bundled_extension()
        path = updater.chrome_extension_dir()
        self.root.clipboard_clear()
        self.root.clipboard_append(str(path))
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.as_uri())
        except OSError:
            pass
        messagebox.showinfo(
            "크롬 확장 폴더",
            f"{path}\n\n크롬 chrome://extensions 에서 '압축해제된 확장 프로그램을 로드합니다'로 "
            "이 폴더를 선택하세요. 경로는 클립보드에 복사했어요.\n\n"
            "이 폴더로 설치해 두면 위젯을 업데이트할 때 확장도 자동으로 업데이트돼요.",
            parent=self._dialog_parent(),
        )

    def quit(self) -> None:
        self._cancel_adaptive_timers()
        self._adaptive_sampler.stop()
        self.context_menu.close()
        self.settings_window.close()
        self.tray.stop()
        self._browser_collector.stop()
        save_config(self.config)
        self.root.destroy()
        if self._layered is not None:
            if self._surface is not None:
                self._surface.close()
                self._surface = None
            self._layered.close()
            self._layered = None


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 구독 사용량 위젯")
    parser.add_argument("--demo", action="store_true", help="예시 데이터로 화면 확인")
    parser.add_argument("--port", type=int, help="확장이 보낼 포트 (기본 47823)")
    parser.add_argument("--finish-update", metavar="EXE", help=argparse.SUPPRESS)
    parser.add_argument("--browser-login", choices=list(browser_worker.LOGIN_URLS), help=argparse.SUPPRESS)
    parser.add_argument("--browser-fetch", metavar="PROVIDERS", help=argparse.SUPPRESS)
    parser.add_argument("--output", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--claude-org-id", metavar="ORG_ID", help=argparse.SUPPRESS)
    parser.add_argument("--check-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-browser-hidden", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # 내장 브라우저(WebView2) 온디맨드 서브프로세스 워커 모드
    if args.check_browser:
        ok, msg = browser_worker.check_browser_runtime()
        print(msg)
        sys.exit(0 if ok else 1)

    if args.check_browser_hidden:
        ok, msg = browser_worker.check_hidden_browser_runtime(Path(args.output) if args.output else None)
        print(msg)
        sys.exit(0 if ok else 1)

    if args.browser_login:
        sys.exit(browser_worker.run_login(args.browser_login))

    if args.browser_fetch:
        if not args.output:
            sys.exit(2)
        provs = [p.strip() for p in args.browser_fetch.split(",") if p.strip()]
        sys.exit(
            browser_worker.run_fetch(
                provs,
                Path(args.output),
                claude_org_id=args.claude_org_id,
            )
        )

    # 업데이트 2단계: 새로 받은 exe가 기존 exe를 덮어쓰고 다시 실행한 뒤 끝난다.
    if args.finish_update:
        self_exe = updater.current_exe()
        if self_exe:
            updater.finish_update(Path(args.finish_update), self_exe)
        return

    if updater.is_frozen():
        threading.Thread(target=updater.cleanup_update_dir, daemon=True).start()
    # 확장 파일을 맞추고 확장 폴더에 로컬 토큰 파일(local-token.json)을 쓴다 (소스 실행 포함).
    token = local_token.get_token()
    updater.sync_bundled_extension(token)

    enable_dpi_awareness()
    config = load_config()
    diagnostics.setup_logging()
    diagnostics.log.info(
        "위젯 시작 v%s (%s%s)", __version__, "exe" if updater.is_frozen() else "소스", ", 데모" if args.demo else ""
    )
    port = args.port or int(config.get("port") or 47823)
    store = Store()

    root = tk.Tk()
    root.withdraw()  # overrideredirect·도구 창 스타일 적용 전 작업 표시줄 깜빡임 방지
    diagnostics.install_exception_hooks(root)
    try:
        server = start_server(
            store,
            port,
            info=lambda: {"extensionVersion": updater.read_manifest_version(updater.chrome_extension_dir())},
            token=token,
        )
    except OSError as e:
        diagnostics.log.error("포트 %s를 열 수 없음: %s", port, e)
        root.withdraw()
        messagebox.showerror(
            "AI 사용량 위젯",
            f"포트 {port}를 열 수 없어요.\n위젯이 이미 실행 중인지 확인하세요.",
        )
        root.destroy()
        return

    if args.demo:
        store.update(demo_payload())
        store.set_local_provider(antigravity.demo_antigravity_state())

    UsageWidget(root, store, config, demo=args.demo)
    try:
        root.mainloop()
    finally:
        diagnostics.log.info("위젯 종료")
        server.shutdown()


if __name__ == "__main__":
    main()
