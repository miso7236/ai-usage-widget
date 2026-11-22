"""Windows 화면 주변 띠 캡처 백엔드 (초경량 GDI BitBlt + 비동기 워커).

캡처 인터페이스를 `BaseScreenSampler`로 분리해 두어, 추후 GPU(DXGI Desktop Duplication)
백엔드로 교체하더라도 위젯 본체(`widget.py`) 코드를 거의 건드리지 않고 확장할 수 있다.
Windows가 아닌 환경에서는 자동으로 `NullScreenSampler`가 선택되어 안전하게 `None`을 반환한다.
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
from typing import Sequence

from adaptive_color import (
    RGB,
    Rect,
    extract_slot_samples_from_bgra,
    outer_capture_box,
)

GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0


class BaseScreenSampler:
    """위젯 바깥 띠 픽셀을 칸(슬롯)별 배경 RGB로 샘플링하는 백엔드 인터페이스."""

    def sample_slots(
        self,
        hwnd: int,
        slot_ratios: Sequence[tuple[float, float]],
        *,
        thickness: int = 5,
        stride: int = 2,
    ) -> list[RGB | None] | None:
        return None

    def close(self) -> None:
        pass


class NullScreenSampler(BaseScreenSampler):
    """비-Windows 환경이나 테스트용 노옵(no-op) 샘플러."""


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_uint),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]


class GDIScreenSampler(BaseScreenSampler):
    """Windows Win32 GDI BitBlt로 위젯 바깥 띠만 한 번에 읽어 슬롯별 배경색을 계산한다."""

    def __init__(self) -> None:
        self._available = sys.platform == "win32"
        self._apis: tuple | None = None

    def _get_apis(self):
        if self._apis is not None:
            return self._apis
        from ctypes import wintypes

        # 전용 WinDLL 인스턴스에 시그니처를 설정해 다른 모듈과 충돌하지 않게 한다.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND

        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL

        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HMONITOR

        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC

        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int

        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC

        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP

        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ

        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL

        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.POINTER(_BITMAPINFO),
            wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int

        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL

        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL

        self._apis = (user32, gdi32)
        return self._apis

    def sample_slots(
        self,
        hwnd: int,
        slot_ratios: Sequence[tuple[float, float]],
        *,
        thickness: int = 5,
        stride: int = 2,
    ) -> list[RGB | None] | None:
        if not self._available or hwnd <= 0 or not slot_ratios:
            return None
        try:
            user32, gdi32 = self._get_apis()
        except Exception:
            return None

        top_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        rect = _RECT()
        if not user32.GetWindowRect(top_hwnd, ctypes.byref(rect)):
            return None
        win_rect: Rect = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if win_rect[2] <= win_rect[0] or win_rect[3] <= win_rect[1]:
            return None

        hmon = user32.MonitorFromWindow(top_hwnd, MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        minfo = _MONITORINFO()
        minfo.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(minfo)):
            return None
        m = minfo.rcMonitor
        mon_rect: Rect = (int(m.left), int(m.top), int(m.right), int(m.bottom))

        box = outer_capture_box(win_rect, mon_rect, thickness=thickness)
        if box is None:
            return None
        bl, bt, br, bb = box
        bw = br - bl
        bh = bb - bt
        # 비정상적으로 큰 창 캡처 방지 (컴팩트 바는 보통 300×50 수준)
        if bw <= 0 or bh <= 0 or bw * bh > 2_000_000:
            return None

        hdc_screen = user32.GetDC(None)
        if not hdc_screen:
            return None
        hdc_mem = None
        hbmp = None
        old_obj = None
        try:
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            if not hdc_mem:
                return None
            hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, bw, bh)
            if not hbmp:
                return None
            old_obj = gdi32.SelectObject(hdc_mem, hbmp)
            if not old_obj:
                return None
            if not gdi32.BitBlt(hdc_mem, 0, 0, bw, bh, hdc_screen, bl, bt, SRCCOPY):
                return None

            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = bw
            bmi.bmiHeader.biHeight = -bh  # top-down DIB
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = BI_RGB

            buf = (ctypes.c_ubyte * (bw * bh * 4))()
            rows = gdi32.GetDIBits(
                hdc_mem,
                hbmp,
                0,
                bh,
                buf,
                ctypes.byref(bmi),
                DIB_RGB_COLORS,
            )
            if rows == 0:
                return None
            return extract_slot_samples_from_bgra(
                memoryview(buf),
                box,
                win_rect,
                slot_ratios,
                stride=stride,
            )
        except Exception:
            return None
        finally:
            if hdc_mem and old_obj:
                gdi32.SelectObject(hdc_mem, old_obj)
            if hbmp:
                gdi32.DeleteObject(hbmp)
            if hdc_mem:
                gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)


def create_default_sampler() -> BaseScreenSampler:
    """현재 OS에 맞는 기본 화면 샘플러를 생성한다."""
    return GDIScreenSampler() if sys.platform == "win32" else NullScreenSampler()


class AsyncScreenSampler:
    """단일 백그라운드 데몬 스레드로 화면 캡처를 수행해 Tk 메인 스레드 지연을 없앤다."""

    def __init__(self, sampler: BaseScreenSampler | None = None) -> None:
        self._sampler = sampler or create_default_sampler()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending: tuple[int, tuple[tuple[float, float], ...], bool] | None = None
        self._results: queue.Queue[tuple[list[RGB | None] | None, bool]] = queue.Queue(maxsize=1)
        self._thread: threading.Thread | None = None

    def _ensure_started(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="adaptive-color-sampler", daemon=True)
        self._thread.start()

    def request(
        self,
        hwnd: int,
        slot_ratios: Sequence[tuple[float, float]],
        *,
        immediate: bool = False,
    ) -> None:
        """최신 캡처 요청을 덮어쓰기 방식으로 예약한다 (밀린 요청은 자동 폐기)."""
        if self._stop.is_set() or not slot_ratios:
            return
        self._ensure_started()
        with self._lock:
            self._pending = (int(hwnd), tuple(slot_ratios), bool(immediate))
        self._wake.set()

    def poll_result(self) -> tuple[list[RGB | None] | None, bool] | None:
        """완료된 최신 샘플 결과가 있으면 반환하고, 없으면 `None`을 반환한다."""
        try:
            return self._results.get_nowait()
        except queue.Empty:
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                req = self._pending
                self._pending = None
            if req is None:
                continue
            hwnd, ratios, immediate = req
            try:
                samples = self._sampler.sample_slots(hwnd, ratios)
            except Exception:
                samples = None
            # 큐가 차 있으면 오래된 결과를 버리고 최신 결과만 유지한다.
            try:
                self._results.get_nowait()
            except queue.Empty:
                pass
            try:
                self._results.put_nowait((samples, immediate))
            except queue.Full:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._thread = None
        self._sampler.close()
