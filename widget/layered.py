"""픽셀별 투명도를 가진 창에 직접 그리기 (Windows 전용).

32비트 비트맵에 GDI+로 글자·막대·카드를 그린 뒤 UpdateLayeredWindow로 창에 올린다.
크로마키(-transparentcolor)와 달리 반투명 픽셀을 쓸 수 있어 글자 가장자리가 매끄럽고,
글자 외곽선·둥근 모서리를 만들 수 있다. ctypes만 쓴다.
"""

from __future__ import annotations

import ctypes
import math
import sys
from ctypes import byref, c_float, c_uint, c_void_p

from layout import Bar, Box, Dot, Ring, Scene, Text

PIXEL_FORMAT_32BPP_PARGB = 0x000E200B
UNIT_PIXEL = 2
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 2
FALLBACK_FAMILIES = ("Malgun Gothic", "Segoe UI")
FONT_CACHE_LIMIT = 48


def is_supported() -> bool:
    return sys.platform == "win32"


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32),
    ]


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", c_void_p),
        ("SuppressBackgroundThread", ctypes.c_int), ("SuppressExternalCodecs", ctypes.c_int),
    ]


class _RectF(ctypes.Structure):
    _fields_ = [("x", c_float), ("y", c_float), ("w", c_float), ("h", c_float)]


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class Surface:
    """그림이 그려진 비트맵 한 장. 다 쓰면 close()로 돌려준다."""

    def __init__(self, renderer: "Renderer", width: int, height: int) -> None:
        self._r = renderer
        self.width, self.height = width, height
        gdi32 = renderer.gdi32
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = width, -height  # 음수 높이 = 위에서 아래로
        bmi.biPlanes, bmi.biBitCount = 1, 32
        bits = c_void_p()
        self.hbmp = gdi32.CreateDIBSection(None, byref(bmi), 0, byref(bits), None, 0)
        if not self.hbmp:
            raise OSError("CreateDIBSection 실패")
        self.memdc = gdi32.CreateCompatibleDC(None)
        self._old = gdi32.SelectObject(self.memdc, self.hbmp)
        self.bitmap = c_void_p()
        self.g = c_void_p()
        try:
            renderer.call("GdipCreateBitmapFromScan0", width, height, width * 4, PIXEL_FORMAT_32BPP_PARGB, bits, byref(self.bitmap))
            renderer.call("GdipGetImageGraphicsContext", self.bitmap, byref(self.g))
            renderer.call("GdipSetSmoothingMode", self.g, 4)  # AntiAlias
            renderer.call("GdipSetPixelOffsetMode", self.g, 4)  # Half
            # ClearType은 반투명 픽셀과 함께 쓸 수 없고, GridFit은 한글 자간이 들쭉날쭉해진다.
            renderer.call("GdipSetTextRenderingHint", self.g, 4)  # AntiAlias
            renderer.call("GdipGraphicsClear", self.g, c_uint(0))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        gdip, gdi32 = self._r.gdip, self._r.gdi32
        if self.g:
            gdip.GdipDeleteGraphics(self.g)
            self.g = c_void_p()
        if self.bitmap:
            gdip.GdipDisposeImage(self.bitmap)
            self.bitmap = c_void_p()
        if self.memdc:
            gdi32.SelectObject(self.memdc, self._old)
            gdi32.DeleteDC(self.memdc)
            self.memdc = None
        if self.hbmp:
            gdi32.DeleteObject(self.hbmp)
            self.hbmp = None


class Renderer:
    """GDI+ 시작·글꼴 관리·그리기·창에 올리기를 맡는다. layout.Metrics 역할도 한다(set_family 후)."""

    def __init__(self) -> None:
        if not is_supported():
            raise OSError("Windows에서만 쓸 수 있어요")
        self.gdip = ctypes.WinDLL("gdiplus")
        self.gdi32 = ctypes.WinDLL("gdi32")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._declare()
        self._token = ctypes.c_size_t()
        self.call("GdiplusStartup", byref(self._token), byref(_GdiplusStartupInput(1, None, 0, 0)), None)
        self._families: dict[str, c_void_p] = {}
        self._fonts: dict[tuple[str, float, bool], c_void_p] = {}
        self._line_ratio: dict[str, float] = {}
        self._fmt = c_void_p()
        self.call("GdipStringFormatGetGenericTypographic", byref(self._fmt))
        self._family_name = FALLBACK_FAMILIES[0]
        self._scratch = Surface(self, 1, 1)  # 글자 너비를 재는 용도
        self._present_failed = False

    def _declare(self) -> None:
        # 64비트에서 핸들이 32비트로 잘리지 않도록 타입을 명시한다.
        g, u = self.gdi32, self.user32
        g.CreateCompatibleDC.restype = c_void_p
        g.CreateCompatibleDC.argtypes = [c_void_p]
        g.SelectObject.restype = c_void_p
        g.SelectObject.argtypes = [c_void_p, c_void_p]
        g.DeleteDC.argtypes = [c_void_p]
        g.DeleteObject.argtypes = [c_void_p]
        g.CreateDIBSection.restype = c_void_p
        g.CreateDIBSection.argtypes = [c_void_p, c_void_p, c_uint, ctypes.POINTER(c_void_p), c_void_p, c_uint]
        self._get_long = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        self._set_long = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        self._get_long.restype = ctypes.c_ssize_t
        self._get_long.argtypes = [c_void_p, ctypes.c_int]
        self._set_long.restype = ctypes.c_ssize_t
        self._set_long.argtypes = [c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        u.GetAncestor.restype = c_void_p
        u.GetAncestor.argtypes = [c_void_p, c_uint]
        u.UpdateLayeredWindow.restype = ctypes.c_int
        u.UpdateLayeredWindow.argtypes = [
            c_void_p, c_void_p, ctypes.POINTER(_Point), ctypes.POINTER(_Size),
            c_void_p, ctypes.POINTER(_Point), ctypes.c_uint32, ctypes.POINTER(_BLENDFUNCTION), ctypes.c_uint32,
        ]

    def call(self, name: str, *args) -> None:
        status = getattr(self.gdip, name)(*args)
        if status:
            raise OSError(f"{name} 실패 (GDI+ 상태 {status})")

    # ---------------- 글꼴 ----------------
    def set_family(self, name: str) -> None:
        self._family_name = name or FALLBACK_FAMILIES[0]

    def _family(self, name: str) -> c_void_p:
        if name not in self._families:
            handle = c_void_p()
            for candidate in (name, *FALLBACK_FAMILIES):
                if self.gdip.GdipCreateFontFamilyFromName(candidate, None, byref(handle)) == 0:
                    break
            else:
                self.call("GdipGetGenericFontFamilySansSerif", byref(handle))
            self._families[name] = handle
        return self._families[name]

    def _style(self, family: c_void_p, bold: bool) -> int:
        """글꼴에 굵은(또는 보통) 모양이 없으면 있는 쪽을 쓴다."""
        want = 1 if bold else 0
        available = ctypes.c_int()
        for style in (want, 1 - want):
            if self.gdip.GdipIsStyleAvailable(family, style, byref(available)) == 0 and available.value:
                return style
        return want

    def _font(self, size: float, bold: bool) -> c_void_p:
        key = (self._family_name, round(size, 2), bold)
        font = self._fonts.get(key)
        if font is None:
            if len(self._fonts) >= FONT_CACHE_LIMIT:  # 크기 조절 드래그로 쌓인 글꼴 정리
                for old in self._fonts.values():
                    self.gdip.GdipDeleteFont(old)
                self._fonts.clear()
            family = self._family(self._family_name)
            font = c_void_p()
            self.call("GdipCreateFont", family, c_float(size), self._style(family, bold), UNIT_PIXEL, byref(font))
            self._fonts[key] = font
        return font

    def measure(self, text: str, size: float, bold: bool = False) -> float:
        out = _RectF()
        self.call(
            "GdipMeasureString", self._scratch.g, text, -1, self._font(size, bold),
            byref(_RectF(0, 0, 0, 0)), self._fmt, byref(out), None, None,
        )
        return float(out.w)

    def line_height(self, size: float) -> float:
        name = self._family_name
        ratio = self._line_ratio.get(name)
        if ratio is None:
            family = self._family(name)
            spacing, em = ctypes.c_uint16(), ctypes.c_uint16()
            ratio = 1.33
            if (
                self.gdip.GdipGetLineSpacing(family, 0, byref(spacing)) == 0
                and self.gdip.GdipGetEmHeight(family, 0, byref(em)) == 0
                and em.value
            ):
                ratio = spacing.value / em.value
            self._line_ratio[name] = ratio
        return float(round(size * ratio))

    # ---------------- 그리기 ----------------
    def paint(self, scene: Scene) -> Surface:
        surface = Surface(self, scene.width, scene.height)
        try:
            for op in scene.ops:
                if isinstance(op, Text):
                    self._text(surface, op)
                elif isinstance(op, Bar):
                    self._bar(surface, op)
                elif isinstance(op, Dot):
                    self._dot(surface, op)
                elif isinstance(op, Box):
                    self._box(surface, op)
                elif isinstance(op, Ring):
                    self._ring(surface, op)
            self.call("GdipFlush", surface.g, 1)
        except Exception:
            surface.close()
            raise
        return surface

    def _text(self, s: Surface, op: Text) -> None:
        font = self._font(op.size, op.bold)
        if op.halo:
            # 같은 글자를 어두운 반투명색으로 사방에 조금씩 밀어 찍어 외곽선을 만든다.
            # (글자 윤곽선을 따로 뽑으면 가변 글꼴·대체 글꼴에서 본문 글자와 어긋난다.)
            near = max(1.0, op.size * 0.09)
            for radius, color in ((near * 2, 0x16000000), (near, 0x58000000)):
                diag = radius * 0.7071
                offsets = (
                    (radius, 0), (-radius, 0), (0, radius), (0, -radius),
                    (diag, diag), (diag, -diag), (-diag, diag), (-diag, -diag),
                )
                self._draw_strings(s, op.text, font, [(op.x + dx, op.y + dy) for dx, dy in offsets], color)
        self._draw_strings(s, op.text, font, [(op.x, op.y)], op.color)

    def _draw_strings(self, s: Surface, text: str, font: c_void_p, points: list[tuple[float, float]], color: int) -> None:
        brush = c_void_p()
        self.call("GdipCreateSolidFill", c_uint(color), byref(brush))
        try:
            for x, y in points:
                self.call("GdipDrawString", s.g, text, -1, font, byref(_RectF(x, y, 0, 0)), self._fmt, brush)
        finally:
            self.gdip.GdipDeleteBrush(brush)

    def _stroke_path(self, s: Surface, path: c_void_p, color: int, width: float) -> None:
        pen = c_void_p()
        self.call("GdipCreatePen1", c_uint(color), c_float(width), UNIT_PIXEL, byref(pen))
        try:
            self.gdip.GdipSetPenLineJoin(pen, 2)  # Round
            self.call("GdipDrawPath", s.g, pen, path)
        finally:
            self.gdip.GdipDeletePen(pen)

    def _line(self, s: Surface, x1: float, y1: float, x2: float, y2: float, width: float, color: int, round_caps: bool) -> None:
        pen = c_void_p()
        self.call("GdipCreatePen1", c_uint(color), c_float(width), UNIT_PIXEL, byref(pen))
        try:
            if round_caps:
                self.gdip.GdipSetPenStartCap(pen, 2)
                self.gdip.GdipSetPenEndCap(pen, 2)
            self.call("GdipDrawLine", s.g, pen, c_float(x1), c_float(y1), c_float(x2), c_float(y2))
        finally:
            self.gdip.GdipDeletePen(pen)

    def _bar(self, s: Surface, op: Bar) -> None:
        h = max(2.0, op.h)
        w = max(h + 2, op.w)
        cy = op.y + h / 2
        self._line(s, op.x + h / 2, cy, op.x + w - h / 2, cy, h, op.track, True)
        pct = max(0.0, min(100.0, op.pct))
        if pct > 0:
            fill_w = max(h, w * pct / 100.0)
            if fill_w <= h + 0.01:
                self._dot(s, Dot(op.x + h / 2, cy, h, op.fill))
            else:
                self._line(s, op.x + h / 2, cy, op.x + fill_w - h / 2, cy, h, op.fill, True)
        if op.pace is not None:
            mx = op.x + w * max(0.0, min(100.0, op.pace)) / 100.0
            mx = max(op.x + 1, min(op.x + w - 1, mx))
            self._line(s, mx, op.y - h * 0.25, mx, op.y + h * 1.25, max(1.5, h * 0.3), op.marker, False)

    def _arc(self, s: Surface, op: Ring, start: float, sweep: float, width: float, color: int, round_caps: bool) -> None:
        pen = c_void_p()
        self.call("GdipCreatePen1", c_uint(color), c_float(width), UNIT_PIXEL, byref(pen))
        try:
            if round_caps:
                self.gdip.GdipSetPenStartCap(pen, 2)
                self.gdip.GdipSetPenEndCap(pen, 2)
            self.call(
                "GdipDrawArc", s.g, pen,
                c_float(op.cx - op.d / 2), c_float(op.cy - op.d / 2), c_float(op.d), c_float(op.d),
                c_float(start), c_float(sweep),
            )
        finally:
            self.gdip.GdipDeletePen(pen)

    def _ring(self, s: Surface, op: Ring) -> None:
        """12시에서 시작해 시계 방향으로 차오르는 고리. 기준값은 고리를 가로지르는 눈금으로 표시한다."""
        if op.halo:
            self._arc(s, op, 0, 360, op.t + max(2.0, op.t * 0.8), 0x58000000, False)
        self._arc(s, op, 0, 360, op.t, op.track, False)
        pct = max(0.0, min(100.0, op.pct or 0.0))
        if pct >= 99.5:
            self._arc(s, op, 0, 360, op.t, op.fill, False)
        elif pct > 0:
            self._arc(s, op, -90, max(1.0, 360 * pct / 100.0), op.t, op.fill, True)
        if op.gain > 0 and pct > 0:
            gain = min(pct, op.gain)
            self._arc(s, op, -90 + 360 * (pct - gain) / 100.0, max(1.0, 360 * gain / 100.0), op.t, op.gain_color, True)
        if op.pace is not None:
            angle = math.radians(-90 + 360 * max(0.0, min(100.0, op.pace)) / 100.0)
            dx, dy = math.cos(angle), math.sin(angle)
            r0, r1 = op.d / 2 - op.t * 0.75, op.d / 2 + op.t * 0.75
            self._line(
                s, op.cx + dx * r0, op.cy + dy * r0, op.cx + dx * r1, op.cy + dy * r1,
                max(1.5, op.t * 0.45), op.marker, False,
            )

    def _dot(self, s: Surface, op: Dot) -> None:
        brush = c_void_p()
        self.call("GdipCreateSolidFill", c_uint(op.color), byref(brush))
        try:
            self.call(
                "GdipFillEllipse", s.g, brush,
                c_float(op.cx - op.d / 2), c_float(op.cy - op.d / 2), c_float(op.d), c_float(op.d),
            )
        finally:
            self.gdip.GdipDeleteBrush(brush)

    def _box(self, s: Surface, op: Box) -> None:
        brush = c_void_p()
        self.call("GdipCreateSolidFill", c_uint(op.fill), byref(brush))
        try:
            if op.radius <= 0:
                self.call("GdipFillRectangle", s.g, brush, c_float(op.x), c_float(op.y), c_float(op.w), c_float(op.h))
                return
            d = min(op.radius * 2, op.w, op.h)
            path = c_void_p()
            self.call("GdipCreatePath", 0, byref(path))
            try:
                corners = (
                    (op.x, op.y, 180), (op.x + op.w - d, op.y, 270),
                    (op.x + op.w - d, op.y + op.h - d, 0), (op.x, op.y + op.h - d, 90),
                )
                for ax, ay, start in corners:
                    self.call("GdipAddPathArc", path, c_float(ax), c_float(ay), c_float(d), c_float(d), c_float(start), c_float(90))
                self.call("GdipClosePathFigure", path)
                self.call("GdipFillPath", s.g, brush, path)
                if op.stroke is not None:
                    self._stroke_path(s, path, op.stroke, 1.0)
            finally:
                self.gdip.GdipDeletePath(path)
        finally:
            self.gdip.GdipDeleteBrush(brush)

    # ---------------- 창에 올리기 ----------------
    def toplevel_hwnd(self, tk_winfo_id: int) -> int:
        return int(self.user32.GetAncestor(tk_winfo_id, 2) or tk_winfo_id)  # GA_ROOT

    def present(self, hwnd: int, surface: Surface, alpha: float = 1.0) -> bool:
        """그린 비트맵을 창에 올린다. alpha는 창 전체에 곱해지는 불투명도(0~1)."""
        blend = _BLENDFUNCTION(0, 0, max(1, min(255, round(alpha * 255))), 1)  # AC_SRC_OVER, 픽셀별 알파
        size = _Size(surface.width, surface.height)
        origin = _Point(0, 0)

        def update() -> bool:
            return bool(
                self.user32.UpdateLayeredWindow(hwnd, None, None, byref(size), surface.memdc, byref(origin), 0, byref(blend), ULW_ALPHA)
            )

        ex = int(self._get_long(hwnd, GWL_EXSTYLE))
        if not ex & WS_EX_LAYERED:
            self._set_long(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        if update():
            return True
        # 다른 방식의 투명 설정(SetLayeredWindowAttributes)이 걸려 있으면 비트를 껐다 켜야 받아들인다.
        self._set_long(hwnd, GWL_EXSTYLE, ex & ~WS_EX_LAYERED)
        self._set_long(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        return update()

    def close(self) -> None:
        self._scratch.close()
        for font in self._fonts.values():
            self.gdip.GdipDeleteFont(font)
        self._fonts.clear()
        for family in self._families.values():
            self.gdip.GdipDeleteFontFamily(family)
        self._families.clear()
        if self._token:
            self.gdip.GdiplusShutdown(self._token)
            self._token = ctypes.c_size_t()
