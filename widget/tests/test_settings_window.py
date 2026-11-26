import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings_window as sw  # noqa: E402
from state import POSITION_PRESETS  # noqa: E402


class SettingsWindowPureLogicTests(unittest.TestCase):
    def test_clamp_alpha_percent(self):
        self.assertEqual(sw.clamp_alpha_percent(92), 92)
        self.assertEqual(sw.clamp_alpha_percent(30), sw.MIN_ALPHA_PCT)
        self.assertEqual(sw.clamp_alpha_percent(140), sw.MAX_ALPHA_PCT)
        self.assertEqual(sw.clamp_alpha_percent("bad"), 92)
        self.assertEqual(sw.clamp_alpha_percent(float("nan")), 92)

    def test_clamp_scale_percent(self):
        self.assertEqual(sw.clamp_scale_percent(100), 100)
        self.assertEqual(sw.clamp_scale_percent(40), sw.MIN_SCALE_PCT)
        self.assertEqual(sw.clamp_scale_percent(260), sw.MAX_SCALE_PCT)
        self.assertEqual(sw.clamp_scale_percent("bad"), 100)
        self.assertEqual(sw.clamp_scale_percent(float("nan")), 100)

    def test_normalize_browser_interval(self):
        self.assertEqual(sw.normalize_browser_interval(5), 5)
        self.assertEqual(sw.normalize_browser_interval(0), sw.MIN_BROWSER_INTERVAL)
        self.assertEqual(sw.normalize_browser_interval(-10), sw.MIN_BROWSER_INTERVAL)
        self.assertEqual(sw.normalize_browser_interval(120), sw.MAX_BROWSER_INTERVAL)
        self.assertEqual(sw.normalize_browser_interval("bad"), 3)

    def test_rounded_rect_points_stay_inside_the_rect(self):
        points = sw.rounded_rect_points(0, 0, 100, 40, 12)
        self.assertEqual(len(points) % 2, 0)
        self.assertTrue(all(0 <= x <= 100 for x in points[0::2]))
        self.assertTrue(all(0 <= y <= 40 for y in points[1::2]))
        # 반지름이 너무 크면 짧은 변의 절반으로 줄인다
        self.assertIn(5, sw.rounded_rect_points(0, 0, 100, 10, 50)[1::2])

    def test_scroll_thumb_span(self):
        self.assertEqual(sw.scroll_thumb_span(0.0, 1.0, 200), (0.0, 200.0))  # 다 보이면 길 전체
        self.assertEqual(sw.scroll_thumb_span(0.0, 0.5, 200), (0.0, 100.0))
        self.assertEqual(sw.scroll_thumb_span(0.5, 1.0, 200), (100.0, 200.0))
        start, end = sw.scroll_thumb_span(0.99, 1.0, 200)  # 내용이 아주 길어도 손잡이는 잡을 만한 길이
        self.assertEqual(end - start, 28.0)
        self.assertEqual(end, 200.0)
        start, end = sw.scroll_thumb_span(0.0, 0.01, 200)
        self.assertEqual((start, end), (0.0, 28.0))

    def test_normalize_track_fast_sec(self):
        self.assertEqual(sw.normalize_track_fast_sec(30), 30)
        self.assertEqual(sw.normalize_track_fast_sec("60"), 60)
        self.assertEqual(sw.normalize_track_fast_sec(5), 15)  # 고를 수 없는 값은 기본 15초
        self.assertEqual(sw.normalize_track_fast_sec(None), 15)

    def test_center_window_rect_primary_and_negative_monitor(self):
        x, y = sw.center_window_rect(560, 500, (0, 0, 1920, 1080))
        self.assertEqual((x, y), (680, 290))

        # 왼쪽 보조 모니터(음수 X)에서도 중앙 배치
        nx, ny = sw.center_window_rect(560, 500, (-1920, 0, 0, 1080))
        self.assertEqual((nx, ny), (-1240, 290))

        # 창보다 작은 화면에서도 좌상단 여백을 유지하며 경계 안쪽으로 보정
        cx, cy = sw.center_window_rect(560, 500, (100, 50, 500, 400), margin=12)
        self.assertEqual((cx, cy), (112, 62))

    def test_position_grid_covers_all_position_presets(self):
        grid_keys = {key for row in sw.POSITION_GRID for key, _ in row}
        preset_keys = {key for key, _ in POSITION_PRESETS}
        self.assertEqual(grid_keys, preset_keys)

    def test_tabs_and_palette_tokens(self):
        self.assertEqual([k for k, _, _ in sw.TABS], ["services", "display", "sources", "system"])
        for token in ("bg", "sidebar", "card", "border", "fg", "muted", "accent", "switch_on", "switch_off"):
            self.assertIn(token, sw.SETTINGS_PALETTE)
            self.assertTrue(sw.SETTINGS_PALETTE[token].startswith("#"))

    def test_apply_win11_dark_titlebar_safe_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("비-Windows 환경 전용 테스트")
        self.assertFalse(sw.apply_win11_dark_titlebar(12345))
        self.assertFalse(sw.apply_win11_dark_titlebar(0))

    def test_snapshot_and_callbacks_include_adaptive_compact_color(self):
        from dataclasses import fields

        snap_names = {f.name for f in fields(sw.SettingsSnapshot)}
        cb_names = {f.name for f in fields(sw.SettingsCallbacks)}
        self.assertIn("adaptive_compact_color", snap_names)
        self.assertIn("on_set_adaptive_compact_color", cb_names)


    def test_clamp_window_size(self):
        mon = (0, 0, 1920, 1080)
        self.assertEqual(sw.clamp_window_size(720, 640, mon), (720, 640))
        # 최소 크기보다 작으면 MIN_WINDOW_*로 보정
        self.assertEqual(
            sw.clamp_window_size(200, 180, mon),
            (sw.MIN_WINDOW_WIDTH, sw.MIN_WINDOW_HEIGHT),
        )
        # 모니터보다 크면 여백을 두고 모니터 크기에 맞게 보정
        self.assertEqual(sw.clamp_window_size(3000, 2000, (0, 0, 1280, 720), margin=24), (1232, 672))


if __name__ == "__main__":
    unittest.main()
