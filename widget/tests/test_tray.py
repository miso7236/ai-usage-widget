import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tray  # noqa: E402


class TrayTests(unittest.TestCase):
    def test_compute_ex_style_enables_layered_and_transparent(self):
        base = 0x00000100
        enabled = tray.compute_ex_style(base, True)
        self.assertTrue(enabled & tray.WS_EX_LAYERED)
        self.assertTrue(enabled & tray.WS_EX_TRANSPARENT)

        disabled = tray.compute_ex_style(enabled, False)
        self.assertTrue(disabled & tray.WS_EX_LAYERED)
        self.assertFalse(disabled & tray.WS_EX_TRANSPARENT)

    def test_compute_toolwindow_ex_style_hides_from_taskbar(self):
        base = tray.WS_EX_APPWINDOW | tray.WS_EX_LAYERED
        updated = tray.compute_toolwindow_ex_style(base)
        self.assertTrue(updated & tray.WS_EX_TOOLWINDOW)
        self.assertFalse(updated & tray.WS_EX_APPWINDOW)
        self.assertTrue(updated & tray.WS_EX_LAYERED)

    def test_non_windows_is_safe_noop(self):
        if sys.platform == "win32":
            self.skipTest("비-Windows 환경 전용 테스트")
        self.assertFalse(tray.is_supported())
        self.assertFalse(tray.set_click_through(1234, True))
        self.assertFalse(tray.hide_from_taskbar(1234))
        called = []
        t = tray.SystemTray(on_open_settings=lambda: called.append(True))
        self.assertIsNotNone(t.on_open_settings)
        self.assertFalse(t.start())
        self.assertFalse(t.active)
        t.stop()


if __name__ == "__main__":
    unittest.main()
