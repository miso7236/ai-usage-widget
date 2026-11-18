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
    demo_payload,
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


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store()
        cls.server = start_server(cls.store, 0)  # 0 = 빈 포트 자동 선택
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
        self.assertEqual(len(self.store.snapshot()), 3)

    def test_web_origin_rejected(self):
        status, _ = self.post(demo_payload(NOW), origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_bad_payload(self):
        status, _ = self.post({"providers": "x"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
