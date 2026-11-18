"""AI 구독 사용량 위젯 (Windows).

화면 구석에 항상 위에 떠 있는 작은 창을 띄우고,
크롬 확장이 http://127.0.0.1:47823/update 로 보내는 사용량을 표시한다.

실행:  pythonw widget.py          (콘솔 창 없이)
       python widget.py --demo    (확장 없이 예시 데이터로 화면 확인)
조작:  드래그로 이동, 오른쪽 클릭으로 메뉴(항상 위, 투명도, 종료)
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from tkinter import messagebox

from server import start_server
from state import (
    Store,
    demo_payload,
    format_clock,
    format_reset,
    level,
    load_config,
    save_config,
)

COLORS = {
    "bg": "#1e1e1e",
    "fg": "#e6e6e6",
    "muted": "#8a8a8a",
    "bar_bg": "#3a3a3a",
    "ok": "#43a047",
    "warn": "#fb8c00",
    "danger": "#e53935",
    "error": "#ef9a9a",
}
FONT = ("Malgun Gothic", 9)
FONT_BOLD = ("Malgun Gothic", 9, "bold")
FONT_SMALL = ("Malgun Gothic", 8)
BAR_W, BAR_H = 90, 7


def enable_dpi_awareness() -> None:
    """고해상도 모니터에서 글자가 흐리게 보이지 않도록 한다 (Windows 전용)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class UsageWidget:
    def __init__(self, root: tk.Tk, store: Store, config: dict) -> None:
        self.root = root
        self.store = store
        self.config = config
        self.shown_version = -1
        self._drag = (0, 0)

        root.title("AI 사용량")
        root.overrideredirect(True)  # 제목 표시줄 없는 창
        root.configure(bg=COLORS["bg"])
        root.attributes("-topmost", bool(config["topmost"]))
        root.attributes("-alpha", float(config["alpha"]))

        self.frame = tk.Frame(root, bg=COLORS["bg"], padx=10, pady=8)
        self.frame.pack(fill="both", expand=True)

        header = tk.Frame(self.frame, bg=COLORS["bg"])
        header.pack(fill="x")
        tk.Label(header, text="AI 사용량", font=FONT_BOLD, bg=COLORS["bg"], fg=COLORS["fg"]).pack(side="left")
        close = tk.Label(header, text="×", font=FONT_BOLD, bg=COLORS["bg"], fg=COLORS["muted"], cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.quit())
        self.clock = tk.Label(header, text="", font=FONT_SMALL, bg=COLORS["bg"], fg=COLORS["muted"])
        self.clock.pack(side="right", padx=(0, 6))
        self.close_button = close

        self.body = tk.Frame(self.frame, bg=COLORS["bg"])
        self.body.pack(fill="both", expand=True, pady=(6, 0))

        self.menu = self._build_menu()
        self.render()
        self._place_initial()
        self._tick_fast()
        self._tick_slow()

    # ---------------- 메뉴 ----------------
    def _build_menu(self) -> tk.Menu:
        menu = tk.Menu(self.root, tearoff=0)
        self.topmost_var = tk.BooleanVar(value=bool(self.config["topmost"]))
        menu.add_checkbutton(label="항상 위", variable=self.topmost_var, command=self._toggle_topmost)
        alpha_menu = tk.Menu(menu, tearoff=0)
        for pct in (100, 90, 80, 70, 60):
            alpha_menu.add_command(label=f"{pct}%", command=lambda p=pct: self._set_alpha(p / 100))
        menu.add_cascade(label="투명도", menu=alpha_menu)
        menu.add_command(label="위치 초기화", command=self._reset_position)
        menu.add_separator()
        menu.add_command(label="종료", command=self.quit)
        return menu

    def _toggle_topmost(self) -> None:
        self.config["topmost"] = self.topmost_var.get()
        self.root.attributes("-topmost", self.config["topmost"])
        save_config(self.config)

    def _set_alpha(self, alpha: float) -> None:
        self.config["alpha"] = alpha
        self.root.attributes("-alpha", alpha)
        save_config(self.config)

    def _reset_position(self) -> None:
        self.config["x"] = self.config["y"] = None
        self._place_initial()
        save_config(self.config)

    # ---------------- 위치·드래그 ----------------
    def _place_initial(self) -> None:
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = self.config.get("x"), self.config.get("y")
        if x is None or y is None:
            x, y = sw - w - 20, sh - h - 60  # 기본: 오른쪽 아래 (작업 표시줄 위)
        x = max(0, min(int(x), sw - 50))
        y = max(0, min(int(y), sh - 30))
        self.root.geometry(f"+{x}+{y}")

    def _bind_interaction(self, widget: tk.Misc) -> None:
        if widget is not self.close_button:
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)
        widget.bind("<Button-3>", self._show_menu)
        for child in widget.winfo_children():
            self._bind_interaction(child)

    def _start_drag(self, event) -> None:
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _on_drag(self, event) -> None:
        dx, dy = self._drag
        self.root.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _end_drag(self, event) -> None:
        self.config["x"], self.config["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.config)

    def _show_menu(self, event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------------- 화면 그리기 ----------------
    def render(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

        providers = self.store.snapshot()
        self.shown_version = self.store.version
        self.clock.config(text=f"갱신 {format_clock(self.store.received_at)}" if self.store.received_at else "")

        if not providers:
            tk.Label(
                self.body,
                text="확장 프로그램 데이터를 기다리는 중…\n크롬 확장이 설치되어 있는지 확인하세요",
                font=FONT_SMALL, bg=COLORS["bg"], fg=COLORS["muted"], justify="left",
            ).grid(row=0, column=0, sticky="w")
        row = 0
        for p in providers:
            stale = p.is_stale()
            name = p.name + ("  (오래됨)" if stale else "")
            tk.Label(
                self.body, text=name, font=FONT_BOLD, bg=COLORS["bg"],
                fg=COLORS["muted"] if stale else COLORS["fg"],
            ).grid(row=row, column=0, columnspan=4, sticky="w", pady=(4 if row else 0, 1))
            row += 1
            dim = stale or not p.ok
            for w in p.windows:
                self._window_row(row, w, dim)
                row += 1
            if p.error:
                tk.Label(
                    self.body, text=p.error, font=FONT_SMALL, bg=COLORS["bg"], fg=COLORS["error"],
                    wraplength=240, justify="left",
                ).grid(row=row, column=0, columnspan=4, sticky="w")
                row += 1

        self._bind_interaction(self.frame)

    def _window_row(self, row: int, w, dim: bool) -> None:
        fg = COLORS["muted"] if dim else COLORS["fg"]
        tk.Label(self.body, text=w.label, font=FONT, bg=COLORS["bg"], fg=fg, width=9, anchor="w").grid(
            row=row, column=0, sticky="w"
        )
        bar = tk.Canvas(self.body, width=BAR_W, height=BAR_H, bg=COLORS["bar_bg"], highlightthickness=0, bd=0)
        fill_color = COLORS["muted"] if dim else COLORS[level(w.used_percent)]
        fill_w = int(BAR_W * w.used_percent / 100)
        if fill_w > 0:
            bar.create_rectangle(0, 0, fill_w, BAR_H, fill=fill_color, width=0)
        bar.grid(row=row, column=1, padx=4)
        tk.Label(self.body, text=f"{round(w.used_percent)}%", font=FONT, bg=COLORS["bg"], fg=fg, width=4, anchor="e").grid(
            row=row, column=2
        )
        tk.Label(self.body, text=format_reset(w), font=FONT_SMALL, bg=COLORS["bg"], fg=COLORS["muted"], anchor="w").grid(
            row=row, column=3, sticky="w", padx=(6, 0)
        )

    # ---------------- 주기 작업 ----------------
    def _tick_fast(self) -> None:
        # 서버 스레드가 새 데이터를 받았는지 0.5초마다 확인 (tkinter는 화면 스레드에서만 그려야 함)
        if self.store.version != self.shown_version:
            self.render()
        self.root.after(500, self._tick_fast)

    def _tick_slow(self) -> None:
        # 남은 시간과 '오래됨' 표시를 갱신
        self.render()
        self.root.after(30_000, self._tick_slow)

    def quit(self) -> None:
        save_config(self.config)
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 구독 사용량 위젯")
    parser.add_argument("--demo", action="store_true", help="예시 데이터로 화면 확인")
    parser.add_argument("--port", type=int, help="확장이 보낼 포트 (기본 47823)")
    args = parser.parse_args()

    enable_dpi_awareness()
    config = load_config()
    port = args.port or int(config.get("port") or 47823)
    store = Store()

    root = tk.Tk()
    try:
        server = start_server(store, port)
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
