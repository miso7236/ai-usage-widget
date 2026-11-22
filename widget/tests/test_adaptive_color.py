import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adaptive_color as ac  # noqa: E402
import screen_sampler as ss  # noqa: E402


def _make_bgra_canvas(
    box: ac.Rect,
    win_rect: ac.Rect,
    *,
    left_outer_rgb: ac.RGB,
    right_outer_rgb: ac.RGB,
    split_x: int,
    inside_rgb: ac.RGB = (255, 0, 255),
) -> bytearray:
    """테스트용 top-down 32bpp BGRA 버퍼를 생성한다 (창 내부는 마젠타로 채워 미읽기 검증)."""
    bl, bt, br, bb = box
    wl, wt, wr, wb = win_rect
    bw = br - bl
    bh = bb - bt
    buf = bytearray(bw * bh * 4)
    for ly in range(bh):
        gy = bt + ly
        for lx in range(bw):
            gx = bl + lx
            if wl <= gx < wr and wt <= gy < wb:
                r, g, b = inside_rgb
            elif gx < split_x:
                r, g, b = left_outer_rgb
            else:
                r, g, b = right_outer_rgb
            off = (ly * bw + lx) * 4
            buf[off] = b
            buf[off + 1] = g
            buf[off + 2] = r
            buf[off + 3] = 255
    return buf


class AdaptiveColorPureLogicTests(unittest.TestCase):
    def test_parse_and_format_hex(self):
        self.assertEqual(ac.parse_hex("#010203"), (1, 2, 3))
        self.assertEqual(ac.parse_hex("#fff"), (255, 255, 255))
        self.assertEqual(ac.to_hex((255, 0, 128)), "#ff0080")
        self.assertEqual(ac.to_hex((-5, 300, 12.6)), "#00ff0d")
        with self.assertRaises(ValueError):
            ac.parse_hex("#12345")

    def test_luminance_and_contrast_known_values(self):
        self.assertAlmostEqual(ac.relative_luminance((0, 0, 0)), 0.0, places=5)
        self.assertAlmostEqual(ac.relative_luminance((255, 255, 255)), 1.0, places=5)
        self.assertAlmostEqual(ac.contrast_ratio((255, 255, 255), (0, 0, 0)), 21.0, places=2)
        self.assertAlmostEqual(ac.contrast_ratio((120, 80, 40), (120, 80, 40)), 1.0, places=5)

    def test_ensure_contrast_meets_wcag_across_backgrounds(self):
        backgrounds = [
            (255, 255, 255),
            (0, 0, 0),
            (118, 118, 118),
            (235, 64, 52),
            (46, 204, 113),
            (241, 196, 15),
            (52, 152, 219),
            (155, 89, 182),
            (240, 244, 249),
        ]
        for bg in backgrounds:
            theme = "light" if ac.relative_luminance(bg) > 0.5 else "dark"
            key = ac.chroma_key_for(theme)
            pal = ac.build_slot_palette(theme, bg, chroma_key=key, min_contrast=4.5)
            for role in ac.FOREGROUND_ROLES:
                fg = ac.parse_hex(pal[role])
                ratio = ac.contrast_ratio(fg, bg)
                self.assertGreaterEqual(ratio, 4.5, f"role={role} on bg={bg} got {ratio:.2f}")
                self.assertNotEqual(pal[role], key, f"role={role} collided with chroma key {key}")

    def test_avoid_chroma_key(self):
        self.assertNotEqual(ac.avoid_chroma_key((1, 1, 1), ac.CHROMA_KEY_DARK), (1, 1, 1))
        self.assertNotEqual(ac.avoid_chroma_key((254, 254, 254), ac.CHROMA_KEY_LIGHT), (254, 254, 254))
        self.assertEqual(ac.avoid_chroma_key((100, 120, 140), ac.CHROMA_KEY_DARK), (100, 120, 140))

    def test_robust_bg_color_median_and_empty(self):
        self.assertIsNone(ac.robust_bg_color([]))
        # 순수 검정 배경도 유효한 배경색으로 인식해야 어두운 창으로 복귀할 수 있다.
        self.assertEqual(ac.robust_bg_color([(0, 0, 0), (0, 0, 0)]), (0, 0, 0))
        # 이상치(창 테두리 등)가 섞여도 중앙값 유지
        samples = [(20, 20, 20)] * 7 + [(255, 255, 255), (250, 250, 250)]
        self.assertEqual(ac.robust_bg_color(samples), (20, 20, 20))

    def test_hysteresis_prevents_flapping(self):
        # 중간 회색(상대 휘도 ~0.50: RGB 약 188)에서는 기존 테마를 유지해야 한다.
        mid = (188, 188, 188)
        self.assertEqual(ac.slot_theme(mid, "dark"), "dark")
        self.assertEqual(ac.slot_theme(mid, "light"), "light")
        self.assertEqual(ac.slot_theme((245, 245, 245), "dark"), "light")
        self.assertEqual(ac.slot_theme((20, 20, 20), "light"), "dark")

        # 전역 크로마키도 넓은 히스테리시스(0.35~0.65) 유지
        self.assertEqual(ac.global_chroma_key([0.50, 0.52], ac.CHROMA_KEY_DARK), ac.CHROMA_KEY_DARK)
        self.assertEqual(ac.global_chroma_key([0.50, 0.52], ac.CHROMA_KEY_LIGHT), ac.CHROMA_KEY_LIGHT)
        self.assertEqual(ac.global_chroma_key([0.80, 0.75], ac.CHROMA_KEY_DARK), ac.CHROMA_KEY_LIGHT)
        self.assertEqual(ac.global_chroma_key([0.15, 0.20], ac.CHROMA_KEY_LIGHT), ac.CHROMA_KEY_DARK)

    def test_ring_sample_rects_clipping_and_negative_monitor(self):
        # 화면 최상단에 붙은 창: top 띠는 None, bottom/left/right는 유효
        rects = ac.ring_sample_rects((100, 0, 360, 44), (0, 0, 1920, 1080), thickness=5)
        self.assertIsNone(rects["top"])
        self.assertEqual(rects["bottom"], (100, 44, 360, 49))
        self.assertEqual(rects["left"], (95, 0, 100, 44))
        self.assertEqual(rects["right"], (360, 0, 365, 44))

        # 왼쪽 보조 모니터(음수 X 좌표)
        neg_box = ac.outer_capture_box((-1000, 100, -740, 144), (-1920, 0, 0, 1080), thickness=5)
        self.assertEqual(neg_box, (-1005, 95, -735, 149))

    def test_extract_slot_samples_ignores_inside_and_splits_horizontally(self):
        mon = (0, 0, 1920, 1080)
        win = (100, 100, 300, 140)
        box = ac.outer_capture_box(win, mon, thickness=5)
        assert box is not None
        buf = _make_bgra_canvas(
            box,
            win,
            left_outer_rgb=(15, 20, 25),
            right_outer_rgb=(245, 240, 235),
            split_x=200,
            inside_rgb=(255, 0, 255),
        )
        samples = ac.extract_slot_samples_from_bgra(
            buf, box, win, [(0.0, 0.45), (0.55, 1.0)], stride=1
        )
        self.assertEqual(samples, [(15, 20, 25), (245, 240, 235)])

    def test_extract_slot_samples_when_flush_to_top_edge(self):
        mon = (0, 0, 1920, 1080)
        win = (100, 0, 300, 40)
        box = ac.outer_capture_box(win, mon, thickness=5)
        assert box is not None
        buf = _make_bgra_canvas(
            box,
            win,
            left_outer_rgb=(12, 12, 12),
            right_outer_rgb=(240, 240, 240),
            split_x=200,
        )
        samples = ac.extract_slot_samples_from_bgra(
            buf, box, win, [(0.0, 0.45), (0.55, 1.0)], stride=1
        )
        self.assertEqual(samples, [(12, 12, 12), (240, 240, 240)])

    def test_color_animator_smooth_convergence(self):
        anim = ac.ColorAnimator(alpha=0.3, snap_eps=1.0)
        # 첫 목표는 즉시 초기화(깜빡임 방지)
        self.assertFalse(anim.set_targets({(0, "fg"): (255, 255, 255)}))
        self.assertEqual(anim.current_hex((0, "fg")), "#ffffff")

        # 새 목표로 전환 시 보간 필요
        self.assertTrue(anim.set_targets({(0, "fg"): (31, 31, 31)}))
        steps = 0
        still = True
        while still and steps < 40:
            hex_map, still = anim.step()
            steps += 1
        self.assertFalse(still)
        self.assertGreater(steps, 2)
        self.assertEqual(hex_map[(0, "fg")], "#1f1f1f")

    def test_controller_per_slot_mixed_background_and_chroma_switch(self):
        ctrl = ac.AdaptiveColorController(anim_alpha=0.5)

        # 왼쪽 2칸은 어두운 배경, 오른쪽 1칸은 밝은 배경
        upd = ctrl.apply_samples([(15, 15, 15), (20, 20, 20), (245, 245, 245)], immediate=True)
        self.assertEqual(upd.chroma_key, ac.CHROMA_KEY_DARK)
        self.assertFalse(upd.chroma_changed)
        self.assertEqual(ctrl.slot_themes[0], "dark")
        self.assertEqual(ctrl.slot_themes[2], "light")
        # 어두운 칸 글자는 밝고, 밝은 칸 글자는 어두워야 함
        self.assertGreater(ac.relative_luminance(ac.parse_hex(ctrl.color_for(0, "ok"))), 0.3)
        self.assertLess(ac.relative_luminance(ac.parse_hex(ctrl.color_for(2, "ok"))), 0.25)

        # 전체가 밝은 흰 배경 위로 이동 → 크로마키가 #fefefe로 전환
        upd2 = ctrl.apply_samples([(250, 250, 250), (248, 248, 248), (255, 255, 255)], immediate=False)
        self.assertTrue(upd2.chroma_changed)
        self.assertEqual(upd2.chroma_key, ac.CHROMA_KEY_LIGHT)
        self.assertTrue(upd2.needs_animation)

        # 전부 None 샘플이면 기존 상태 유지
        upd3 = ctrl.apply_samples([None, None, None])
        self.assertFalse(upd3.chroma_changed)
        self.assertEqual(ctrl.chroma_key, ac.CHROMA_KEY_LIGHT)

        # reset 시 기본 다크 크로마키로 복귀
        ctrl.reset()
        self.assertEqual(ctrl.chroma_key, ac.CHROMA_KEY_DARK)


class ScreenSamplerTests(unittest.TestCase):
    def test_samplers_safe_on_non_windows(self):
        if sys.platform == "win32":
            self.skipTest("비-Windows 환경 전용 테스트")
        self.assertIsNone(ss.NullScreenSampler().sample_slots(1234, [(0.0, 1.0)]))
        self.assertIsNone(ss.GDIScreenSampler().sample_slots(1234, [(0.0, 1.0)]))
        self.assertIsInstance(ss.create_default_sampler(), ss.NullScreenSampler)

    def test_async_sampler_delivers_result_and_stops(self):
        class FakeSampler(ss.BaseScreenSampler):
            def __init__(self):
                self.closed = False

            def sample_slots(self, hwnd, slot_ratios, *, thickness=5, stride=2):
                return [(10, 20, 30) for _ in slot_ratios]

            def close(self):
                self.closed = True

        fake = FakeSampler()
        worker = ss.AsyncScreenSampler(fake)
        try:
            worker.request(999, [(0.0, 0.5), (0.5, 1.0)], immediate=True)
            res = None
            deadline = time.monotonic() + 1.0
            while res is None and time.monotonic() < deadline:
                res = worker.poll_result()
                if res is None:
                    time.sleep(0.01)
            self.assertEqual(res, ([(10, 20, 30), (10, 20, 30)], True))
        finally:
            worker.stop()
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
