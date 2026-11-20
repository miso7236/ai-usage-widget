"""Google Material Design 3 스타일 커스텀 우클릭 컨텍스트 메뉴.

Windows에서 기본 tk.Menu는 구형 Win32 네이티브 메뉴로 그려져 다크 테마·둥근 모서리·
호버 상태 레이어가 적용되지 않는다. 이 모듈은 외부 의존성 없이 순수 tkinter
Toplevel과 Windows 11 DWM API(둥근 모서리·테두리)로 Google Chrome / Material 3
다크 서피스 스타일의 컨텍스트 메뉴를 구현한다.
"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

Rect = tuple[int, int, int, int]
MenuItemKind = Literal["command", "check", "radio", "submenu", "separator", "info"]

GOOGLE_DOTS = ("#4285f4", "#ea4335", "#fbbc04", "#34a853")

MENU_PALETTE: dict[str, str] = {
    "surface": "#292a2d",
    "surface_hover": "#35363a",
    "border": "#3c4043",
    "divider": "#3c4043",
    "fg": "#e8eaed",
    "muted": "#9aa0a6",
    "disabled": "#6e7278",
    "accent": "#8ab4f8",
    "danger": "#f28b82",
    "danger_hover": "#3c2b2c",
}

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2
GA_ROOT = 2

SUBMENU_OPEN_DELAY_MS = 90
FOCUS_CHECK_DELAY_MS = 30


@dataclass(frozen=True)
class MenuItem:
    """컨텍스트 메뉴 항목 모델."""

    kind: MenuItemKind
    label: str = ""
    command: Callable[[], None] | None = None
    checked: bool = False
    enabled: bool = True
    icon: str = ""
    hint: str = ""
    danger: bool = False
    show_brand_dots: bool = False
    children: tuple["MenuItem", ...] = field(default_factory=tuple)

    @property
    def interactive(self) -> bool:
        return self.kind in ("command", "check", "radio", "submenu") and self.enabled


def hex_to_colorref(hex_color: str) -> int:
    """#RRGGBB 문자열을 Win32 COLORREF(0x00BBGGRR) 정수로 변환한다."""
    s = hex_color.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"Invalid hex color: {hex_color!r}")
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return (b << 16) | (g << 8) | r


def find_monitor_for_point(x: int, y: int, monitors: Sequence[Rect], primary: Rect) -> Rect:
    """지정 좌표가 속한 모니터 작업 영역을 찾는다. 없으면 주 모니터를 반환한다."""
    for rect in monitors:
        left, top, right, bottom = rect
        if left <= x < right and top <= y < bottom:
            return rect
    return primary


def place_popup_rect(
    anchor_x: int,
    anchor_y: int,
    width: int,
    height: int,
    monitor: Rect,
    margin: int = 6,
) -> tuple[int, int]:
    """팝업 메뉴가 모니터 경계를 벗어나지 않도록 배치 좌표를 계산한다.

    기본은 커서 오른쪽 아래에 띄우되, 오른쪽/아래쪽 공간이 부족하면 반대편으로
    자연스럽게 반전한 뒤 모니터 작업 영역 안쪽으로 보정한다.
    """
    left, top, right, bottom = monitor
    min_x = left + margin
    min_y = top + margin
    max_x = max(min_x, right - width - margin)
    max_y = max(min_y, bottom - height - margin)

    x = anchor_x
    if x + width + margin > right:
        x = anchor_x - width
    y = anchor_y
    if y + height + margin > bottom:
        y = anchor_y - height

    x = max(min_x, min(x, max_x))
    y = max(min_y, min(y, max_y))
    return int(x), int(y)


def place_submenu_rect(
    parent_rect: Rect,
    item_top: int,
    width: int,
    height: int,
    monitor: Rect,
    overlap: int = 4,
    margin: int = 6,
) -> tuple[int, int]:
    """하위 메뉴를 부모 항목 옆에 배치한다. 오른쪽 공간이 부족하면 왼쪽으로 반전한다."""
    p_left, _p_top, p_right, _p_bottom = parent_rect
    left, top, right, bottom = monitor
    min_x = left + margin
    min_y = top + margin
    max_x = max(min_x, right - width - margin)
    max_y = max(min_y, bottom - height - margin)

    x = p_right - overlap
    if x + width + margin > right:
        x = p_left - width + overlap
    y = item_top - 6  # 첫 항목이 부모 항목과 수평 정렬되도록 상단 패딩 보정

    x = max(min_x, min(x, max_x))
    y = max(min_y, min(y, max_y))
    return int(x), int(y)


def apply_win11_menu_style(hwnd: int, border_hex: str | None = None) -> bool:
    """Windows 11 DWM 둥근 모서리 및 테두리 색상을 적용한다. 지원하지 않는 OS에서는 안전하게 무시한다."""
    if sys.platform != "win32" or hwnd <= 0:
        return False
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        top_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd

        pref = ctypes.c_int(DWMWCP_ROUND)
        res = dwmapi.DwmSetWindowAttribute(
            top_hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
        if border_hex:
            color = ctypes.c_uint32(hex_to_colorref(border_hex))
            dwmapi.DwmSetWindowAttribute(
                top_hwnd,
                DWMWA_BORDER_COLOR,
                ctypes.byref(color),
                ctypes.sizeof(color),
            )
        return res == 0
    except Exception:
        return False


class _MenuPanel:
    """단일 메뉴 팝업(루트 또는 서브메뉴) 창."""

    def __init__(
        self,
        owner: "MaterialContextMenu",
        items: Sequence[MenuItem],
        font_family: str,
        monitor: Rect,
        parent_panel: "_MenuPanel | None" = None,
    ) -> None:
        self.owner = owner
        self.items = list(items)
        self.font_family = font_family
        self.monitor = monitor
        self.parent_panel = parent_panel
        self.child_panel: _MenuPanel | None = None
        self.active_index: int | None = None
        self.submenu_owner_index: int | None = None
        self._submenu_after_id: str | None = None
        self._rows: list[tuple[MenuItem, tk.Frame, list[tk.Widget], tk.Label | None, tk.Label | None, tk.Label | None]] = []

        pal = owner.palette
        win = tk.Toplevel(owner.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=pal["surface"])
        self.win = win

        self.container = tk.Frame(
            win,
            bg=pal["surface"],
            padx=4,
            pady=6,
            highlightthickness=1,
            highlightbackground=pal["border"],
        )
        self.container.pack(fill="both", expand=True)

        font_main = (font_family, 9)
        font_icon = (font_family, 9, "bold")
        font_hint = (font_family, 8)
        font_chevron = (font_family, 10)

        for idx, item in enumerate(self.items):
            if item.kind == "separator":
                sep = tk.Frame(self.container, bg=pal["divider"], height=1)
                sep.pack(fill="x", padx=8, pady=4)
                self._rows.append((item, sep, [sep], None, None, None))
                continue

            if item.kind == "info":
                row = tk.Frame(self.container, bg=pal["surface"])
                row.pack(fill="x", padx=2, pady=(2, 1))
                if item.show_brand_dots:
                    dot_d = 4
                    gap = 2
                    total_w = dot_d * 4 + gap * 3
                    dots = tk.Canvas(
                        row, width=total_w, height=dot_d, bg=pal["surface"], highlightthickness=0, bd=0
                    )
                    for di, color in enumerate(GOOGLE_DOTS):
                        x0 = di * (dot_d + gap)
                        dots.create_oval(x0, 0, x0 + dot_d, dot_d, fill=color, outline="")
                    dots.pack(side="left", padx=(8, 6), pady=4)
                lbl = tk.Label(
                    row,
                    text=item.label,
                    font=font_hint,
                    bg=pal["surface"],
                    fg=pal["muted"],
                    anchor="w",
                    padx=0 if item.show_brand_dots else 28,
                    pady=3,
                )
                lbl.pack(side="left", fill="x", expand=True)
                self._rows.append((item, row, [row, lbl], None, lbl, None))
                continue

            cursor = "hand2" if item.enabled else ""
            row = tk.Frame(self.container, bg=pal["surface"], cursor=cursor)
            row.pack(fill="x", padx=2, pady=1)

            icon_text, icon_fg = self._icon_spec(item, hovered=False)
            icon_lbl = tk.Label(
                row,
                text=icon_text,
                width=2,
                font=font_icon,
                bg=pal["surface"],
                fg=icon_fg,
                anchor="center",
                padx=4,
                pady=5,
                cursor=cursor,
            )
            icon_lbl.pack(side="left")

            text_fg = pal["fg"] if item.enabled else pal["disabled"]
            text_lbl = tk.Label(
                row,
                text=item.label,
                font=font_main,
                bg=pal["surface"],
                fg=text_fg,
                anchor="w",
                padx=2,
                pady=5,
                cursor=cursor,
            )
            text_lbl.pack(side="left", fill="x", expand=True, padx=(0, 14))

            widgets: list[tk.Widget] = [row, icon_lbl, text_lbl]
            hint_lbl: tk.Label | None = None

            if item.kind == "submenu":
                chev = tk.Label(
                    row,
                    text="›",
                    font=font_chevron,
                    bg=pal["surface"],
                    fg=pal["muted"] if item.enabled else pal["disabled"],
                    anchor="e",
                    padx=6,
                    pady=3,
                    cursor=cursor,
                )
                chev.pack(side="right")
                widgets.append(chev)

            if item.hint:
                hint_lbl = tk.Label(
                    row,
                    text=item.hint,
                    font=font_hint,
                    bg=pal["surface"],
                    fg=pal["muted"] if item.enabled else pal["disabled"],
                    anchor="e",
                    padx=4,
                    pady=5,
                    cursor=cursor,
                )
                hint_lbl.pack(side="right")
                widgets.append(hint_lbl)

            if item.interactive:
                for w in widgets:
                    w.bind("<Enter>", lambda _e, i=idx: self._on_row_enter(i))
                    w.bind("<Button-1>", lambda _e, i=idx: self._on_row_click(i))

            self._rows.append((item, row, widgets, icon_lbl, text_lbl, hint_lbl))

        win.bind("<FocusOut>", lambda _e: self.owner._schedule_focus_check())
        win.bind("<Escape>", self._on_escape)
        win.bind("<Up>", lambda _e: self._move_selection(-1))
        win.bind("<Down>", lambda _e: self._move_selection(1))
        win.bind("<Right>", self._on_right_key)
        win.bind("<Left>", self._on_left_key)
        win.bind("<Return>", self._on_activate_key)
        win.bind("<space>", self._on_activate_key)

    def _icon_spec(self, item: MenuItem, hovered: bool) -> tuple[str, str]:
        pal = self.owner.palette
        if not item.enabled:
            if item.kind == "check" and item.checked:
                return "✓", pal["disabled"]
            if item.kind == "radio" and item.checked:
                return "●", pal["disabled"]
            return item.icon, pal["disabled"]
        if item.kind == "check":
            return ("✓", pal["accent"]) if item.checked else ("", pal["muted"])
        if item.kind == "radio":
            return ("●", pal["accent"]) if item.checked else ("", pal["muted"])
        if item.danger:
            return item.icon, pal["danger"] if hovered else pal["muted"]
        return item.icon, pal["fg"] if hovered else pal["muted"]

    def show_at(self, x: int, y: int) -> None:
        self.win.update_idletasks()
        w = max(180, self.win.winfo_reqwidth())
        h = self.win.winfo_reqheight()
        px, py = place_popup_rect(x, y, w, h, self.monitor)
        self.win.geometry(f"{w}x{h}+{px}+{py}")
        self.win.deiconify()
        self.win.lift()
        apply_win11_menu_style(self.win.winfo_id(), self.owner.palette["border"])
        try:
            self.win.focus_force()
        except tk.TclError:
            pass

    def show_as_submenu(self, parent_rect: Rect, item_top: int) -> None:
        self.win.update_idletasks()
        w = max(136, self.win.winfo_reqwidth())
        h = self.win.winfo_reqheight()
        px, py = place_submenu_rect(parent_rect, item_top, w, h, self.monitor)
        self.win.geometry(f"{w}x{h}+{px}+{py}")
        self.win.deiconify()
        self.win.lift()
        apply_win11_menu_style(self.win.winfo_id(), self.owner.palette["border"])

    def _cancel_submenu_timer(self) -> None:
        if self._submenu_after_id is not None:
            try:
                self.win.after_cancel(self._submenu_after_id)
            except tk.TclError:
                pass
            self._submenu_after_id = None

    def close_child(self) -> None:
        self._cancel_submenu_timer()
        if self.child_panel is not None:
            child = self.child_panel
            self.child_panel = None
            self.submenu_owner_index = None
            self.owner._unregister_panel(child)
            child.destroy()

    def destroy(self) -> None:
        self.close_child()
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _paint_row(self, idx: int, hovered: bool) -> None:
        item, _row, widgets, icon_lbl, text_lbl, _hint_lbl = self._rows[idx]
        if not item.interactive:
            return
        pal = self.owner.palette
        if hovered:
            bg = pal["danger_hover"] if item.danger else pal["surface_hover"]
        else:
            bg = pal["surface"]
        for w in widgets:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass
        if icon_lbl is not None:
            icon_text, icon_fg = self._icon_spec(item, hovered=hovered)
            icon_lbl.configure(text=icon_text, fg=icon_fg)
        if text_lbl is not None:
            fg = pal["danger"] if (hovered and item.danger) else pal["fg"]
            text_lbl.configure(fg=fg)

    def _set_active(self, idx: int | None) -> None:
        if self.active_index == idx:
            return
        if self.active_index is not None:
            self._paint_row(self.active_index, hovered=False)
        self.active_index = idx
        if idx is not None:
            self._paint_row(idx, hovered=True)

    def _on_row_enter(self, idx: int) -> None:
        self._set_active(idx)
        item = self.items[idx]
        self._cancel_submenu_timer()
        if item.kind == "submenu" and item.children:
            if self.submenu_owner_index != idx:
                self._submenu_after_id = self.win.after(
                    SUBMENU_OPEN_DELAY_MS, lambda i=idx: self._open_submenu(i, focus_child=False)
                )
        elif self.child_panel is not None:
            self.close_child()

    def _open_submenu(self, idx: int, focus_child: bool = False) -> None:
        self._submenu_after_id = None
        if not self.win.winfo_exists():
            return
        item, row, *_ = self._rows[idx]
        if item.kind != "submenu" or not item.children:
            return
        if self.submenu_owner_index == idx and self.child_panel is not None:
            if focus_child:
                self._focus_child_first()
            return
        self.close_child()
        self.win.update_idletasks()
        px, py = self.win.winfo_rootx(), self.win.winfo_rooty()
        pw, ph = self.win.winfo_width(), self.win.winfo_height()
        item_top = row.winfo_rooty()

        child = _MenuPanel(
            owner=self.owner,
            items=item.children,
            font_family=self.font_family,
            monitor=self.monitor,
            parent_panel=self,
        )
        self.child_panel = child
        self.submenu_owner_index = idx
        self.owner._register_panel(child)
        child.show_as_submenu((px, py, px + pw, py + ph), item_top)
        if focus_child:
            self._focus_child_first()

    def _focus_child_first(self) -> None:
        if self.child_panel is None:
            return
        try:
            self.child_panel.win.focus_force()
        except tk.TclError:
            pass
        self.child_panel._move_selection(1)

    def _on_row_click(self, idx: int) -> None:
        item = self.items[idx]
        if not item.interactive:
            return
        if item.kind == "submenu":
            self._open_submenu(idx, focus_child=True)
            return
        cmd = item.command
        self.owner.close()
        if cmd is not None:
            self.owner.root.after_idle(cmd)

    def _move_selection(self, step: int) -> None:
        interactive_indices = [i for i, it in enumerate(self.items) if it.interactive]
        if not interactive_indices:
            return
        if self.active_index not in interactive_indices:
            nxt = interactive_indices[0] if step > 0 else interactive_indices[-1]
        else:
            pos = interactive_indices.index(self.active_index)
            nxt = interactive_indices[(pos + step) % len(interactive_indices)]
        self._set_active(nxt)
        if self.child_panel is not None and self.submenu_owner_index != nxt:
            self.close_child()

    def _on_right_key(self, _event) -> None:
        if self.active_index is None:
            return
        item = self.items[self.active_index]
        if item.kind == "submenu" and item.children:
            self._open_submenu(self.active_index, focus_child=True)

    def _on_left_key(self, _event) -> None:
        if self.parent_panel is not None:
            parent = self.parent_panel
            parent.close_child()
            try:
                parent.win.focus_force()
            except tk.TclError:
                pass

    def _on_escape(self, _event) -> None:
        if self.parent_panel is not None:
            parent = self.parent_panel
            parent.close_child()
            try:
                parent.win.focus_force()
            except tk.TclError:
                pass
        else:
            self.owner.close()

    def _on_activate_key(self, _event) -> None:
        if self.active_index is not None:
            self._on_row_click(self.active_index)


class MaterialContextMenu:
    """Google Material Design 3 스타일 팝업 컨텍스트 메뉴 매니저."""

    def __init__(self, root: tk.Tk, palette: dict[str, str] | None = None) -> None:
        self.root = root
        self.palette = dict(MENU_PALETTE if palette is None else palette)
        self._root_panel: _MenuPanel | None = None
        self._panels: list[_MenuPanel] = []
        self._focus_check_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self._root_panel is not None

    def _register_panel(self, panel: _MenuPanel) -> None:
        self._panels.append(panel)

    def _unregister_panel(self, panel: _MenuPanel) -> None:
        if panel in self._panels:
            self._panels.remove(panel)

    def popup(
        self,
        x_root: int,
        y_root: int,
        items: Sequence[MenuItem],
        font_family: str,
        monitors: Sequence[Rect],
        primary: Rect,
    ) -> None:
        """지정한 화면 좌표에 메뉴를 띄운다. 이미 열려 있으면 닫고 새로 연다."""
        self.close()
        monitor = find_monitor_for_point(x_root, y_root, monitors, primary)
        panel = _MenuPanel(
            owner=self,
            items=items,
            font_family=font_family,
            monitor=monitor,
        )
        self._root_panel = panel
        self._panels = [panel]
        panel.show_at(x_root, y_root)

    def close(self) -> None:
        if self._focus_check_id is not None:
            try:
                self.root.after_cancel(self._focus_check_id)
            except tk.TclError:
                pass
            self._focus_check_id = None
        if self._root_panel is not None:
            self._root_panel.destroy()
            self._root_panel = None
        self._panels.clear()

    def _schedule_focus_check(self) -> None:
        if self._focus_check_id is not None:
            try:
                self.root.after_cancel(self._focus_check_id)
            except tk.TclError:
                pass
        self._focus_check_id = self.root.after(FOCUS_CHECK_DELAY_MS, self._check_focus)

    def _check_focus(self) -> None:
        self._focus_check_id = None
        if not self._panels:
            return
        try:
            focused = self.root.focus_get()
        except tk.TclError:
            focused = None
        if focused is None:
            self.close()
            return
        try:
            top = focused.winfo_toplevel()
        except tk.TclError:
            self.close()
            return
        live_windows = [p.win for p in self._panels if p.win.winfo_exists()]
        if top not in live_windows:
            self.close()
