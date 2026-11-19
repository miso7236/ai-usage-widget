import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import start_server  # noqa: E402
from state import (  # noqa: E402
    ProviderState,
    Store,
    UsageWindow,
    clamp_position,
    compact_summary,
    default_position,
    demo_payload,
    format_eta,
    format_reset,
    level,
    load_config,
    save_config,
)

NOW = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)


class FormatTests(unittest.TestCase):
    def test_level(self):
        self.assertEqual(level(10), "ok")
        self.assertEqual(level(70), "warn")
        self.assertEqual(level(90), "danger")

    def test_format_reset_hours(self):
        w = UsageWindow("5시간", 40, resets_at=NOW + timedelta(hours=2, minutes=13, seconds=30))
        self.assertEqual(format_reset(w, NOW), "2시간 13분 후")

    def test_format_reset_days(self):
        w = UsageWindow("주간", 40, resets_at=NOW + timedelta(days=3, hours=4))
        self.assertEqual(format_reset(w, NOW), "3일 4시간 후")

    def test_format_reset_past_and_text(self):
        self.assertEqual(format_reset(UsageWindow("a", 1, resets_at=NOW - timedelta(minutes=1)), NOW), "곧 리셋")
        self.assertEqual(format_reset(UsageWindow("a", 1, reset_text="오후 3시 재설정"), NOW), "오후 3시 재설정")
        self.assertEqual(format_reset(UsageWindow("a", 1), NOW), "")

    def test_format_eta(self):
        w = UsageWindow("5시간", 60, resets_at=NOW + timedelta(hours=3), eta_at=NOW + timedelta(hours=1, minutes=20))
        self.assertEqual(format_eta(w, NOW), "약 1시간 20분 후 소진")
        w_min = UsageWindow("5시간", 80, eta_at=NOW + timedelta(minutes=25))
        self.assertEqual(format_eta(w_min, NOW), "약 25분 후 소진")
        # 리셋 이후에 소진 예정이면 표시하지 않음
        w_after_reset = UsageWindow("5시간", 40, resets_at=NOW + timedelta(hours=1), eta_at=NOW + timedelta(hours=2))
        self.assertEqual(format_eta(w_after_reset, NOW), "")
        self.assertEqual(format_eta(UsageWindow("5시간", 40), NOW), "")

    def test_compact_summary(self):
        providers = [
            ProviderState("claude", "Claude", True, [UsageWindow("5시간", 42), UsageWindow("주간", 71)], fetched_at=NOW),
            ProviderState("chatgpt", "ChatGPT", True, [UsageWindow("주간", 93)], fetched_at=NOW),
            ProviderState("gemini", "Gemini", False, [UsageWindow("5시간", 30)], error="오류", fetched_at=NOW),
            ProviderState("other", "Perplexity", False, [], error="실패", fetched_at=NOW),
        ]
        self.assertEqual(
            compact_summary(providers, NOW),
            [
                ("C", "71%", "warn"),
                ("GPT", "93%", "danger"),
                ("Gem", "30%", "muted"),
                ("Perp", "!", "error"),
            ],
        )


class ProviderStateTests(unittest.TestCase):
    def test_from_dict_clamps_and_skips_bad_windows(self):
        s = ProviderState.from_dict({
            "provider": "claude", "name": "Claude", "ok": True,
            "fetchedAt": "2026-11-18T14:00:00Z",
            "windows": [
                {"label": "5시간", "usedPercent": 150, "resetsAt": "2026-11-18T16:00:00Z"},
                {"label": "깨짐", "usedPercent": "abc"},
                "not a dict",
            ],
        })
        self.assertEqual(len(s.windows), 1)
        self.assertEqual(s.windows[0].used_percent, 100.0)
        self.assertEqual(s.windows[0].resets_at, NOW + timedelta(hours=2))

    def test_missing_provider_rejected(self):
        with self.assertRaises(ValueError):
            ProviderState.from_dict({"windows": []})

    def test_stale(self):
        s = ProviderState("claude", "Claude", True, fetched_at=NOW - timedelta(minutes=20))
        self.assertTrue(s.is_stale(NOW))
        s.fetched_at = NOW - timedelta(minutes=5)
        self.assertFalse(s.is_stale(NOW))


class StoreTests(unittest.TestCase):
    def test_update_replaces_and_orders(self):
        store = Store()
        store.update(demo_payload(NOW))
        self.assertEqual([p.provider for p in store.snapshot()], ["claude", "chatgpt", "gemini"])
        store.update({"providers": [demo_payload(NOW)["providers"][1]]})
        self.assertEqual([p.provider for p in store.snapshot()], ["chatgpt"])
        self.assertEqual(store.version, 2)

    def test_update_rejects_bad_payload(self):
        with self.assertRaises(ValueError):
            Store().update({"nope": 1})


class ConfigTests(unittest.TestCase):
    def test_roundtrip_and_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "config.json"
            self.assertEqual(load_config(path)["alpha"], 0.92)
            save_config({"x": 10, "y": 20, "alpha": 0.8, "topmost": False, "port": 47823}, path)
            cfg = load_config(path)
            self.assertEqual((cfg["x"], cfg["alpha"], cfg["topmost"]), (10, 0.8, False))


class PositionTests(unittest.TestCase):
    PRIMARY = (0, 0, 1920, 1040)
    LEFT_MON = (-1920, 0, 0, 1040)
    RIGHT_MON = (1920, 0, 3840, 1040)

    def test_default_position_when_unset(self):
        pos = clamp_position(None, None, 260, 180, [self.PRIMARY], self.PRIMARY)
        self.assertEqual(pos, default_position(260, 180, self.PRIMARY))
        self.assertEqual(pos, (1640, 800))

    def test_preserves_negative_x_on_left_monitor(self):
        monitors = [self.LEFT_MON, self.PRIMARY]
        pos = clamp_position(-400, 120, 260, 180, monitors, self.PRIMARY)
        self.assertEqual(pos, (-400, 120))

    def test_preserves_right_monitor_and_clamps_overflow(self):
        monitors = [self.PRIMARY, self.RIGHT_MON]
        pos = clamp_position(3700, 950, 260, 180, monitors, self.PRIMARY)
        # 오른쪽 모니터(3840x1040) 안쪽으로만 당겨지고 주 모니터로 끌려오지 않는다
        self.assertEqual(pos, (3840 - 260 - 10, 1040 - 180 - 10))

    def test_returns_to_primary_when_disconnected_monitor(self):
        # 보조 모니터(-1920..0)가 분리되어 주 모니터만 남은 경우
        pos = clamp_position(-1000, 200, 260, 180, [self.PRIMARY], self.PRIMARY)
        self.assertEqual(pos, default_position(260, 180, self.PRIMARY))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store()
        cls.server = start_server(cls.store, 0, info=lambda: {"extensionVersion": "9.9.9"})  # 0 = 빈 포트
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body, origin=None):
        data = json.dumps(body).encode()
        req = urllib.request.Request(f"{self.base}/update", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        if origin:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["app"], "ai-usage-widget")

    def test_update_from_extension(self):
        status, body = self.post(demo_payload(NOW), origin="chrome-extension://abcdef")
        self.assertEqual((status, body["providers"]), (200, 3))
        self.assertEqual(body["extensionVersion"], "9.9.9")  # 확장 자동 업데이트용
        self.assertIn("widgetVersion", body)
        self.assertEqual(len(self.store.snapshot()), 3)

    def test_web_origin_rejected(self):
        status, _ = self.post(demo_payload(NOW), origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_bad_payload(self):
        status, _ = self.post({"providers": "x"})
        self.assertEqual(status, 400)

    def test_refresh_and_wait_command(self):
        # 현재 seq 확인 후 짧은 timeout으로 대기하면 refresh=False
        with urllib.request.urlopen(f"{self.base}/health", timeout=5) as r:
            seq0 = json.loads(r.read())["refreshSeq"]
        with urllib.request.urlopen(f"{self.base}/wait-command?after={seq0}&timeout=0.05", timeout=5) as r:
            body = json.loads(r.read())
            self.assertEqual((body["refresh"], body["refreshSeq"]), (False, seq0))

        # POST /refresh 호출 후 즉시 refresh=True 반환
        req = urllib.request.Request(f"{self.base}/refresh", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            refreshed = json.loads(r.read())
            self.assertEqual(refreshed["refreshSeq"], seq0 + 1)
        with urllib.request.urlopen(f"{self.base}/wait-command?after={seq0}&timeout=1", timeout=5) as r:
            body = json.loads(r.read())
            self.assertEqual((body["refresh"], body["refreshSeq"]), (True, seq0 + 1))


if __name__ == "__main__":
    unittest.main()
