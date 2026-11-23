"""tests/fixtures/normalize/*.json 공용 샘플로 내장 브라우저의 응답 해석을 검증한다.

같은 샘플을 tests/js/fixtures.test.js가 크롬 확장용 normalize.js에도 돌린다.
두 구현 중 한쪽만 고치면 둘 중 하나가 실패한다.
"""

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_normalize  # noqa: E402
from state import parse_iso  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "normalize"


def _epoch(value):
    if value is None:
        return None
    dt = parse_iso(value)
    if dt is None:
        raise AssertionError(f"ISO 시각이 아니에요: {value!r}")
    return round(dt.timestamp() * 1000)


def _comparable(windows):
    return [{**w, "resetsAt": _epoch(w.get("resetsAt"))} for w in windows]


def _run(fx):
    now = parse_iso(fx["now"])
    provider = fx["provider"]
    if provider == "claude":
        return web_normalize.normalize_claude(fx["input"], now)
    if provider == "chatgpt":
        return web_normalize.normalize_chatgpt(fx["input"], now)
    if provider == "gemini":
        tz = timezone(timedelta(minutes=fx.get("utcOffsetMinutes") or 0))
        return web_normalize.normalize_gemini(fx["input"], now, local_tz=tz)
    raise AssertionError(f"알 수 없는 provider: {provider}")


class SharedFixtureTests(unittest.TestCase):
    def test_fixtures_exist(self):
        self.assertGreaterEqual(len(list(FIXTURE_DIR.glob("*.json"))), 10)

    def test_all_fixtures(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            fx = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(fixture=path.name, description=fx["description"]):
                if fx.get("error"):
                    with self.assertRaises(ValueError) as ctx:
                        _run(fx)
                    self.assertIn(fx["error"], str(ctx.exception))
                    continue
                result = _run(fx)
                self.assertTrue(result["ok"])
                self.assertEqual(result["provider"], fx["provider"])
                self.assertEqual(_comparable(result["windows"]), _comparable(fx["expected"]))


if __name__ == "__main__":
    unittest.main()
