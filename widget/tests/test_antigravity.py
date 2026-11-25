import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import antigravity  # noqa: E402
from state import UsageWindow  # noqa: E402

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class FilterTests(unittest.TestCase):
    def test_default_gemini_filter_excludes_argon_opus_claude_gpt(self):
        self.assertTrue(antigravity.is_default_gemini_name("Gemini Models"))
        self.assertTrue(antigravity.is_default_gemini_name("Gemini 3.1 Pro (High)"))
        self.assertTrue(antigravity.is_default_gemini_name("gemini-3.8-flash-high"))

        self.assertFalse(antigravity.is_default_gemini_name("Gemini 4 Argon"))
        self.assertFalse(antigravity.is_default_gemini_name("gemini-argon-preview"))
        self.assertFalse(antigravity.is_default_gemini_name("Claude Opus 4.6 (Thinking)"))
        self.assertFalse(antigravity.is_default_gemini_name("Claude Sonnet 4.6"))
        self.assertFalse(antigravity.is_default_gemini_name("Claude and GPT models"))
        self.assertFalse(antigravity.is_default_gemini_name("GPT-OSS"))
        self.assertFalse(antigravity.is_default_gemini_name("Gemini Imagen 3"))
        self.assertFalse(antigravity.is_default_gemini_name(""))


class ParseQuotaSummaryTests(unittest.TestCase):
    def test_extracts_only_default_gemini_5h_and_weekly(self):
        payload = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini_weekly",
                                "displayName": "Weekly limit",
                                "remainingFraction": 0.62,
                                "resetTime": (NOW + timedelta(days=2, hours=5)).isoformat(),
                            },
                            {
                                "bucketId": "gemini_5h",
                                "displayName": "5-hour limit",
                                "remainingFraction": 0.75,
                                "resetTime": (NOW + timedelta(hours=3, minutes=10)).isoformat(),
                            },
                            {
                                "bucketId": "gemini_disabled",
                                "displayName": "Disabled",
                                "remainingFraction": 0.0,
                                "disabled": True,
                            },
                        ],
                    },
                    {
                        "displayName": "Gemini 4 Argon",
                        "buckets": [
                            {
                                "bucketId": "argon_weekly",
                                "displayName": "Weekly limit",
                                "remainingFraction": 0.05,
                                "resetTime": (NOW + timedelta(days=5)).isoformat(),
                            }
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "bucketId": "claude_5h",
                                "displayName": "5-hour limit",
                                "remainingFraction": 0.1,
                                "resetTime": (NOW + timedelta(hours=1)).isoformat(),
                            }
                        ],
                    },
                ]
            }
        }
        windows = antigravity.parse_quota_summary(payload, now=NOW)
        self.assertEqual([w.label for w in windows], ["5시간", "주간"])
        self.assertEqual(windows[0].used_percent, 25.0)
        self.assertEqual(windows[1].used_percent, 38.0)
        self.assertEqual(windows[0].resets_at, NOW + timedelta(hours=3, minutes=10))

    def test_supports_nested_remaining_object(self):
        payload = {
            "groups": [
                {
                    "name": "Gemini",
                    "buckets": [
                        {
                            "id": "five_hour",
                            "remaining": {"remainingFraction": 0.4},
                            "resetTime": (NOW + timedelta(hours=2)).isoformat(),
                        }
                    ],
                }
            ]
        }
        windows = antigravity.parse_quota_summary(payload, now=NOW)
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0].label, windows[0].used_percent), ("5시간", 60.0))

    def test_oneof_remaining_window_cadence_and_proto3_zero(self):
        payload = {
            "response": {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                # 주간 리셋이 몇 시간 남지 않아도 window 값이 우선한다.
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "displayName": "Weekly Limit Remaining",
                                "remaining": {"case": "remainingFraction", "value": 0.78},
                                "resetTime": (NOW + timedelta(hours=3)).isoformat(),
                            },
                            {
                                # 다 쓰면 proto3 JSON이 remainingFraction(0)을 생략한다.
                                "bucketId": "gemini-5h",
                                "window": "5h",
                                "displayName": "Five Hour Limit Remaining",
                                "resetTime": (NOW + timedelta(minutes=20)).isoformat(),
                                "description": "You have hit your 5-hour limit, it will refresh in 20 minutes.",
                            },
                        ],
                    }
                ]
            }
        }
        windows = antigravity.parse_quota_summary(payload, now=NOW)
        self.assertEqual([(w.label, w.used_percent) for w in windows], [("5시간", 100.0), ("주간", 22.0)])

    def test_top_level_buckets_without_groups(self):
        payload = {
            "buckets": [
                {"bucketId": "gemini-weekly", "remainingFraction": 0.5004, "resetTime": (NOW + timedelta(days=4)).isoformat()},
                {"bucketId": "gemini-5h", "remainingFraction": 0.9, "resetTime": (NOW + timedelta(hours=2)).isoformat()},
                {"bucketId": "gemini-argon-weekly", "remainingFraction": 0.01, "resetTime": (NOW + timedelta(days=5)).isoformat()},
                {"bucketId": "3p-5h", "remainingFraction": 0.1, "resetTime": (NOW + timedelta(hours=1)).isoformat()},
            ]
        }
        windows = antigravity.parse_quota_summary(payload, now=NOW)
        self.assertEqual([(w.label, w.used_percent) for w in windows], [("5시간", 10.0), ("주간", 50.0)])

    def test_missing_fraction_without_hint_is_skipped(self):
        payload = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [{"bucketId": "gemini-5h", "resetTime": (NOW + timedelta(hours=2)).isoformat()}],
                }
            ]
        }
        self.assertEqual(antigravity.parse_quota_summary(payload, now=NOW), [])


class ParseModelConfigsTests(unittest.TestCase):
    def test_get_user_status_picks_default_gemini_and_ignores_argon_opus(self):
        payload = {
            "userStatus": {
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "Claude Opus 4.6",
                            "modelOrAlias": {"model": "claude-opus-4-6"},
                            "quotaInfo": {"remainingFraction": 0.05, "resetTime": (NOW + timedelta(hours=1)).isoformat()},
                        },
                        {
                            "label": "Gemini 4 Argon",
                            "modelOrAlias": {"model": "gemini-4-argon"},
                            "quotaInfo": {"remainingFraction": 0.10, "resetTime": (NOW + timedelta(days=4)).isoformat()},
                        },
                        {
                            "label": "Gemini 3.8 Flash (High)",
                            "modelOrAlias": {"model": "gemini-3.8-flash-high"},
                            "quotaInfo": {"remainingFraction": 0.80, "resetTime": (NOW + timedelta(hours=4)).isoformat()},
                        },
                        {
                            "label": "Gemini 3.1 Pro (High)",
                            "modelOrAlias": {"model": "gemini-3.1-pro-high"},
                            "quotaInfo": {"remainingFraction": 0.65, "resetTime": (NOW + timedelta(hours=4)).isoformat()},
                        },
                    ]
                }
            }
        }
        windows = antigravity.parse_model_configs(payload, now=NOW)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].label, "5시간")
        self.assertEqual(windows[0].used_percent, 35.0)

    def test_command_model_configs_weekly_label_inference(self):
        payload = {
            "clientModelConfigs": [
                {
                    "label": "Gemini 3.1 Pro",
                    "modelOrAlias": {"model": "gemini-pro"},
                    "quotaInfo": {"remainingFraction": 0.50, "resetTime": (NOW + timedelta(days=3)).isoformat()},
                }
            ]
        }
        windows = antigravity.parse_model_configs(payload, now=NOW)
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0].label, windows[0].used_percent), ("주간", 50.0))

    def test_model_configs_split_by_reset_cadence(self):
        payload = {
            "clientModelConfigs": [
                {
                    "label": "Gemini 3.1 Pro",
                    "modelOrAlias": {"model": "gemini-pro"},
                    "quotaInfo": {"remainingFraction": 0.9, "resetTime": (NOW + timedelta(hours=4)).isoformat()},
                },
                {
                    "label": "Gemini 3.8 Flash",
                    "modelOrAlias": {"model": "gemini-flash"},
                    "quotaInfo": {"remainingFraction": 0.6, "resetTime": (NOW + timedelta(days=2)).isoformat()},
                },
            ]
        }
        windows = antigravity.parse_model_configs(payload, now=NOW)
        self.assertEqual([(w.label, w.used_percent) for w in windows], [("5시간", 10.0), ("주간", 40.0)])


class DiscoveryParserTests(unittest.TestCase):
    def test_parse_process_args(self):
        cmd = (
            '"C:\\Users\\me\\AppData\\Local\\Antigravity\\language_server_windows_x64.exe" '
            '--enable_lsp --random_port --app_data_dir antigravity '
            '--enable_telemetry --csrf_token abc-123 '
            '--extension_server_port=51234 --extension_server_csrf_token "ext-tok"'
        )
        args = antigravity.parse_process_args(cmd)
        self.assertEqual(args["app_data_dir"], "antigravity")
        self.assertEqual(args["csrf_token"], "abc-123")
        self.assertEqual(args["extension_server_port"], "51234")
        self.assertEqual(args["extension_server_csrf_token"], "ext-tok")

    def test_parse_process_args_single_dash_and_adjacent_boolean_flags(self):
        cmd = "language_server.exe --random_port -csrf_token tok-999 -extension_server_port=40001"
        args = antigravity.parse_process_args(cmd)
        self.assertEqual(args["csrf_token"], "tok-999")
        self.assertEqual(args["extension_server_port"], "40001")

    def test_is_antigravity_process(self):
        self.assertEqual(
            antigravity.is_antigravity_process(
                "language_server_windows_x64.exe",
                "C:\\Antigravity\\language_server_windows_x64.exe --app_data_dir antigravity --csrf_token x",
            ),
            (True, False),
        )
        self.assertEqual(
            antigravity.is_antigravity_process(
                "language-server.exe",
                "C:\\Users\\홍길동\\AppData\\Local\\Programs\\Antigravity\\language-server.exe --csrf_token x",
            ),
            (True, False),
        )
        self.assertEqual(antigravity.is_antigravity_process("agy.exe", "agy.exe"), (True, True))
        self.assertEqual(antigravity.is_antigravity_process("antigravity-cli.exe", "antigravity-cli.exe"), (True, True))
        # Windsurf 등 다른 Codeium 포크는 제외
        self.assertEqual(
            antigravity.is_antigravity_process(
                "language_server_windows_x64.exe",
                "C:\\Windsurf\\language_server_windows_x64.exe --app_data_dir windsurf --csrf_token x",
            ),
            (False, False),
        )

    def test_parse_netstat_ports_multilocale(self):
        sample = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:54321        0.0.0.0:0              LISTENING       4242
  TCP    [::1]:54322            [::]:0                 ABHÖREN         4242
  TCP    127.0.0.1:54321        127.0.0.1:59999        ESTABLISHED     4242
  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       9999
"""
        self.assertEqual(antigravity.parse_netstat_ports(sample, 4242), [54321, 54322])

    def test_build_candidate_endpoints_allows_tokenless_desktop_and_cli(self):
        desktop_eps = antigravity.build_candidate_endpoints([54321], {"https_server_port": "0"}, is_cli=False)
        self.assertEqual(
            [(e.scheme, e.port, e.csrf_token) for e in desktop_eps],
            [("https", 54321, ""), ("http", 54321, "")],
        )
        cli_eps = antigravity.build_candidate_endpoints([54321], {}, is_cli=True)
        self.assertEqual(
            [(e.scheme, e.port, e.csrf_token) for e in cli_eps],
            [("https", 54321, ""), ("http", 54321, "")],
        )

    def test_load_daemon_endpoints_uses_only_running_pid(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "hub_49654.json").write_text(
                json.dumps({"pid": 15076, "httpsPort": 49653, "httpPort": 49654, "lsVersion": "2.35.1", "csrfToken": "tok"}),
                encoding="utf-8",
            )
            (d / "hub_51140.json").write_text(
                json.dumps({"pid": 999, "httpsPort": 51139, "httpPort": 51140, "csrfToken": "stale"}),
                encoding="utf-8",
            )
            (d / "broken.json").write_text("{not json", encoding="utf-8")
            (d / "no_token.json").write_text(json.dumps({"pid": 15076, "httpsPort": 1}), encoding="utf-8")

            eps = antigravity.load_daemon_endpoints({15076}, d)
            self.assertEqual(
                [(e.scheme, e.port, e.csrf_token) for e in eps],
                [("https", 49653, "tok"), ("http", 49654, "tok")],
            )
            self.assertEqual(antigravity.load_daemon_endpoints(set(), d), [])
            self.assertEqual(antigravity.load_daemon_endpoints({15076}, d / "missing"), [])


class FetcherTests(unittest.TestCase):
    def test_fetcher_caches_endpoint_and_keeps_last_windows_when_stopped(self):
        fetcher = antigravity.AntigravityFetcher()
        ep = antigravity.LocalEndpoint("https", 54321, "tok")
        win = [UsageWindow("5시간", 20.0, resets_at=NOW + timedelta(hours=3))]

        with mock.patch.object(fetcher, "_discover_endpoints", return_value=[ep]) as disc, \
             mock.patch.object(antigravity, "_fetch_from_endpoint", return_value=win):
            s1 = fetcher.fetch(NOW)
            self.assertTrue(s1.ok)
            self.assertEqual(s1.windows[0].used_percent, 20.0)
            self.assertEqual(disc.call_count, 1)

            # 두 번째 호출은 캐시된 엔드포인트를 바로 쓰므로 _discover_endpoints를 다시 호출하지 않음
            s2 = fetcher.fetch(NOW + timedelta(minutes=1))
            self.assertTrue(s2.ok)
            self.assertEqual(disc.call_count, 1)

        # Antigravity가 종료되면 이전 값은 유지하고 오류 메시지를 표시
        with mock.patch.object(fetcher, "_discover_endpoints", return_value=[]), \
             mock.patch.object(antigravity, "_fetch_from_endpoint", return_value=[]):
            s3 = fetcher.fetch(NOW + timedelta(minutes=2))
            self.assertFalse(s3.ok)
            self.assertIn("실행 중이 아니", s3.error or "")
            self.assertEqual(len(s3.windows), 1)



class DebugDumpTests(unittest.TestCase):
    def test_fetch_falls_back_and_writes_sanitized_debug(self):
        import json
        import tempfile

        user_status = {
            "userStatus": {
                "email": "secret@example.com",
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "Gemini 3.1 Pro",
                            "modelOrAlias": {"model": "gemini-pro"},
                            "quotaInfo": {"remainingFraction": 0.7, "resetTime": (NOW + timedelta(hours=2)).isoformat()},
                        }
                    ]
                },
            }
        }

        def fake_rpc(endpoint, path, timeout=2.5):
            if path.endswith("RetrieveUserQuotaSummary"):
                raise OSError("404")
            return user_status

        with tempfile.TemporaryDirectory() as tmp:
            debug_file = Path(tmp) / "antigravity_debug.json"
            with mock.patch.object(antigravity, "_post_rpc", side_effect=fake_rpc), mock.patch.object(
                antigravity, "debug_path", return_value=debug_file
            ):
                windows = antigravity._fetch_from_endpoint(antigravity.LocalEndpoint("https", 1234, "t"), NOW)
            self.assertEqual([(w.label, w.used_percent) for w in windows], [("5시간", 30.0)])
            text = debug_file.read_text(encoding="utf-8")
            self.assertNotIn("secret@example.com", text)
            record = json.loads(text)
            self.assertEqual(record["attempts"][0]["rpc"], "RetrieveUserQuotaSummary")
            self.assertIn("error", record["attempts"][0])
            self.assertEqual(record["windows"][0]["usedPercent"], 30.0)

    def test_discovery_failure_writes_sanitized_debug(self):
        import json
        import tempfile

        fetcher = antigravity.AntigravityFetcher()
        fake_procs = [
            (
                1001,
                "language_server_windows_x64.exe",
                "C:\\Antigravity\\language_server_windows_x64.exe --random_port --csrf_token SUPER_SECRET_TOKEN",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            debug_file = Path(tmp) / "antigravity_debug.json"
            with mock.patch.object(antigravity, "_discover_processes_posix", return_value=fake_procs), \
                 mock.patch.object(antigravity, "_discover_listening_ports_posix", return_value=[]), \
                 mock.patch.object(antigravity, "_discover_processes_windows", return_value=fake_procs), \
                 mock.patch.object(antigravity, "_discover_listening_ports_windows", return_value=[]), \
                 mock.patch.object(antigravity, "debug_path", return_value=debug_file):
                state = fetcher.fetch(NOW)
            self.assertFalse(state.ok)
            self.assertIn("실행 중이 아니", state.error or "")
            text = debug_file.read_text(encoding="utf-8")
            self.assertNotIn("SUPER_SECRET_TOKEN", text)
            record = json.loads(text)
            self.assertEqual(record["discovery"][0]["matched"], True)
            self.assertIn("csrf_token", record["discovery"][0]["argKeys"])
            self.assertEqual(record["discovery"][0]["portCount"], 0)


if __name__ == "__main__":
    unittest.main()
