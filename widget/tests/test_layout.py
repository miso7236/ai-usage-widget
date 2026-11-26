import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import layout  # noqa: E402
from layout import Bar, Box, CompactItem, Gauge, ProviderBlock, Ring, Style, Text, WindowRow  # noqa: E402


class FakeMetrics:
    """글자 하나를 크기의 절반 너비로 치는 가짜 측정 도구."""

    def measure(self, text: str, size: float, bold: bool = False) -> float:
        return len(text) * size * (0.55 if bold else 0.5)

    def line_height(self, size: float) -> float:
        return round(size * 1.3)


COLORS = {
    role: 0xFF000000 | i
    for i, role in enumerate(("bg", "border", "fg", "muted", "bar_bg", "ok", "warn", "danger", "error", "accent", "marker"), 1)
}


def make_style(transparent: bool = False, unit: float = 1.0) -> Style:
    return Style(
        main=11 * unit, small=9 * unit, bar_w=54 * unit, bar_h=5 * unit, pad_x=4 * unit, pad_y=3 * unit,
        compact_pad_x=3 * unit, compact_pad_y=2 * unit, wrap_w=180 * unit, unit=unit,
        transparent=transparent, colors=COLORS,
    )


def make_blocks() -> tuple[ProviderBlock, ...]:
    return (
        ProviderBlock(
            "Claude", "warn", False, False, "",
            (
                WindowRow("5시간", 42.0, 56.0, "ok", "2h12m", ""),
                WindowRow("주간", 71.0, 55.0, "warn", "3d3h", "약 2일 뒤 소진"),
            ),
        ),
        ProviderBlock("Gemini", "error", False, True, "Gemini 사용량 화면을 읽지 못했어요", (WindowRow("5시간", 100.0, None, "muted", "", ""),)),
    )


def texts(scene) -> list[Text]:
    return [op for op in scene.ops if isinstance(op, Text)]


def find(scene, value: str) -> Text:
    return next(op for op in texts(scene) if op.text == value)


class WrapTextTests(unittest.TestCase):
    def test_short_text_stays_on_one_line(self):
        self.assertEqual(layout.wrap_text("짧은 문장", 100, len), ["짧은 문장"])

    def test_wraps_at_spaces(self):
        self.assertEqual(layout.wrap_text("aaa bbb ccc", 7, len), ["aaa bbb", "ccc"])

    def test_breaks_a_word_longer_than_the_line(self):
        lines = layout.wrap_text("abcdefghij", 4, len)
        self.assertEqual("".join(lines), "abcdefghij")
        self.assertTrue(all(len(line) <= 4 for line in lines))


class DetailLayoutTests(unittest.TestCase):
    def setUp(self):
        self.metrics = FakeMetrics()

    def test_everything_fits_inside_the_scene(self):
        scene = layout.layout_detail(make_blocks(), make_style(), self.metrics, clock_text="10:00", update_text="⬆ v1.2.3")
        for op in texts(scene):
            right = op.x + self.metrics.measure(op.text, op.size, op.bold)
            self.assertGreaterEqual(op.x, 0, op.text)
            self.assertLessEqual(right, scene.width, op.text)
            self.assertLessEqual(op.y + self.metrics.line_height(op.size), scene.height, op.text)
        for op in scene.ops:
            if isinstance(op, Bar):
                self.assertLessEqual(op.x + op.w, scene.width)

    def test_header_buttons_have_hit_areas(self):
        scene = layout.layout_detail(make_blocks(), make_style(), self.metrics)
        self.assertEqual(set(scene.hits), {"compact", "refresh", "settings", "close"})
        close = find(scene, "×")
        x0, y0, x1, y1 = scene.hits["close"]
        self.assertTrue(x0 <= close.x <= x1 and y0 <= close.y <= y1)
        # 버튼 영역끼리 겹치지 않는다
        rects = sorted(scene.hits[name] for name in ("refresh", "settings", "close"))
        for left, right in zip(rects, rects[1:]):
            self.assertLessEqual(left[2], right[0] + 4)

    def test_update_text_adds_a_hit_area(self):
        scene = layout.layout_detail(make_blocks(), make_style(), self.metrics, update_text="⬆ v1.2.3")
        self.assertIn("update", scene.hits)

    def test_percent_column_is_right_aligned(self):
        scene = layout.layout_detail(make_blocks(), make_style(), self.metrics)
        rights = {round(op.x + self.metrics.measure(op.text, op.size, op.bold), 3) for op in texts(scene) if op.text in ("42%", "71%", "100%")}
        self.assertEqual(len(rights), 1)

    def test_dim_provider_uses_muted_colors_and_hides_eta(self):
        scene = layout.layout_detail(make_blocks(), make_style(), self.metrics)
        self.assertEqual(find(scene, "100%").color, COLORS["muted"])
        self.assertEqual(find(scene, "42%").color, COLORS["fg"])
        dim_bar = [op for op in scene.ops if isinstance(op, Bar)][-1]
        self.assertEqual(dim_bar.fill, COLORS["muted"])
        self.assertEqual(find(scene, "↳ 약 2일 뒤 소진").color, COLORS["warn"])

    def test_bar_fill_color_follows_usage_level(self):
        scene = layout.layout_detail(make_blocks(), make_style(), self.metrics)
        bars = [op for op in scene.ops if isinstance(op, Bar)]
        self.assertEqual(bars[0].fill, COLORS["ok"])
        self.assertEqual(bars[1].fill, COLORS["warn"])
        self.assertEqual(bars[0].pace, 56.0)

    def test_opaque_background_is_a_bordered_card_with_divider(self):
        scene = layout.layout_detail(make_blocks(), make_style(transparent=False), self.metrics)
        background = scene.ops[0]
        self.assertIsInstance(background, Box)
        self.assertEqual(background.fill, COLORS["bg"])
        self.assertGreater(background.radius, 0)
        self.assertFalse(any(op.halo for op in texts(scene)))
        dividers = [op for op in scene.ops[1:] if isinstance(op, Box)]
        self.assertEqual(len(dividers), 1)

    def test_transparent_background_catches_clicks_and_outlines_text(self):
        scene = layout.layout_detail(make_blocks(), make_style(transparent=True), self.metrics)
        background = scene.ops[0]
        self.assertEqual((background.x, background.y, background.w, background.h), (0, 0, scene.width, scene.height))
        alpha = background.fill >> 24
        self.assertTrue(0 < alpha <= 2)  # 보이지 않지만 클릭은 받는다
        self.assertTrue(all(op.halo for op in texts(scene)))
        self.assertEqual([op for op in scene.ops[1:] if isinstance(op, Box)], [])

    def test_long_error_is_wrapped(self):
        blocks = (ProviderBlock("ChatGPT", "error", False, True, "아주 " * 60, ()),)
        style = make_style()
        scene = layout.layout_detail(blocks, style, self.metrics)
        error_lines = [op for op in texts(scene) if op.color == COLORS["error"]]
        self.assertGreater(len(error_lines), 1)
        for op in error_lines:
            self.assertLessEqual(self.metrics.measure(op.text, op.size), style.wrap_w)

    def test_no_providers_shows_waiting_message(self):
        scene = layout.layout_detail((), make_style(), self.metrics)
        self.assertTrue(any(op.text == "데이터 대기 중…" for op in texts(scene)))

    def test_scene_grows_with_unit(self):
        small = layout.layout_detail(make_blocks(), make_style(unit=1.0), self.metrics)
        big = layout.layout_detail(make_blocks(), make_style(unit=2.0), self.metrics)
        self.assertGreater(big.width, small.width * 1.8)
        self.assertGreater(big.height, small.height * 1.8)


class CompactLayoutTests(unittest.TestCase):
    def setUp(self):
        self.metrics = FakeMetrics()
        self.items = [
            CompactItem("C", Gauge(71.0, 55.0, "warn", "warn"), Gauge(42.0, 56.0, "ok", "ok")),
            CompactItem("GPT", Gauge(93.0, 86.0, "danger", "muted"), Gauge(12.0, 20.0, "ok", "muted")),
            CompactItem("Gem", None, Gauge(30.0, None, "muted", "muted")),
        ]

    def rings(self, scene) -> list[Ring]:
        return [op for op in scene.ops if isinstance(op, Ring)]

    def test_each_provider_gets_an_outer_and_inner_ring(self):
        scene = layout.layout_compact(self.items, make_style(), self.metrics)
        rings = self.rings(scene)
        self.assertEqual(len(rings), 6)
        outer, inner = rings[0], rings[1]
        self.assertEqual((outer.cx, outer.cy), (inner.cx, inner.cy))
        self.assertLess(inner.d + inner.t, outer.d - outer.t)  # 두 고리가 겹치지 않는다
        self.assertEqual((outer.pct, outer.fill), (71.0, COLORS["warn"]))
        self.assertEqual((inner.pct, inner.fill), (42.0, COLORS["ok"]))
        self.assertIsNone(rings[4].pct)  # 긴 주기 값이 없으면 빈 고리
        self.assertLess(rings[0].cx, rings[2].cx)

    def test_pace_is_a_marker_on_the_ring_not_a_number(self):
        scene = layout.layout_compact(self.items, make_style(), self.metrics)
        rings = self.rings(scene)
        self.assertEqual((rings[0].pace, rings[1].pace), (55.0, 56.0))
        self.assertEqual(rings[0].marker, COLORS["marker"])
        self.assertIsNone(rings[5].pace)
        self.assertFalse(any("55" in op.text or "/" in op.text for op in texts(scene)))

    def test_number_shows_long_window_and_falls_back_to_short(self):
        scene = layout.layout_compact(self.items, make_style(), self.metrics)
        self.assertEqual(find(scene, "71").color, COLORS["warn"])
        self.assertEqual(find(scene, "93").color, COLORS["danger"])
        self.assertEqual(find(scene, "30").color, COLORS["muted"])
        calm = layout.layout_compact([CompactItem("C", Gauge(20.0, None, "ok"), None)], make_style(), self.metrics)
        self.assertEqual(find(calm, "20").color, COLORS["fg"])

    def test_missing_values_show_the_mark(self):
        scene = layout.layout_compact([CompactItem("GPT", None, None, "!", "error")], make_style(), self.metrics)
        self.assertEqual(find(scene, "!").color, COLORS["error"])
        self.assertEqual(len(self.rings(scene)), 1)  # 5시간 값이 아무에게도 없으면 고리 하나

    def test_opaque_background_is_a_pill(self):
        scene = layout.layout_compact(self.items, make_style(), self.metrics)
        background = scene.ops[0]
        self.assertIsInstance(background, Box)
        self.assertAlmostEqual(background.radius, scene.height / 2, delta=1)
        self.assertFalse(any(op.halo for op in self.rings(scene)))
        clear = layout.layout_compact(self.items, make_style(transparent=True), self.metrics)
        self.assertTrue(all(op.halo for op in self.rings(clear)))

    def test_hit_areas(self):
        scene = layout.layout_compact(self.items, make_style(), self.metrics)
        self.assertEqual(set(scene.hits), {"compact"})
        scene = layout.layout_compact(self.items, make_style(), self.metrics, has_update=True)
        self.assertEqual(set(scene.hits), {"compact", "update"})

    def test_empty_shows_waiting_message(self):
        scene = layout.layout_compact([], make_style(), self.metrics)
        self.assertTrue(any(op.text == "대기 중…" for op in texts(scene)))
        self.assertIn("compact", scene.hits)

    def test_everything_fits_inside_the_scene(self):
        scene = layout.layout_compact(self.items, make_style(transparent=True), self.metrics, has_update=True)
        for op in texts(scene):
            self.assertGreaterEqual(op.x, 0, op.text)
            self.assertLessEqual(op.x + self.metrics.measure(op.text, op.size, op.bold), scene.width, op.text)
        for op in self.rings(scene):
            self.assertGreaterEqual(op.cy - (op.d + op.t) / 2, 0)
            self.assertLessEqual(op.cy + (op.d + op.t) / 2, scene.height)


class ArgbTests(unittest.TestCase):
    def test_converts_hex_colors(self):
        self.assertEqual(layout.argb("#202124"), 0xFF202124)
        self.assertEqual(layout.argb("#ffffff", 0x80), 0x80FFFFFF)


if __name__ == "__main__":
    unittest.main()
