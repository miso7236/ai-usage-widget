"""렌더링 방식 시안: 픽셀별 투명도를 가진 창(UpdateLayeredWindow)에 GDI+로 직접 그린다.

기본 라이브러리(ctypes, tkinter)만 쓴다. 위젯 본체와 무관하게 혼자 실행된다.

    python prototypes\\layered_render.py

- 드래그: 이동
- 오른쪽 클릭 또는 Esc: 닫기

한 창에 세 가지 변형을 위에서 아래로 그린다.
  A. 투명 배경 + 글자 외곽선 (지금의 컴팩트·투명 모드에 해당)
  B. 반투명 카드 + 그림자 (컴팩트)
  C. 반투명 카드 (상세)
"""

from __future__ import annotations

import argparse
import ctypes
import tkinter as tk
from ctypes import byref, c_float, c_uint, c_void_p, wintypes

gdip = ctypes.WinDLL("gdiplus")
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# 64비트에서 핸들이 잘리지 않도록 타입을 명시한다.
user32.GetParent.restype = c_void_p
user32.GetParent.argtypes = [c_void_p]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [c_void_p, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_ssize_t]
gdi32.CreateCompatibleDC.restype = c_void_p
gdi32.CreateCompatibleDC.argtypes = [c_void_p]
gdi32.SelectObject.restype = c_void_p
gdi32.SelectObject.argtypes = [c_void_p, c_void_p]
gdi32.CreateDIBSection.restype = c_void_p
gdi32.CreateDIBSection.argtypes = [c_void_p, c_void_p, c_uint, ctypes.POINTER(c_void_p), c_void_p, c_uint]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_byte),
    ]


user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    c_void_p, c_void_p, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    c_void_p, ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class RectF(ctypes.Structure):
    _fields_ = [("x", c_float), ("y", c_float), ("w", c_float), ("h", c_float)]


def G(name: str, *args) -> None:
    status = getattr(gdip, name)(*args)
    if status:
        raise RuntimeError(f"{name} 실패 (GDI+ 상태 {status})")


PIXEL_FORMAT_32BPP_PARGB = 0x000E200B
UNIT_PIXEL = 2
FONT_CANDIDATES = ("Pretendard", "Malgun Gothic", "Segoe UI")

FG = 0xFFFFFFFF
MUTED = 0xFFDADCE0
OK = 0xFF81C995
WARN = 0xFFFDD663
DANGER = 0xFFF28B82


def level(pct: float) -> int:
    return DANGER if pct >= 90 else WARN if pct >= 70 else OK


class Surface:
    """미리 곱한 알파(PARGB) 32비트 비트맵과 그 위의 GDI+ 그리기 도구."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = width, -height  # 음수 높이 = 위에서 아래로
        bmi.biPlanes, bmi.biBitCount = 1, 32
        bits = c_void_p()
        self.hbmp = gdi32.CreateDIBSection(None, byref(bmi), 0, byref(bits), None, 0)
        self.memdc = gdi32.CreateCompatibleDC(None)
        gdi32.SelectObject(self.memdc, self.hbmp)

        self.bitmap = c_void_p()
        G("GdipCreateBitmapFromScan0", width, height, width * 4, PIXEL_FORMAT_32BPP_PARGB, bits, byref(self.bitmap))
        self.g = c_void_p()
        G("GdipGetImageGraphicsContext", self.bitmap, byref(self.g))
        G("GdipSetSmoothingMode", self.g, 4)  # AntiAlias
        G("GdipSetPixelOffsetMode", self.g, 4)  # Half
        G("GdipSetTextRenderingHint", self.g, 4)  # AntiAlias. ClearType은 알파와 함께 못 쓰고, GridFit은 한글 자간이 들쭉날쭉해진다
        G("GdipGraphicsClear", self.g, c_uint(0))

        self.family = c_void_p()
        self.family_name = ""
        for name in FONT_CANDIDATES:
            if gdip.GdipCreateFontFamilyFromName(name, None, byref(self.family)) == 0:
                self.family_name = name
                break
        self.fmt = c_void_p()
        G("GdipStringFormatGetGenericTypographic", byref(self.fmt))
        self._fonts: dict[tuple[float, bool], c_void_p] = {}

    def _font(self, size: float, bold: bool) -> c_void_p:
        key = (size, bold)
        if key not in self._fonts:
            font = c_void_p()
            G("GdipCreateFont", self.family, c_float(size), 1 if bold else 0, UNIT_PIXEL, byref(font))
            self._fonts[key] = font
        return self._fonts[key]

    def measure(self, s: str, size: float, bold: bool = False) -> float:
        out = RectF()
        G("GdipMeasureString", self.g, s, -1, self._font(size, bold), byref(RectF(0, 0, 0, 0)), self.fmt, byref(out), None, None)
        return out.w

    def text(self, s: str, x: float, y: float, size: float, color: int, *, bold: bool = False,
             halo: bool = False, align: str = "l") -> float:
        w = self.measure(s, size, bold)
        if align == "r":
            x -= w
        rect = RectF(x, y, 0, 0)
        if halo:
            # 글자 윤곽을 어두운 반투명 선으로 두 번 둘러 어떤 배경에서도 읽히게 한다.
            path = c_void_p()
            G("GdipCreatePath", 0, byref(path))
            G("GdipAddPathString", path, s, -1, self.family, 1 if bold else 0, c_float(size), byref(rect), self.fmt)
            for argb, width in ((0x38000000, size * 0.42), (0x90000000, size * 0.2)):
                pen = c_void_p()
                G("GdipCreatePen1", c_uint(argb), c_float(width), UNIT_PIXEL, byref(pen))
                G("GdipSetPenLineJoin", pen, 2)  # Round
                G("GdipDrawPath", self.g, pen, path)
                G("GdipDeletePen", pen)
            G("GdipDeletePath", path)
        brush = c_void_p()
        G("GdipCreateSolidFill", c_uint(color), byref(brush))
        G("GdipDrawString", self.g, s, -1, self._font(size, bold), byref(rect), self.fmt, brush)
        G("GdipDeleteBrush", brush)
        return w

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float, color: int, round_caps: bool = True) -> None:
        pen = c_void_p()
        G("GdipCreatePen1", c_uint(color), c_float(width), UNIT_PIXEL, byref(pen))
        if round_caps:
            G("GdipSetPenStartCap", pen, 2)
            G("GdipSetPenEndCap", pen, 2)
        G("GdipDrawLine", self.g, pen, c_float(x1), c_float(y1), c_float(x2), c_float(y2))
        G("GdipDeletePen", pen)

    def rrect(self, x: float, y: float, w: float, h: float, r: float, fill: int, stroke: int | None = None) -> None:
        d = r * 2
        path = c_void_p()
        G("GdipCreatePath", 0, byref(path))
        for ax, ay, start in ((x, y, 180), (x + w - d, y, 270), (x + w - d, y + h - d, 0), (x, y + h - d, 90)):
            G("GdipAddPathArc", path, c_float(ax), c_float(ay), c_float(d), c_float(d), c_float(start), c_float(90))
        G("GdipClosePathFigure", path)
        brush = c_void_p()
        G("GdipCreateSolidFill", c_uint(fill), byref(brush))
        G("GdipFillPath", self.g, brush, path)
        G("GdipDeleteBrush", brush)
        if stroke is not None:
            pen = c_void_p()
            G("GdipCreatePen1", c_uint(stroke), c_float(1), UNIT_PIXEL, byref(pen))
            G("GdipDrawPath", self.g, pen, path)
            G("GdipDeletePen", pen)
        G("GdipDeletePath", path)

    def dot(self, cx: float, cy: float, d: float, color: int) -> None:
        brush = c_void_p()
        G("GdipCreateSolidFill", c_uint(color), byref(brush))
        G("GdipFillEllipse", self.g, brush, c_float(cx - d / 2), c_float(cy - d / 2), c_float(d), c_float(d))
        G("GdipDeleteBrush", brush)

    def card(self, x: float, y: float, w: float, h: float, r: float, s: float) -> None:
        for i in range(6, 0, -1):  # 겹쳐 그려 만든 부드러운 그림자
            grow = i * 1.3 * s
            self.rrect(x - grow, y - grow + 2 * s, w + grow * 2, h + grow * 2, r + grow, 0x0C000000)
        self.rrect(x, y, w, h, r, 0xD9202124, 0x30FFFFFF)

    def bar(self, x: float, y: float, w: float, h: float, pct: float, pace: float | None, color: int) -> None:
        cy = y + h / 2
        self.line(x + h / 2, cy, x + w - h / 2, cy, h, 0xFF3C4043)
        fill_w = max(h, w * min(100.0, pct) / 100.0)
        if pct > 0:
            self.line(x + h / 2, cy, x + fill_w - h / 2, cy, h, color)
        if pace is not None:
            mx = x + w * pace / 100.0
            self.line(mx, y - h * 0.3, mx, y + h * 1.3, max(1.5, h * 0.3), 0xFFFFFFFF, round_caps=False)

    def flush(self) -> None:
        G("GdipFlush", self.g, 1)


# (짧은 이름, 이름, [(창 이름, 사용률, 페이스 기준, 리셋까지)])
DEMO = [
    ("C", "Claude", [("5시간", 0, 59, "2시간 13분"), ("주간", 25, 52, "3일 4시간")]),
    ("GPT", "ChatGPT", [("5시간", 74, 48, "1시간 40분"), ("주간", 12, 30, "5일 2시간")]),
    ("Gem", "Gemini", [("5시간", 93, 34, "3시간 5분"), ("주간", 71, 60, "1일 0시간")]),
]


def draw_compact(c: Surface, x: float, y: float, s: float, halo: bool) -> float:
    """지금의 컴팩트 두 줄(윗줄 주간, 아랫줄 5시간)을 같은 내용으로 그린다. 그린 너비를 돌려준다."""
    big, small = 13 * s, 11 * s
    row2 = y + 17 * s
    c.text("▸", x, y + 1 * s, small, MUTED, halo=halo)
    c.text("5h", x, row2, small, MUTED, halo=halo)
    cx = x + 22 * s
    for idx, (short, _name, windows) in enumerate(DEMO):
        if idx:
            c.text("·", cx, y, big, MUTED, halo=halo)
            c.text("·", cx, row2, small, MUTED, halo=halo)
            cx += 12 * s
        (_, p5, pace5, _), (_, pw, pacew, _) = windows
        week, five = f"{pw}/{pacew}%", f"{p5}/{pace5}%"
        label_w = c.measure(short, small) + 5 * s
        cell_w = max(label_w + c.measure(week, big, True), c.measure(five, small, True))
        c.text(short, cx, y + 2 * s, small, MUTED, halo=halo)
        c.text(week, cx + cell_w, y, big, level(pw), bold=True, halo=halo, align="r")
        c.text(five, cx + cell_w, row2, small, level(p5), bold=True, halo=halo, align="r")
        cx += cell_w + 8 * s
    return cx - x


def draw_detail(c: Surface, x: float, y: float, s: float) -> float:
    """상세 모드 내용을 그리고 그린 높이를 돌려준다."""
    main, small = 12 * s, 10.5 * s
    cy = y
    for short, name, windows in DEMO:
        peak = max(w[1] for w in windows)
        c.dot(x + 3 * s, cy + 8 * s, 6 * s, level(peak))  # 상태 점
        c.text(name, x + 11 * s, cy, main, FG, bold=True)
        cy += 18 * s
        for label, pct, pace, reset in windows:
            c.text(label, x, cy, main, FG)
            c.bar(x + 40 * s, cy + 5.5 * s, 72 * s, 6 * s, pct, pace, level(pct))
            c.text(f"{pct}%", x + 150 * s, cy, main, FG, bold=True, align="r")
            c.text(f"기준{pace}%", x + 156 * s, cy + 1 * s, small, WARN if pct > pace else MUTED)
            c.text(reset, x + 206 * s, cy + 1 * s, small, MUTED)
            cy += 17 * s
        cy += 4 * s
    return cy - y - 4 * s


def render(scale: float) -> Surface:
    s = scale
    c = Surface(int(330 * s), int(376 * s))
    pad = 16 * s
    cap = 10.5 * s

    y = 8 * s
    c.text("A  투명 배경 + 글자 외곽선", pad, y, cap, MUTED, halo=True)
    draw_compact(c, pad, y + 18 * s, s, halo=True)

    y = 78 * s
    c.text("B  반투명 카드 + 그림자", pad, y, cap, MUTED, halo=True)
    card_y = y + 18 * s
    c.card(pad, card_y, 252 * s, 44 * s, 10 * s, s)
    draw_compact(c, pad + 10 * s, card_y + 6 * s, s, halo=False)

    y = 156 * s
    c.text(f"C  상세 카드  ({c.family_name})", pad, y, cap, MUTED, halo=True)
    card_y = y + 18 * s
    c.card(pad, card_y, 290 * s, 182 * s, 12 * s, s)
    draw_detail(c, pad + 12 * s, card_y + 9 * s, s)

    c.flush()
    return c


def main() -> None:
    parser = argparse.ArgumentParser(description="픽셀별 투명 창 렌더링 시안")
    parser.add_argument("--pos", default="240,220", help="창 위치 x,y")
    parser.add_argument("--seconds", type=float, help="이 시간 뒤 자동 종료 (캡처 확인용)")
    args = parser.parse_args()

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    token = ctypes.c_size_t()
    G("GdiplusStartup", byref(token), byref(GdiplusStartupInput(1, None, False, False)), None)

    root = tk.Tk()
    root.title("layered-render-proto")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        scale = user32.GetDpiForSystem() / 96.0
    except AttributeError:
        scale = 1.0

    surface = render(scale)
    x, y = (int(v) for v in args.pos.split(","))
    root.geometry(f"{surface.width}x{surface.height}+{x}+{y}")
    root.update()

    hwnd = user32.GetParent(root.winfo_id())
    GWL_EXSTYLE, WS_EX_LAYERED = -20, 0x00080000
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) | WS_EX_LAYERED)
    blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, 픽셀별 알파 사용
    ok = user32.UpdateLayeredWindow(
        hwnd, None, None, byref(wintypes.SIZE(surface.width, surface.height)),
        surface.memdc, byref(wintypes.POINT(0, 0)), 0, byref(blend), 2,  # ULW_ALPHA
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())

    drag = {}

    def press(e) -> None:
        drag["dx"], drag["dy"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()

    def motion(e) -> None:
        if drag:
            root.geometry(f"+{e.x_root - drag['dx']}+{e.y_root - drag['dy']}")

    root.bind("<ButtonPress-1>", press)
    root.bind("<B1-Motion>", motion)
    root.bind("<Button-3>", lambda e: root.destroy())
    root.bind("<Escape>", lambda e: root.destroy())
    if args.seconds:
        root.after(int(args.seconds * 1000), root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
