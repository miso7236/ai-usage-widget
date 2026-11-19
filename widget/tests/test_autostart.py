import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import autostart  # noqa: E402


class FakeKey:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeWinReg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def OpenKey(self, root, subkey, reserved, access):
        return FakeKey(self.values)

    def CreateKeyEx(self, root, subkey, reserved, access):
        return FakeKey(self.values)

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, typ, value):
        key.store[name] = value

    def DeleteValue(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


class AutostartTests(unittest.TestCase):
    def test_build_launch_command_frozen(self):
        cmd = autostart.build_launch_command(frozen=True, executable=Path(r"C:\Apps\AIUsageWidget.exe"))
        self.assertEqual(cmd, '"C:\\Apps\\AIUsageWidget.exe"')

    def test_build_launch_command_source_prefers_pythonw(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            py = tmp / "python.exe"
            pyw = tmp / "pythonw.exe"
            script = tmp / "widget.py"
            py.write_text("", encoding="utf-8")
            script.write_text("", encoding="utf-8")

            # pythonw.exe가 없으면 python.exe 사용
            cmd_py = autostart.build_launch_command(frozen=False, executable=py, script_path=script)
            self.assertEqual(cmd_py, f'"{py}" "{script}"')

            # pythonw.exe가 있으면 pythonw.exe 우선 사용
            pyw.write_text("", encoding="utf-8")
            cmd_pyw = autostart.build_launch_command(frozen=False, executable=py, script_path=script)
            self.assertEqual(cmd_pyw, f'"{pyw}" "{script}"')

    def test_enable_and_disable_roundtrip(self):
        reg = FakeWinReg()
        self.assertFalse(autostart.is_enabled(reg=reg))

        self.assertTrue(autostart.set_enabled(True, command='"C:\\Apps\\AIUsageWidget.exe"', reg=reg))
        self.assertTrue(autostart.is_enabled(reg=reg))
        self.assertEqual(reg.values[autostart.APP_VALUE_NAME], '"C:\\Apps\\AIUsageWidget.exe"')

        self.assertFalse(autostart.set_enabled(False, reg=reg))
        self.assertFalse(autostart.is_enabled(reg=reg))

        # 이미 꺼진 상태에서 다시 꺼도 오류 없이 동작
        self.assertFalse(autostart.set_enabled(False, reg=reg))


if __name__ == "__main__":
    unittest.main()
