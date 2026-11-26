import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tracking  # noqa: E402
from tracking import Tracker  # noqa: E402

BASE = 180.0


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.t = Tracker()

    def see(self, now, five, week=40.0, provider="claude", **kw):
        return self.t.observe(provider, {"5시간": five, "주간": week}, now, base_sec=BASE, **kw)

    def test_first_result_only_sets_the_baseline(self):
        self.assertFalse(self.see(0, 10))
        self.assertFalse(self.t.is_tracking("claude", 0))
        self.assertEqual(self.t.interval("claude", BASE, 0), BASE)

    def test_increase_starts_tracking_at_the_fast_interval(self):
        self.see(0, 10)
        self.assertTrue(self.see(180, 11))
        self.assertTrue(self.t.is_tracking("claude", 180))
        self.assertEqual(self.t.interval("claude", BASE, 180), tracking.FAST_SEC)
        self.assertFalse(self.see(195, 12))  # 이미 추적 중이면 새로 켜진 것이 아니다
        self.assertEqual(self.t.tracking(195), frozenset({"claude"}))

    def test_unchanged_value_does_not_start_tracking(self):
        self.see(0, 10)
        self.assertFalse(self.see(180, 10))
        self.assertFalse(self.t.is_tracking("claude", 180))

    def test_slows_down_then_stops_when_quiet(self):
        self.see(0, 10)
        self.see(180, 11)
        self.see(180 + 119, 11)
        self.assertEqual(self.t.interval("claude", BASE, 180 + 119), tracking.FAST_SEC)
        self.assertEqual(self.t.interval("claude", BASE, 180 + 121), tracking.COOL_SEC)
        self.assertTrue(self.t.is_tracking("claude", 180 + 299))
        self.assertFalse(self.t.is_tracking("claude", 180 + 300))
        self.assertEqual(self.t.interval("claude", BASE, 180 + 300), BASE)

    def test_new_change_while_cooling_returns_to_fast(self):
        self.see(0, 10)
        self.see(180, 11)
        self.see(180 + 150, 12)
        self.assertEqual(self.t.interval("claude", BASE, 180 + 150), tracking.FAST_SEC)
        self.assertTrue(self.t.is_tracking("claude", 180 + 150 + 299))

    def test_long_runs_use_the_slower_interval(self):
        self.see(0, 10)
        now, value = 180.0, 11.0
        self.see(now, value)
        while now < 180 + tracking.LONG_RUN_SEC:
            now += 60
            value += 0.5
            self.see(now, value)
        self.assertTrue(self.t.is_tracking("claude", now))
        self.assertEqual(self.t.interval("claude", BASE, now), tracking.COOL_SEC)

    def test_reset_drop_is_not_a_change(self):
        self.see(0, 80)
        self.assertFalse(self.see(180, 0))
        self.assertFalse(self.t.is_tracking("claude", 180))
        self.assertTrue(self.see(360, 1))  # 초기화 뒤 다시 늘면 추적한다

    def test_small_wobble_is_ignored(self):
        self.see(0, 71)
        self.assertFalse(self.see(180, 70))
        self.assertFalse(self.see(360, 71))  # 전에 본 값까지 되돌아온 것은 증가가 아니다
        self.assertFalse(self.t.is_tracking("claude", 360))
        self.assertTrue(self.see(540, 72))

    def test_long_silence_resets_the_baseline(self):
        self.see(0, 10)
        self.see(180, 11)
        gap = 180 + BASE * 2 + 61
        self.assertFalse(self.see(gap, 40))  # 절전에서 깨어난 뒤 첫 조회
        self.assertFalse(self.t.is_tracking("claude", gap))
        self.assertTrue(self.see(gap + 180, 41))

    def test_two_failures_stop_tracking(self):
        self.see(0, 10)
        self.see(180, 11)
        self.t.observe("claude", None, 195, base_sec=BASE)
        self.assertTrue(self.t.is_tracking("claude", 195))
        self.t.observe("claude", None, 210, base_sec=BASE)
        self.assertFalse(self.t.is_tracking("claude", 210))
        self.assertEqual(self.t.interval("claude", BASE, 210), BASE)

    def test_success_clears_the_failure_count(self):
        self.see(0, 10)
        self.see(180, 11)
        self.t.observe("claude", None, 195, base_sec=BASE)
        self.see(210, 11)
        self.t.observe("claude", None, 225, base_sec=BASE)
        self.assertTrue(self.t.is_tracking("claude", 225))

    def test_does_not_start_when_not_allowed(self):
        self.see(0, 10)
        self.assertFalse(self.see(180, 11, allow_start=False))
        self.assertFalse(self.t.is_tracking("claude", 180))
        self.assertFalse(self.see(360, 11))  # 잠금 중에 본 값은 이미 기준에 반영됐다

    def test_keeps_tracking_when_start_is_no_longer_allowed(self):
        self.see(0, 10)
        self.see(180, 11)
        self.see(195, 12, allow_start=False)
        self.assertTrue(self.t.is_tracking("claude", 195 + 200))

    def test_providers_are_independent(self):
        self.see(0, 10)
        self.see(0, 10, provider="chatgpt")
        self.see(180, 11)
        self.see(180, 10, provider="chatgpt")
        self.assertEqual(self.t.tracking(180), frozenset({"claude"}))

    def test_interval_never_exceeds_the_base(self):
        self.see(0, 10)
        self.see(10, 11)
        self.assertEqual(self.t.interval("claude", 10.0, 10), 10.0)
        self.assertEqual(self.t.interval("claude", 600.0, 10, fast_sec=30.0), 30.0)

    def test_baseline_is_the_value_before_tracking_started(self):
        self.see(0, 10, week=40)
        self.assertEqual(self.t.baseline("claude", 0), {})
        self.see(180, 12, week=40)
        self.see(195, 15, week=41)
        self.assertEqual(self.t.baseline("claude", 195), {"5시간": 10, "주간": 40})
        self.assertEqual(self.t.baseline("claude", 195 + 300), {})  # 추적이 끝나면 기준도 없다

    def test_baseline_follows_a_reset_during_tracking(self):
        self.see(0, 90)
        self.see(180, 92)
        self.see(195, 0)  # 5시간 값 초기화
        self.see(210, 3)
        self.assertEqual(self.t.baseline("claude", 210)["5시간"], 0)

    def test_new_tracking_run_gets_a_new_baseline(self):
        self.see(0, 10)
        self.see(180, 12)
        self.see(180 + 400, 12)  # 추적 종료 뒤
        self.see(180 + 580, 13)
        self.assertEqual(self.t.baseline("claude", 180 + 580)["5시간"], 12)

    def test_stop_all(self):
        self.see(0, 10)
        self.see(180, 11)
        self.t.stop_all()
        self.assertFalse(self.t.is_tracking("claude", 181))


if __name__ == "__main__":
    unittest.main()
