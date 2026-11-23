import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import diagnostics  # noqa: E402
from state import ProviderState, UsageWindow  # noqa: E402

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def provider(name="claude", ok=True, error=None, windows=None, minutes_ago=2):
    return ProviderState(
        provider=name,
        name=name.title(),
        ok=ok,
        windows=windows if windows is not None else [UsageWindow(label="5시간", used_percent=42.0)],
        error=error,
        fetched_at=NOW - timedelta(minutes=minutes_ago),
    )


class RedactTests(unittest.TestCase):
    def test_masks_bearer_token(self):
        out = diagnostics.redact("Authorization: Bearer abcDEF123456.789xyz")
        self.assertNotIn("abcDEF123456", out)
        self.assertIn("Bearer [가림]", out)

    def test_masks_api_key_and_jwt(self):
        jwt = "eyJhbGciOi.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4"
        out = diagnostics.redact(f"key sk-ant-oat01-abcdefgh1234 jwt {jwt}")
        self.assertNotIn("abcdefgh1234", out)
        self.assertNotIn(jwt, out)

    def test_masks_key_value_tokens(self):
        out = diagnostics.redact('--csrf_token=1a2b3c4d5e6f "accessToken": "zzzzzzzzzzzz"')
        self.assertNotIn("1a2b3c4d5e6f", out)
        self.assertNotIn("zzzzzzzzzzzz", out)

    def test_masks_uuid_and_email(self):
        out = diagnostics.redact("org 123e4567-e89b-12d3-a456-426614174000 user someone@example.com")
        self.assertIn("[uuid]", out)
        self.assertIn("[email]", out)
        self.assertNotIn("someone@example.com", out)

    def test_masks_home_directory_both_slash_styles(self):
        home = r"C:\Users\tester"
        out = diagnostics.redact(r"C:\Users\tester\.claude\x.json and C:/Users/tester/y", home=home)
        self.assertNotIn("tester", out)
        self.assertIn("~", out)

    def test_keeps_ordinary_text(self):
        text = "claude: 조회 실패 - HTTP 403 (형식이 바뀌었을 수 있어요)"
        self.assertEqual(diagnostics.redact(text, home=""), text)


class ProviderChangeLoggerTests(unittest.TestCase):
    def test_logs_only_on_change(self):
        tracker = diagnostics.ProviderChangeLogger()
        self.assertEqual(len(tracker.observe([provider()])), 1)
        self.assertEqual(tracker.observe([provider()]), [])
        failed = tracker.observe([provider(ok=False, error="HTTP 403", windows=[])])
        self.assertEqual(len(failed), 1)
        self.assertIn("조회 실패 - HTTP 403", failed[0])
        self.assertEqual(tracker.observe([provider(ok=False, error="HTTP 403", windows=[])]), [])
        recovered = tracker.observe([provider()])
        self.assertIn("정상으로 돌아옴", recovered[0])

    def test_forgets_removed_provider(self):
        tracker = diagnostics.ProviderChangeLogger()
        tracker.observe([provider("gemini")])
        tracker.observe([])
        self.assertEqual(len(tracker.observe([provider("gemini")])), 1)


class ResponseShapeTests(unittest.TestCase):
    def test_failure_line_and_report_include_shape(self):
        p = provider("chatgpt", ok=False, error="형식이 바뀌었을 수 있어요", windows=[])
        p.response_shape = '{"usage":{"percent_used":"37"}}'
        [line] = diagnostics.ProviderChangeLogger().observe([p])
        self.assertIn('응답 구조: {"usage":{"percent_used":"37"}}', line)
        report = diagnostics.build_report({}, [p], log_lines=[], now=NOW)
        self.assertIn('응답 구조: `{"usage":{"percent_used":"37"}}`', report)

    def test_from_dict_reads_and_limits_shape(self):
        state = ProviderState.from_dict(
            {"provider": "chatgpt", "ok": False, "error": "x", "responseShape": "{" + "a" * 5000 + "}"}
        )
        self.assertEqual(len(state.response_shape), 1600)
        self.assertIsNone(ProviderState.from_dict({"provider": "chatgpt", "responseShape": 3}).response_shape)


class BuildReportTests(unittest.TestCase):
    def test_report_contains_state_and_hides_paths(self):
        config = {
            "web_source": "browser",
            "port": 47823,
            "claude_oauth_path": r"C:\Users\tester\.claude\.credentials.json",
            "x": 100,
        }
        report = diagnostics.build_report(
            config,
            [provider(), provider("chatgpt", ok=False, error="HTTP 401", windows=[])],
            extra={"실행 방식": "일반"},
            log_lines=["2026-11-23 12:00:00 WARNING chatgpt: 조회 실패 - HTTP 401"],
            now=NOW,
        )
        self.assertIn("web_source: browser", report)
        self.assertIn("claude_oauth_path: 지정됨", report)
        self.assertNotIn("credentials.json", report)
        self.assertIn("- claude: 정상 · 2분 전 · 5시간 42%", report)
        self.assertIn("- chatgpt: 오류 · 2분 전 · HTTP 401", report)
        self.assertIn("실행 방식: 일반", report)
        self.assertIn("WARNING chatgpt", report)
        self.assertNotIn("- x:", report)

    def test_report_without_data(self):
        report = diagnostics.build_report({}, [], log_lines=[], now=NOW)
        self.assertIn("받은 데이터 없음", report)
        self.assertIn("(로그 없음)", report)


class LogFileTests(unittest.TestCase):
    def test_file_log_is_redacted_and_tail_is_readable(self):
        logger = diagnostics.log
        saved = list(logger.handlers)
        logger.handlers.clear()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "widget.log"
            try:
                self.assertEqual(diagnostics.setup_logging(path), path)
                logger.warning("요청 실패 Bearer abcdefghijklmnop")
                for h in logger.handlers:
                    h.flush()
                tail = diagnostics.read_log_tail(path)
                self.assertEqual(len(tail), 1)
                self.assertIn("Bearer [가림]", tail[0])
                self.assertNotIn("abcdefghijklmnop", tail[0])
            finally:
                for h in list(logger.handlers):
                    h.close()
                    logger.removeHandler(h)
                logger.handlers.extend(saved)

    def test_missing_log_gives_empty_tail(self):
        self.assertEqual(diagnostics.read_log_tail(Path("no-such-dir") / "none.log"), [])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
