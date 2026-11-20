"""AI 구독 사용량 위젯 (Windows).

화면 구석에 항상 위에 떠 있는 작은 창을 띄우고,
크롬 확장이 http://127.0.0.1:47823/update 로 보내는 사용량을 표시한다.

실행:  pythonw widget.py          (콘솔 창 없이)
       python widget.py --demo    (확장 없이 예시 데이터로 화면 확인)
조작:  드래그로 이동, 오른쪽 클릭으로 메뉴(항상 위, 투명도, 업데이트, 종료)
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

import autostart
import tray
import updater
from server import start_server
from version import __version__
from state import (
    Rect,
    Store,
    clamp_position,
    clamp_scale,
    compact_five_hour_summary,
    compact_summary,
    demo_payload,
    format_clock,
    format_eta,
    format_reset_compact,
    level,
    load_config,
    point_in_rect,
    save_config,
    scale_from_drag,
    scaled_metrics,
)

TRANSPARENT_KEY = "#010101"  # Windows -transparentcolor 크로마키 (PNG처럼 완전 투명 처리)
COLORS = {
    "bg": "#1e1e1e",
    "grip_bg": "#1e1e1e",
    "fg": "#e6e6e6",
    "muted": "#9a9a9a",
    "bar_bg": "#3a3a3a",
    "ok": "#43a047",
    "warn": "#fb8c00",
    "danger": "#e53935",
    "error": "#ef9a9a",
    "accent": "#64b5f6",
}
COLORS_TRANSPARENT = {
    "bg": TRANSPARENT_KEY,
    "grip_bg": TRANSPARENT_KEY,
    "fg": "#f5f5f5",
    "muted": "#c2c2c2",
    "bar_bg": "#3c3c3c",
    "ok": "#66bb6a",
    "warn": "#ffa726",
    "danger": "#ef5350",
    "error": "#ff8a80",
    "accent": "#82b1ff",
}
UPDATE_CHECK_MS = 6 * 60 * 60 * 1000  # 6시간마다 업데이트 확인
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


class UsageWidget:
    def __init__(self, root: tk.Tk, store: Store, config: dict) -> None:
        self.root = root
        self.store = store
        self.config = config
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
        self.backdrop: tk.Toplevel | None = self._create_backdrop()

        root.title("AI 사용량")
        root.overrideredirect(True)  # 제목 표시줄 없는 창
        root.attributes("-topmost", bool(config["topmost"]))
        root.attributes("-alpha", float(config["alpha"]))

        pal = self._palette()
        root.configure(bg=pal["bg"])

        self.frame = tk.Frame(root, bg=pal["bg"], padx=4, pady=3)
        self.frame.pack(fill="both", expand=True)

        self.header = tk.Frame(self.frame, bg=pal["bg"])
        self.header.pack(fill="x")
        self.compact_btn = tk.Label(self.header, text="▾", font=FONT_SMALL, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=0, pady=0)
        self.compact_btn.pack(side="left", padx=(0, 2))
        self.compact_btn.bind("<Button-1>", lambda e: self._toggle_compact())
        self.title_label = tk.Label(self.header, text="AI", font=FONT_BOLD, bg=pal["bg"], fg=pal["fg"], padx=0, pady=0)
        self.title_label.pack(side="left")
        self.title_label.bind("<Double-Button-1>", lambda e: self._toggle_compact())
        self.update_label = tk.Label(self.header, text="", font=FONT_SMALL, bg=pal["bg"], fg=pal["accent"], cursor="hand2", padx=0, pady=0)
        self.update_label.pack(side="left", padx=(4, 0))
        self.update_label.bind("<Button-1>", lambda e: self.start_update())
        close = tk.Label(self.header, text="×", font=FONT_BOLD, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=0, pady=0)
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.quit())
        self.refresh_btn = tk.Label(self.header, text="↻", font=FONT_BOLD, bg=pal["bg"], fg=pal["muted"], cursor="hand2", padx=0, pady=0)
        self.refresh_btn.pack(side="right", padx=(0, 3))
        self.refresh_btn.bind("<Button-1>", lambda e: self.request_refresh())
        self.clock = tk.Label(self.header, text="", font=FONT_SMALL, bg=pal["bg"], fg=pal["muted"], padx=0, pady=0)
        self.clock.pack(side="right", padx=(0, 4))
        self.close_button = close
        self.clickable_actions = self._base_clickable_actions()
        self.clickables = set(self.clickable_actions)

        self.body = tk.Frame(self.frame, bg=pal["bg"])
        self.body.pack(fill="both", expand=True, pady=(2, 0))

        self.tray = tray.SystemTray(
            tooltip="AI 사용량 위젯",
            on_toggle_visible=lambda: self.events.put(("tray_toggle_visible",)),
            on_toggle_click_through=lambda: self.events.put(("tray_toggle_click_through",)),
            on_toggle_compact=lambda: self.events.put(("tray_toggle_compact",)),
            on_toggle_transparent_bg=lambda: self.events.put(("tray_toggle_transparent_bg",)),
            on_refresh=lambda: self.events.put(("tray_refresh",)),
            on_quit=lambda: self.events.put(("tray_quit",)),
            get_state=lambda: {
                "visible": self.visible,
                "click_through": self.click_through,
                "compact": bool(self.config.get("compact", False)),
                "transparent_bg": self._is_transparent_bg(),
            },
        )
        self.tray.start()

        self.menu = self._build_menu()
        self._apply_background()
        self.render()
        self._place_initial()
        self._tick_fast()
        self._tick_slow()
        if config.get("auto_update_check", True):
            self.root.after(5000, self._auto_update_check)

    # ---------------- 배경·테마 ----------------
    def _base_clickable_actions(self) -> dict[tk.Widget, object]:
        return {
            self.close_button: self.quit,
            self.refresh_btn: self.request_refresh,
            self.update_label: self.start_update,
            self.compact_btn: self._toggle_compact,
        }

    def _hit_clickable(self, x_root: int, y_root: int):
        """투명 배경 뒤 레이어가 클릭을 받았을 때, 그 위 버튼 영역이면 해당 동작을 반환한다."""
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
        if sys.platform != "win32":
            return None
        win = tk.Toplevel(self.root)
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
        win.withdraw()
        return win

    def _is_resize_corner(self, x_root: int, y_root: int) -> bool:
        """오른쪽 아래 모서리(12x12) 영역인지 확인한다."""
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        rw = max(1, self.root.winfo_width())
        rh = max(1, self.root.winfo_height())
        return (rx + rw - 12) <= x_root <= (rx + rw + 2) and (ry + rh - 12) <= y_root <= (ry + rh + 2)

    def _on_hover_motion(self, event) -> None:
        cursor = "size_nw_se" if self._is_resize_corner(event.x_root, event.y_root) else ""
        try:
            event.widget.configure(cursor=cursor)
        except tk.TclError:
            pass

    def _on_backdrop_press(self, event) -> None:
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

    def _sync_backdrop(self) -> None:
        if self.backdrop is None:
            return
        need = self._is_transparent_bg() and self.visible and not self.click_through
        if not need:
            self.backdrop.withdraw()
            return
        self.root.update_idletasks()
        w = max(1, self.root.winfo_reqwidth())
        h = max(1, self.root.winfo_reqheight())
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.backdrop.attributes("-topmost", bool(self.config["topmost"]))
        self.backdrop.geometry(f"{w}x{h}+{x}+{y}")
        if self.backdrop.state() == "withdrawn":
            self.backdrop.deiconify()
        self.root.lift(self.backdrop)

    def _is_transparent_bg(self) -> bool:
        return sys.platform == "win32" and bool(self.config.get("transparent_bg", True))

    def _palette(self) -> dict[str, str]:
        return COLORS_TRANSPARENT if self._is_transparent_bg() else COLORS

    def _apply_background(self) -> None:
        pal = self._palette()
        if sys.platform == "win32":
            try:
                self.root.attributes("-transparentcolor", TRANSPARENT_KEY if self._is_transparent_bg() else "")
            except tk.TclError:
                pass
        self.root.configure(bg=pal["bg"])
        self.frame.configure(bg=pal["bg"])
        self.header.configure(bg=pal["bg"])
        self.body.configure(bg=pal["bg"])
        self.compact_btn.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self.title_label.configure(bg=pal["grip_bg"], fg=pal["fg"])
        self.update_label.configure(bg=pal["bg"], fg=pal["accent"])
        self.clock.configure(bg=pal["bg"], fg=pal["muted"])
        self.refresh_btn.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self.close_button.configure(bg=pal["grip_bg"], fg=pal["muted"])
        self._sync_backdrop()

    # ---------------- 메뉴 ----------------
    def _build_menu(self) -> tk.Menu:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="지금 새로고침", command=self.request_refresh)
        menu.add_separator()
        self.compact_var = tk.BooleanVar(value=bool(self.config.get("compact", False)))
        menu.add_checkbutton(label="컴팩트 모드", variable=self.compact_var, command=self._on_compact_menu)
        self.transparent_bg_var = tk.BooleanVar(value=self._is_transparent_bg())
        menu.add_checkbutton(
            label="투명 배경 (PNG)",
            variable=self.transparent_bg_var,
            command=self._on_transparent_bg_menu,
            state="normal" if sys.platform == "win32" else "disabled",
        )
        self.topmost_var = tk.BooleanVar(value=bool(self.config["topmost"]))
        menu.add_checkbutton(label="항상 위", variable=self.topmost_var, command=self._toggle_topmost)
        self.click_through_var = tk.BooleanVar(value=False)
        menu.add_checkbutton(
            label="클릭 통과 (트레이에서 해제)",
            variable=self.click_through_var,
            command=self._on_click_through_menu,
            state="normal" if self.tray.active else "disabled",
        )
        if self.tray.active:
            menu.add_command(label="트레이로 숨기기", command=self._toggle_visible)
        alpha_menu = tk.Menu(menu, tearoff=0)
        for pct in (100, 90, 80, 70, 60):
            alpha_menu.add_command(label=f"{pct}%", command=lambda p=pct: self._set_alpha(p / 100))
        menu.add_cascade(label="투명도", menu=alpha_menu)
        size_menu = tk.Menu(menu, tearoff=0)
        for pct in (70, 85, 100, 120, 140, 170, 200):
            lbl = f"{pct}% (기본)" if pct == 100 else f"{pct}%"
            size_menu.add_command(label=lbl, command=lambda p=pct: self._set_scale(p / 100))
        menu.add_cascade(label="크기 (휠/모서리 드래그)", menu=size_menu)
        menu.add_command(label="위치 초기화", command=self._reset_position)
        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        menu.add_checkbutton(
            label="Windows 시작 시 자동 실행",
            variable=self.autostart_var,
            command=self._toggle_autostart,
            state="normal" if autostart.is_supported() else "disabled",
        )
        menu.add_separator()
        menu.add_command(label="업데이트 확인", command=lambda: self.check_updates(manual=True))
        menu.add_command(label="크롬 확장 폴더 열기", command=self.open_extension_folder)
        menu.add_command(label=f"버전 {__version__}", state="disabled")
        menu.add_separator()
        menu.add_command(label="종료", command=self.quit)
        return menu

    def _on_transparent_bg_menu(self) -> None:
        self._set_transparent_bg(self.transparent_bg_var.get())

    def _toggle_transparent_bg(self) -> None:
        self._set_transparent_bg(not bool(self.config.get("transparent_bg", True)))

    def _set_transparent_bg(self, value: bool) -> None:
        self.config["transparent_bg"] = bool(value)
        self.transparent_bg_var.set(self._is_transparent_bg())
        save_config(self.config)
        self._apply_background()
        self.render()

    def request_refresh(self) -> None:
        self.store.request_refresh()
        self.clock.config(text="갱신 요청…")

    def _toggle_visible(self) -> None:
        if self.visible:
            self.root.withdraw()
            self.visible = False
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", bool(self.config["topmost"]))
            self.visible = True
        self._sync_backdrop()

    def _on_click_through_menu(self) -> None:
        self._set_click_through(self.click_through_var.get())

    def _toggle_click_through(self) -> None:
        self._set_click_through(not self.click_through)

    def _set_click_through(self, enabled: bool) -> None:
        ok = tray.set_click_through(self.root.winfo_id(), bool(enabled))
        self.click_through = bool(enabled) if ok else False
        self.click_through_var.set(self.click_through)
        self._sync_backdrop()

    def _toggle_autostart(self) -> None:
        want = bool(self.autostart_var.get())
        try:
            actual = autostart.set_enabled(want)
            self.autostart_var.set(actual)
        except OSError as e:
            self.autostart_var.set(not want)
            messagebox.showwarning(
                "AI 사용량 위젯",
                f"자동 실행 설정을 바꾸지 못했어요.\n{e}",
                parent=self.root,
            )

    def _on_compact_menu(self) -> None:
        self._set_compact(self.compact_var.get())

    def _toggle_compact(self) -> None:
        self._set_compact(not bool(self.config.get("compact", False)))

    def _set_compact(self, value: bool) -> None:
        self.config["compact"] = bool(value)
        self.compact_var.set(self.config["compact"])
        save_config(self.config)
        self.render()

    def _toggle_topmost(self) -> None:
        self.config["topmost"] = self.topmost_var.get()
        self.root.attributes("-topmost", self.config["topmost"])
        save_config(self.config)
        self._sync_backdrop()

    def _set_alpha(self, alpha: float) -> None:
        self.config["alpha"] = alpha
        self.root.attributes("-alpha", alpha)
        save_config(self.config)

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

    def _reset_position(self) -> None:
        self.config["x"] = self.config["y"] = None
        self._place_initial()
        save_config(self.config)

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

    def _end_drag(self, event) -> None:
        self._dragging = False
        self.config["x"], self.config["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.config)
        self._sync_backdrop()

    def _show_menu(self, event) -> None:
        self.root.focus_force()
        if self.backdrop is not None and self.backdrop.state() != "withdrawn":
            self.root.lift(self.backdrop)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------------- 화면 그리기 ----------------
    def _scaled_ui(self) -> tuple[dict[str, int], tuple, tuple, tuple]:
        m = scaled_metrics(self._current_scale())
        font = ("Malgun Gothic", m["font_main"])
        font_bold = ("Malgun Gothic", m["font_main"], "bold")
        font_small = ("Malgun Gothic", m["font_small"])
        return m, font, font_bold, font_small

    def render(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.clickable_actions = self._base_clickable_actions()
        self.clickables = set(self.clickable_actions)

        pal = self._palette()
        m, font, font_bold, font_small = self._scaled_ui()
        providers = self.store.snapshot()
        self.shown_version = self.store.version
        compact = bool(self.config.get("compact", False))
        self.compact_btn.config(text="▸" if compact else "▾", font=font_small)
        self.title_label.config(font=font_bold)
        self.update_label.config(font=font_small)
        self.refresh_btn.config(font=font_bold)
        self.close_button.config(font=font_bold)
        self.clock.config(
            text="" if compact else (format_clock(self.store.received_at) if self.store.received_at else ""),
            font=font_small,
        )

        # 컴팩트 모드에서는 별도 헤더를 숨기고 2줄 그리드로 점유 면적을 최소화한다.
        if compact:
            self.header.pack_forget()
            self.frame.configure(padx=m["compact_pad_x"], pady=m["compact_pad_y"])
            self.body.pack_configure(pady=(0, 0))
            self._render_compact(providers, pal, font_bold, font_small)
        else:
            if not self.header.winfo_manager():
                self.header.pack(fill="x", before=self.body)
            self.frame.configure(padx=m["pad_x"], pady=m["pad_y"])
            self.body.pack_configure(pady=(max(1, m["pad_y"] - 1), 0))
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
                    name = p.name + (" (오래됨)" if stale else "")
                    tk.Label(
                        self.body, text=name, font=font_bold, bg=pal["bg"],
                        fg=pal["muted"] if stale else pal["fg"], padx=0, pady=0,
                    ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(2 if row else 0, 0))
                    row += 1
                    dim = stale or not p.ok
                    for w in p.windows:
                        self._window_row(row, w, dim, pal, m, font, font_small)
                        row += 1
                        eta = "" if dim else format_eta(w)
                        if eta:
                            tk.Label(
                                self.body, text=f"↳ {eta}", font=font_small, bg=pal["bg"], fg=pal["warn"],
                                anchor="w", padx=0, pady=0,
                            ).grid(row=row, column=0, columnspan=4, sticky="w")
                            row += 1
                    if p.error:
                        tk.Label(
                            self.body, text=p.error, font=font_small, bg=pal["bg"], fg=pal["error"],
                            wraplength=m["wrap_w"], justify="left", padx=0, pady=0,
                        ).grid(row=row, column=0, columnspan=4, sticky="w")
                        row += 1

        self._bind_interaction(self.frame)
        self._keep_on_screen()

    def _render_compact(self, providers, pal: dict[str, str], font_bold: tuple, font_small: tuple) -> None:
        summary = compact_summary(providers)
        five_h = compact_five_hour_summary(providers)
        box = tk.Frame(self.body, bg=pal["bg"])
        box.pack(fill="x")

        expand = tk.Label(box, text="▸", font=font_small, bg=pal["grip_bg"], fg=pal["muted"], cursor="hand2", padx=2, pady=0)
        expand.grid(row=0, column=0, sticky="w", padx=(0, 2))
        expand.bind("<Button-1>", lambda e: self._toggle_compact())
        self.clickable_actions[expand] = self._toggle_compact
        self.clickables.add(expand)

        if not summary:
            lbl = tk.Label(box, text="대기 중…", font=font_small, bg=pal["bg"], fg=pal["muted"], padx=0, pady=0)
            lbl.grid(row=0, column=1, sticky="w")
            lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            return

        for idx, (short, value, color_key) in enumerate(summary):
            col = idx * 2 + 1
            if idx > 0:
                tk.Label(box, text="·", font=font_small, bg=pal["bg"], fg=pal["muted"], padx=2, pady=0).grid(
                    row=0, column=col - 1
                )
            cell = tk.Frame(box, bg=pal["bg"])
            cell.grid(row=0, column=col, sticky="e")
            s_lbl = tk.Label(cell, text=f"{short} ", font=font_small, bg=pal["bg"], fg=pal["muted"], padx=0, pady=0)
            s_lbl.pack(side="left")
            v_lbl = tk.Label(cell, text=value, font=font_bold, bg=pal["bg"], fg=pal[color_key], padx=0, pady=0)
            v_lbl.pack(side="left")
            s_lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            v_lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())

        if self.update_info:
            upd = tk.Label(box, text=" ⬆", font=font_small, bg=pal["bg"], fg=pal["accent"], cursor="hand2", padx=0, pady=0)
            upd.grid(row=0, column=len(summary) * 2, sticky="w")
            upd.bind("<Button-1>", lambda e: self.start_update())
            self.clickable_actions[upd] = self.start_update
            self.clickables.add(upd)

        if five_h:
            lbl_5h = tk.Label(box, text="5h", font=font_small, bg=pal["bg"], fg=pal["muted"], padx=1, pady=0)
            lbl_5h.grid(row=1, column=0, sticky="w", padx=(0, 2))
            lbl_5h.bind("<Double-Button-1>", lambda e: self._toggle_compact())
            for idx, (_short, value, color_key) in enumerate(five_h):
                col = idx * 2 + 1
                if idx > 0:
                    tk.Label(box, text="·", font=font_small, bg=pal["bg"], fg=pal["muted"], padx=2, pady=0).grid(
                        row=1, column=col - 1
                    )
                v_lbl = tk.Label(box, text=value, font=font_small, bg=pal["bg"], fg=pal[color_key], padx=0, pady=0)
                v_lbl.grid(row=1, column=col, sticky="e")
                v_lbl.bind("<Double-Button-1>", lambda e: self._toggle_compact())

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
        self, row: int, w, dim: bool, pal: dict[str, str], m: dict[str, int], font: tuple, font_small: tuple
    ) -> None:
        fg = pal["muted"] if dim else pal["fg"]
        bar_w, bar_h = m["bar_w"], m["bar_h"]
        tk.Label(self.body, text=w.label, font=font, bg=pal["bg"], fg=fg, anchor="w", padx=0, pady=0).grid(
            row=row, column=0, sticky="w", padx=(0, 3)
        )
        bar = tk.Canvas(self.body, width=bar_w, height=bar_h, bg=pal["bar_bg"], highlightthickness=0, bd=0)
        fill_color = pal["muted"] if dim else pal[level(w.used_percent)]
        fill_w = int(bar_w * w.used_percent / 100)
        if fill_w > 0:
            bar.create_rectangle(0, 0, fill_w, bar_h, fill=fill_color, width=0)
        bar.grid(row=row, column=1, padx=2)
        tk.Label(self.body, text=f"{round(w.used_percent)}%", font=font, bg=pal["bg"], fg=fg, anchor="e", padx=0, pady=0).grid(
            row=row, column=2, sticky="e", padx=(1, 0)
        )
        reset_str = format_reset_compact(w)
        if reset_str:
            tk.Label(self.body, text=reset_str, font=font_small, bg=pal["bg"], fg=pal["muted"], anchor="w", padx=0, pady=0).grid(
                row=row, column=3, sticky="w", padx=(3, 0)
            )

    # ---------------- 주기 작업 ----------------
    def _tick_fast(self) -> None:
        # 서버 스레드가 새 데이터를 받았는지 0.5초마다 확인 (tkinter는 화면 스레드에서만 그려야 함)
        if self.store.version != self.shown_version:
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
        self.root.after(30_000, self._tick_slow)

    # ---------------- 업데이트 ----------------
    def _auto_update_check(self) -> None:
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
                parent=self.root,
            ):
                webbrowser.open(info.page_url)
            return
        notes = info.notes[:400] + ("…" if len(info.notes) > 400 else "")
        if not messagebox.askyesno(
            "AI 사용량 위젯",
            f"새 버전 v{info.version}이 있어요. (현재 v{__version__})\n\n{notes}\n\n"
            "지금 업데이트할까요? 위젯이 잠깐 꺼졌다가 다시 켜져요.",
            parent=self.root,
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
                if manual:
                    self.start_update()
            elif manual:
                messagebox.showinfo("AI 사용량 위젯", f"최신 버전이에요. (v{__version__})", parent=self.root)
        elif kind == "check_error":
            if manual:
                messagebox.showwarning("AI 사용량 위젯", value, parent=self.root)
        elif kind == "progress":
            self.update_label.config(text=f"다운로드 {value}%")
        elif kind == "downloaded":
            try:
                updater.launch_finisher(value, updater.current_exe())
            except OSError as e:
                self.update_busy = False
                self.update_label.config(text=f"⬆ v{self.update_info.version}")
                messagebox.showerror("AI 사용량 위젯", f"업데이트를 시작하지 못했어요.\n{e}", parent=self.root)
                return
            self.update_label.config(text="업데이트 중…")
            self.root.after(100, self.quit)  # 새 exe가 이 파일을 덮어쓸 수 있도록 종료
        elif kind == "download_error":
            self.update_busy = False
            self.update_label.config(text=f"⬆ v{self.update_info.version}" if self.update_info else "")
            messagebox.showerror("AI 사용량 위젯", value, parent=self.root)
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
        elif kind == "tray_quit":
            self.quit()

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
            parent=self.root,
        )

    def quit(self) -> None:
        self.tray.stop()
        save_config(self.config)
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 구독 사용량 위젯")
    parser.add_argument("--demo", action="store_true", help="예시 데이터로 화면 확인")
    parser.add_argument("--port", type=int, help="확장이 보낼 포트 (기본 47823)")
    parser.add_argument("--finish-update", metavar="EXE", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # 업데이트 2단계: 새로 받은 exe가 기존 exe를 덮어쓰고 다시 실행한 뒤 끝난다.
    if args.finish_update:
        self_exe = updater.current_exe()
        if self_exe:
            updater.finish_update(Path(args.finish_update), self_exe)
        return

    if updater.is_frozen():
        threading.Thread(target=updater.cleanup_update_dir, daemon=True).start()
        updater.sync_bundled_extension()

    enable_dpi_awareness()
    config = load_config()
    port = args.port or int(config.get("port") or 47823)
    store = Store()

    root = tk.Tk()
    try:
        server = start_server(
            store,
            port,
            info=lambda: {"extensionVersion": updater.read_manifest_version(updater.chrome_extension_dir())},
        )
    except OSError:
        root.withdraw()
        messagebox.showerror(
            "AI 사용량 위젯",
            f"포트 {port}를 열 수 없어요.\n위젯이 이미 실행 중인지 확인하세요.",
        )
        root.destroy()
        return

    if args.demo:
        store.update(demo_payload())

    UsageWidget(root, store, config)
    try:
        root.mainloop()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
