import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from context_menu import (  # noqa: E402
    MENU_PALETTE,
    MenuItem,
    apply_win11_menu_style,
    find_monitor_for_point,
    hex_to_colorref,
    place_popup_rect,
    place_submenu_rect,
)


class ContextMenuLayoutTests(unittest.TestCase):
    def test_find_monitor_for_point_selects_matching_monitor(self):
        left_mon = (-1920, 0, 0, 1080)
        primary = (0, 0, 1920, 1080)
        right_mon = (1920, 0, 3840, 1080)
        monitors = [left_mon, primary, right_mon]

        self.assertEqual(find_monitor_for_point(-500, 300, monitors, primary), left_mon)
        self.assertEqual(find_monitor_for_point(100, 100, monitors, primary), primary)
        self.assertEqual(find_monitor_for_point(2500, 400, monitors, primary), right_mon)
        # 범위 밖 좌표는 주 모니터로 폴백
        self.assertEqual(find_monitor_for_point(5000, 5000, monitors, primary), primary)

    def test_place_popup_rect_normal_and_edge_flips(self):
        mon = (0, 0, 1920, 1080)
        # 여유 공간이 충분하면 커서 위치 그대로
        self.assertEqual(place_popup_rect(200, 150, 220, 360, mon), (200, 150))
        # 오른쪽 경계 초과 시 왼쪽으로 반전
        self.assertEqual(place_popup_rect(1850, 150, 220, 360, mon), (1630, 150))
        # 아래쪽 경계 초과 시 위쪽으로 반전
        self.assertEqual(place_popup_rect(200, 900, 220, 360, mon), (200, 540))
        # 우측 하단 모서리에서는 좌상단으로 반전
        self.assertEqual(place_popup_rect(1900, 1060, 220, 360, mon), (1680, 700))

    def test_place_popup_rect_clamps_on_negative_monitor(self):
        left_mon = (-1920, 0, 0, 1080)
        # 왼쪽 모니터 우측 끝 근처에서 반전 후 경계 안쪽 유지
        x, y = place_popup_rect(-20, 10, 240, 300, left_mon, margin=6)
        self.assertEqual((x, y), (-260, 10))
        # 반전해도 왼쪽 끝을 넘어가면 min_x로 클램핑
        x2, y2 = place_popup_rect(-1915, 5, 240, 300, left_mon, margin=6)
        self.assertEqual((x2, y2), (-1914, 6))

    def test_place_submenu_rect_right_and_left_flip(self):
        mon = (0, 0, 1920, 1080)
        # 오른쪽 공간 충분 -> 부모 오른쪽 옆에 표시
        parent = (300, 200, 520, 560)
        self.assertEqual(place_submenu_rect(parent, 320, 160, 180, mon), (516, 314))
        # 오른쪽 공간 부족 -> 부모 왼쪽으로 반전
        parent_right = (1650, 200, 1870, 560)
        self.assertEqual(place_submenu_rect(parent_right, 320, 160, 180, mon), (1494, 314))
        # 아래쪽 공간 부족 -> 모니터 하단 margin 안쪽으로 클램핑
        x, y = place_submenu_rect(parent, 980, 160, 200, mon, margin=6)
        self.assertEqual((x, y), (516, 874))


class ContextMenuModelAndStyleTests(unittest.TestCase):
    def test_hex_to_colorref_converts_rgb_to_bgr(self):
        self.assertEqual(hex_to_colorref("#3c4043"), 0x0043403C)
        self.assertEqual(hex_to_colorref("#8ab4f8"), 0x00F8B48A)
        self.assertEqual(hex_to_colorref("ff0000"), 0x000000FF)
        with self.assertRaises(ValueError):
            hex_to_colorref("#fff")

    def test_menu_item_interactive_flag(self):
        self.assertTrue(MenuItem(kind="command", label="새로고침").interactive)
        self.assertFalse(MenuItem(kind="command", label="비활성", enabled=False).interactive)
        self.assertTrue(MenuItem(kind="check", label="항상 위", checked=True).interactive)
        self.assertTrue(MenuItem(kind="radio", label="100%", checked=True).interactive)
        self.assertTrue(MenuItem(kind="submenu", label="투명도").interactive)
        self.assertFalse(MenuItem(kind="separator").interactive)
        self.assertFalse(MenuItem(kind="info", label="버전 0.6.3").interactive)

    def test_palette_contains_material3_dark_tokens(self):
        for key in ("surface", "surface_hover", "border", "divider", "fg", "muted", "accent", "danger"):
            self.assertIn(key, MENU_PALETTE)
            self.assertTrue(MENU_PALETTE[key].startswith("#"))

    def test_apply_win11_menu_style_safe_on_non_windows(self):
        if sys.platform != "win32":
            self.assertFalse(apply_win11_menu_style(12345, MENU_PALETTE["border"]))
        self.assertFalse(apply_win11_menu_style(0, MENU_PALETTE["border"]))


if __name__ == "__main__":
    unittest.main()
