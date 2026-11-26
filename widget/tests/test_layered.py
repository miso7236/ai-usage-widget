import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import layered  # noqa: E402
import layout  # noqa: E402


@unittest.skipUnless(sys.platform == "win32", "GDI+는 Windows 전용")
class RendererTests(unittest.TestCase):
    def setUp(self):
        self.renderer = layered.Renderer()
        self.addCleanup(self.renderer.close)

    def test_measures_text(self):
        short = self.renderer.measure("42%", 12)
        self.assertGreater(short, 0)
        self.assertGreater(self.renderer.measure("42% 사용", 12), short)
        self.assertGreater(self.renderer.measure("42%", 24), short)
        self.assertGreaterEqual(self.renderer.line_height(12), 12)

    def test_unknown_font_falls_back(self):
        self.renderer.set_family("이런 글꼴은 없음")
        self.assertGreater(self.renderer.measure("가나다", 12, bold=True), 0)

    def test_paints_every_kind_of_op(self):
        scene = layout.Scene(
            120, 40,
            [
                layout.Box(0.5, 0.5, 119, 39, 6, 0xFF202124, 0xFF3C4043),
                layout.Box(4, 20, 100, 1, 0, 0xFF3C4043),
                layout.Dot(8, 8, 5, 0xFF81C995),
                layout.Bar(20, 6, 54, 5, 42, 56, 0xFF3C4043, 0xFF81C995, 0xFFFFFFFF),
                layout.Bar(20, 14, 54, 5, 0, None, 0xFF3C4043, 0xFF81C995, 0xFFFFFFFF),
                layout.Ring(100, 12, 16, 2.5, 71, 55, 0xFF3C4043, 0xFFFDD663, 0xFFFFFFFF, halo=True),
                layout.Ring(100, 12, 8, 2.5, None, None, 0xFF3C4043, 0xFF81C995, 0xFFFFFFFF),
                layout.Ring(100, 30, 8, 2.5, 100, 0, 0xFF3C4043, 0xFF81C995, 0xFFFFFFFF),
                layout.Text(4, 22, "주간 71% ↻", 11, 0xFFFFFFFF, bold=True, halo=True),
            ],
        )
        surface = self.renderer.paint(scene)
        self.addCleanup(surface.close)
        self.assertEqual((surface.width, surface.height), (120, 40))
        self.assertTrue(surface.memdc)


if __name__ == "__main__":
    unittest.main()
