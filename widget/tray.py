"""Windows 시스템 트레이 아이콘 및 클릭 통과(Click-through) 지원.

외부 패키지 없이 Windows API(ctypes)만 사용한다.
리눅스 등 비-Windows 환경에서는 안전하게 비활성화(no-op)된다.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GA_ROOT = 2


def is_supported() -> bool:
    return sys.platform == "win32"


def compute_ex_style(current_style: int, click_through: bool) -> int:
    """클릭 통과 여부에 따라 WS_EX_LAYERED / WS_EX_TRANSPARENT 비트를 계산한다."""
    style = current_style | WS_EX_LAYERED
    if click_through:
        return style | WS_EX_TRANSPARENT
    return style & ~WS_EX_TRANSPARENT


def compute_toolwindow_ex_style(current_style: int) -> int:
    """작업 표시줄과 Alt+Tab에 표시되지 않도록 WS_EX_TOOLWINDOW를 켜고 WS_EX_APPWINDOW를 끈다."""
    return (current_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW


def _apply_ex_style(hwnd_or_id: int, transform: Callable[[int], int], *, resolve_parent: bool = True, refresh_frame: bool = False) -> bool:
    if not is_supported() or not hwnd_or_id:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = int(hwnd_or_id)
        if resolve_parent:
            # GetParent는 소유된 팝업(Toplevel)에서 소유자(root)를 돌려주므로,
            # 자식->상위 창만 따라가는 GetAncestor(GA_ROOT)로 해당 창의 실제 최상위 HWND를 구한다.
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            hwnd = int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)

        LONG_PTR = ctypes.c_ssize_t
        get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = LONG_PTR
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, LONG_PTR]
        set_long.restype = LONG_PTR

        current = int(get_long(hwnd, GWL_EXSTYLE))
        updated = transform(current)
        if updated != current:
            set_long(hwnd, GWL_EXSTYLE, updated)
            if refresh_frame:
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
                ]
                user32.SetWindowPos.restype = wintypes.BOOL
                user32.SetWindowPos(
                    hwnd, 0, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
                )
        return True
    except Exception:
        return False


def set_click_through(tk_winfo_id: int, enabled: bool) -> bool:
    """Windows 최상위 창의 클릭 통과 스타일을 적용한다."""
    return _apply_ex_style(tk_winfo_id, lambda cur: compute_ex_style(cur, enabled))


def hide_from_taskbar(hwnd_or_id: int, *, resolve_parent: bool = True) -> bool:
    """창이 Windows 작업 표시줄과 Alt+Tab 목록에 뜨지 않도록 도구 창 스타일을 적용한다."""
    return _apply_ex_style(
        hwnd_or_id,
        compute_toolwindow_ex_style,
        resolve_parent=resolve_parent,
        refresh_frame=True,
    )


class SystemTray:
    """Windows 알림 영역(시스템 트레이) 아이콘.

    별도 데몬 스레드에서 숨겨진 Win32 메시지 창을 실행하고,
    사용자 메뉴 선택 시 등록된 콜백을 호출한다.
    """

    def __init__(
        self,
        tooltip: str = "AI 사용량 위젯",
        on_toggle_visible: Callable[[], None] | None = None,
        on_toggle_click_through: Callable[[], None] | None = None,
        on_toggle_compact: Callable[[], None] | None = None,
        on_toggle_transparent_bg: Callable[[], None] | None = None,
        on_refresh: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        get_state: Callable[[], dict] | None = None,
    ) -> None:
        self.tooltip = tooltip
        self.on_toggle_visible = on_toggle_visible
        self.on_toggle_click_through = on_toggle_click_through
        self.on_toggle_compact = on_toggle_compact
        self.on_toggle_transparent_bg = on_toggle_transparent_bg
        self.on_refresh = on_refresh
        self.on_quit = on_quit
        self.get_state = get_state or (lambda: {})
        self._hwnd = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        if not is_supported():
            return False
        if self._thread and self._thread.is_alive():
            return self._active
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self._active

    def stop(self) -> None:
        if not is_supported() or not self._hwnd:
            self._active = False
            return
        try:
            import ctypes
            from ctypes import wintypes

            WM_CLOSE = 0x0010
            user32 = ctypes.windll.user32
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        self._active = False

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32

            WM_NULL = 0x0000
            WM_USER = 0x0400
            WM_TRAY = WM_USER + 20
            WM_DESTROY = 0x0002
            WM_CLOSE = 0x0010
            WM_LBUTTONUP = 0x0202
            WM_LBUTTONDBLCLK = 0x0203
            WM_RBUTTONUP = 0x0205

            NIM_ADD = 0x00000000
            NIM_DELETE = 0x00000002
            NIF_MESSAGE = 0x00000001
            NIF_ICON = 0x00000002
            NIF_TIP = 0x00000004

            MF_STRING = 0x00000000
            MF_SEPARATOR = 0x00000800
            MF_CHECKED = 0x00000008
            TPM_LEFTALIGN = 0x0000
            TPM_BOTTOMALIGN = 0x0020
            TPM_RIGHTBUTTON = 0x0002
            TPM_RETURNCMD = 0x0100

            IDI_APPLICATION = 32512

            ID_TOGGLE_VISIBLE = 1001
            ID_CLICK_THROUGH = 1002
            ID_COMPACT = 1003
            ID_TRANSPARENT_BG = 1004
            ID_REFRESH = 1005
            ID_QUIT = 1006

            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(
                LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            )

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128),
                ]

            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

            # 64비트 Windows에서 핸들(HWND/HMENU/HICON)이 32비트로 잘리지 않도록 타입을 명시한다.
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
            user32.LoadIconW.restype = wintypes.HICON
            user32.CreatePopupMenu.argtypes = []
            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = [
                wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, wintypes.LPVOID,
            ]
            user32.TrackPopupMenu.restype = wintypes.UINT
            user32.DestroyMenu.argtypes = [wintypes.HMENU]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = LRESULT
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL

            def dispatch(cmd: int) -> None:
                if cmd == ID_TOGGLE_VISIBLE and self.on_toggle_visible:
                    self.on_toggle_visible()
                elif cmd == ID_CLICK_THROUGH and self.on_toggle_click_through:
                    self.on_toggle_click_through()
                elif cmd == ID_COMPACT and self.on_toggle_compact:
                    self.on_toggle_compact()
                elif cmd == ID_TRANSPARENT_BG and self.on_toggle_transparent_bg:
                    self.on_toggle_transparent_bg()
                elif cmd == ID_REFRESH and self.on_refresh:
                    self.on_refresh()
                elif cmd == ID_QUIT and self.on_quit:
                    self.on_quit()

            def show_popup(hwnd):
                menu = user32.CreatePopupMenu()
                if not menu:
                    return
                state = self.get_state() or {}
                visible = state.get("visible", True)
                click_through = bool(state.get("click_through", False))
                compact = bool(state.get("compact", False))
                transparent_bg = bool(state.get("transparent_bg", True))

                user32.AppendMenuW(menu, MF_STRING, ID_TOGGLE_VISIBLE, "위젯 숨기기" if visible else "위젯 표시")
                user32.AppendMenuW(
                    menu,
                    MF_STRING | (MF_CHECKED if click_through else 0),
                    ID_CLICK_THROUGH,
                    "클릭 통과",
                )
                user32.AppendMenuW(
                    menu,
                    MF_STRING | (MF_CHECKED if compact else 0),
                    ID_COMPACT,
                    "컴팩트 모드",
                )
                user32.AppendMenuW(
                    menu,
                    MF_STRING | (MF_CHECKED if transparent_bg else 0),
                    ID_TRANSPARENT_BG,
                    "투명 배경 (PNG)",
                )
                user32.AppendMenuW(menu, MF_STRING, ID_REFRESH, "지금 새로고침")
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "종료")

                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                user32.SetForegroundWindow(hwnd)
                cmd = int(
                    user32.TrackPopupMenu(
                        menu,
                        TPM_LEFTALIGN | TPM_BOTTOMALIGN | TPM_RIGHTBUTTON | TPM_RETURNCMD,
                        pt.x,
                        pt.y,
                        0,
                        hwnd,
                        None,
                    )
                )
                user32.PostMessageW(hwnd, WM_NULL, 0, 0)
                user32.DestroyMenu(menu)
                if cmd:
                    dispatch(cmd)

            def wnd_proc(hwnd, msg, wparam, lparam):
                if msg == WM_TRAY:
                    event = lparam & 0xFFFF
                    if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                        if self.on_toggle_visible:
                            self.on_toggle_visible()
                    elif event == WM_RBUTTONUP:
                        show_popup(hwnd)
                    return 0
                if msg == WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            hinstance = kernel32.GetModuleHandleW(None)
            class_name = "AIUsageWidgetTrayWindow"
            proc_ref = WNDPROC(wnd_proc)

            wc = WNDCLASSW()
            wc.lpfnWndProc = proc_ref
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            user32.RegisterClassW(ctypes.byref(wc))

            hwnd = user32.CreateWindowExW(
                WS_EX_TOOLWINDOW, class_name, class_name, 0, 0, 0, 0, 0, None, None, hinstance, None
            )
            if not hwnd:
                self._ready.set()
                return
            self._hwnd = hwnd

            hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = hicon
            nid.szTip = self.tooltip[:127]

            self._active = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
            self._ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._hwnd = 0
            self._active = False
        except Exception:
            self._active = False
            self._ready.set()
