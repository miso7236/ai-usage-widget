import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_oauth  # noqa: E402
from state import Store  # noqa: E402

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class CredentialDiscoveryTests(unittest.TestCase):
    def test_extracts_hermes_oauth_and_skips_api_keys_and_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes_dir = Path(tmp) / "hermes"
            hermes_dir.mkdir()
            (hermes_dir / "auth.json").write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "anthropic": [
                                {"auth_type": "api_key", "access_token": "sk-ant-api03-should-skip-12345"},
                                {"auth_type": "oauth", "access_token": "sk-ant-oat01-disabled-token", "disabled": True},
                                {
                                    "auth_type": "oauth",
                                    "label": "hermes-main",
                                    "access_token": "sk-ant-oat01-hermes-valid-token",
                                    "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                                },
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_dir), "CLAUDE_CODE_OAUTH_TOKEN": ""}, clear=False), mock.patch.object(
                Path, "home", return_value=Path(tmp) / "empty_home"
            ):
                creds = claude_oauth.discover_oauth_tokens(now=NOW)

        self.assertEqual(len(creds), 1)
        self.assertEqual(creds[0].token, "sk-ant-oat01-hermes-valid-token")
        self.assertEqual(creds[0].source, "hermes")
        self.assertEqual(creds[0].display_name, "Claude (Hermes)")

    def test_extracts_claude_code_credentials_and_prefers_unexpired(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            hermes_dir = home / ".hermes"
            claude_dir = home / ".claude"
            hermes_dir.mkdir()
            claude_dir.mkdir()
            # Hermes 토큰은 만료됨
            (hermes_dir / "auth.json").write_text(
                json.dumps(
                    {
                        "credential_pool": {
                            "anthropic": [
                                {
                                    "auth_type": "oauth",
                                    "access_token": "sk-ant-oat01-expired-hermes",
                                    "expires_at": (NOW - timedelta(minutes=10)).isoformat(),
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            # Claude Code 토큰은 유효함 (밀리초 epoch)
            valid_ms = int((NOW + timedelta(minutes=45)).timestamp() * 1000)
            (claude_dir / ".credentials.json").write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "sk-ant-oat01-valid-claudecode",
                            "expiresAt": valid_ms,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"HERMES_HOME": "", "LOCALAPPDATA": "", "CLAUDE_CONFIG_DIR": "", "CLAUDE_CODE_OAUTH_TOKEN": ""},
                clear=False,
            ), mock.patch.object(Path, "home", return_value=home):
                creds = claude_oauth.discover_oauth_tokens(now=NOW)

        self.assertEqual([c.token for c in creds], ["sk-ant-oat01-valid-claudecode", "sk-ant-oat01-expired-hermes"])
        self.assertEqual(creds[0].display_name, "Claude (CLI)")


class ParseUsageTests(unittest.TestCase):
    def test_parses_standard_oauth_usage_response(self):
        payload = {
            "seven_day": {"utilization": 31.0, "resets_at": "2026-11-25T03:00:00+00:00"},
            "five_hour": {"utilization": 54.4, "resets_at": "2026-11-20T14:00:00+00:00"},
            "seven_day_sonnet": {"utilization": 12.0, "resets_at": "2026-11-25T03:00:00+00:00"},
            "seven_day_opus": None,
            "extra_usage": {"is_enabled": False, "utilization": 80.0},
        }
        windows = claude_oauth.parse_oauth_usage(payload)
        self.assertEqual([(w.label, w.used_percent) for w in windows], [("5시간", 54.4), ("주간", 31.0), ("주간 Sonnet", 12.0)])


class FetcherTests(unittest.TestCase):
    def test_fetcher_success_429_backoff_and_store_switching(self):
        cred = claude_oauth.OAuthCredential(token="sk-ant-oat01-test-token", source="hermes", label="Hermes")
        sample = {
            "five_hour": {"utilization": 22.0, "resets_at": (NOW + timedelta(hours=3)).isoformat()},
            "seven_day": {"utilization": 45.0, "resets_at": (NOW + timedelta(days=4)).isoformat()},
        }
        fetcher = claude_oauth.ClaudeOAuthFetcher()

        with mock.patch.object(claude_oauth, "discover_oauth_tokens", return_value=[cred]), mock.patch.object(
            claude_oauth, "_request_usage", return_value=sample
        ):
            s1 = fetcher.fetch(now=NOW)

        self.assertTrue(s1.ok)
        self.assertEqual(s1.name, "Claude (Hermes)")
        self.assertEqual([(w.label, w.used_percent) for w in s1.windows], [("5시간", 22.0), ("주간", 45.0)])

        # Store에서 크롬 웹 Claude와 로컬 OAuth Claude 전환 확인
        store = Store()
        store.update(
            {
                "providers": [
                    {
                        "provider": "claude",
                        "name": "Claude",
                        "ok": True,
                        "fetchedAt": NOW.isoformat(),
                        "windows": [{"label": "5시간", "usedPercent": 80}],
                    }
                ]
            }
        )
        store.set_local_provider(s1)
        self.assertEqual(store.snapshot()[0].name, "Claude (Hermes)")
        self.assertEqual(store.snapshot()[0].windows[0].used_percent, 22.0)

        # 웹 계정으로 돌아오면 기존 크롬 확장 데이터가 즉시 복원된다.
        store.remove_local_provider("claude")
        self.assertEqual(store.snapshot()[0].name, "Claude")
        self.assertEqual(store.snapshot()[0].windows[0].used_percent, 80.0)

        # 429 응답 시 백오프가 걸리고 이전 윈도우를 유지한다.
        err_429 = urllib.error.HTTPError(claude_oauth.USAGE_URL, 429, "Too Many Requests", {}, io.BytesIO(b""))
        with mock.patch.object(claude_oauth, "discover_oauth_tokens", return_value=[cred]), mock.patch.object(
            claude_oauth, "_request_usage", side_effect=err_429
        ) as req_mock:
            s2 = fetcher.fetch(now=NOW + timedelta(minutes=5))
            self.assertFalse(s2.ok)
            self.assertEqual(len(s2.windows), 2)
            self.assertIn("한도 초과", s2.error or "")
            # 백오프 시간 내에는 API를 다시 호출하지 않는다.
            s3 = fetcher.fetch(now=NOW + timedelta(minutes=6))
            self.assertFalse(s3.ok)
            self.assertEqual(req_mock.call_count, 1)

    def test_fetcher_reports_missing_credentials(self):
        fetcher = claude_oauth.ClaudeOAuthFetcher()
        with mock.patch.object(claude_oauth, "discover_oauth_tokens", return_value=[]):
            s = fetcher.fetch(now=NOW)
        self.assertFalse(s.ok)
        self.assertIn("토큰 없음", s.error or "")


if __name__ == "__main__":
    unittest.main()
