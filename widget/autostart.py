"""Windows 시작 시 자동 실행 등록/해제.

HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 레지스트리를 사용한다.
tkinter에 의존하지 않아 리눅스 CI에서도 테스트할 수 있다.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE_NAME = "AIUsageWidget"


def is_supported() -> bool:
    return sys.platform == "win32"


def build_launch_command(
    frozen: bool | None = None,
    executable: Path | None = None,
    script_path: Path | None = None,
) -> str:
    """Windows 시작 시 실행할 명령 문자열을 만든다."""
    is_exe = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    exe = Path(executable) if executable is not None else Path(sys.executable)
    if is_exe:
        return f'"{exe}"'
    script = Path(script_path) if script_path is not None else (Path(__file__).resolve().parent / "widget.py")
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" "{script}"'


def _load_winreg(reg=None):
    if reg is not None:
        return reg
    if not is_supported():
        return None
    import winreg

    return winreg


def is_enabled(reg=None) -> bool:
    """자동 실행이 등록되어 있는지 확인한다."""
    winreg = _load_winreg(reg)
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool, command: str | None = None, reg=None) -> bool:
    """자동 실행을 켜거나 끈다. 지원하지 않는 OS에서는 False를 반환한다."""
    winreg = _load_winreg(reg)
    if winreg is None:
        return False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            cmd = command or build_launch_command()
            winreg.SetValueEx(key, APP_VALUE_NAME, 0, winreg.REG_SZ, cmd)
            return True
        try:
            winreg.DeleteValue(key, APP_VALUE_NAME)
        except FileNotFoundError:
            pass
        return False
