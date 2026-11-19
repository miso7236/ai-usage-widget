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

    def test_non_windows_is_safe_noop(self):
        if sys.platform == "win32":
            self.skipTest("비-Windows 환경 전용 테스트")
        self.assertFalse(tray.is_supported())
        self.assertFalse(tray.set_click_through(1234, True))
        t = tray.SystemTray()
        self.assertFalse(t.start())
        self.assertFalse(t.active)
        t.stop()


if __name__ == "__main__":
    unittest.main()
