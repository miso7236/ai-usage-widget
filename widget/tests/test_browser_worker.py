import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_worker  # noqa: E402
import web_normalize  # noqa: E402
from state import ProviderState, Store, UsageWindow  # noqa: E402

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


class WebNormalizeTests(unittest.TestCase):
    def test_claude_org_selection_and_normalization(self):
        orgs = [
            {"uuid": "org-api", "capabilities": ["api"]},
            {"uuid": "org-chat", "capabilities": ["chat"]},
        ]
        self.assertEqual(web_normalize.pick_claude_org(orgs)["uuid"], "org-chat")
        self.assertEqual(web_normalize.pick_claude_org(orgs, "org-api")["uuid"], "org-api")

        res = web_normalize.normalize_claude(
            {
                "seven_day": {"utilization": 65.2, "resets_at": "2026-11-24T00:00:00Z"},
                "five_hour": {"utilization": 18, "resets_at": "2026-11-20T14:00:00Z"},
                "extra_usage": {"is_enabled": False, "utilization": 99},
            },
            now=NOW,
        )
        self.assertTrue(res["ok"])
        self.assertEqual([(w["label"], w["usedPercent"]) for w in res["windows"]], [("5시간", 18.0), ("주간", 65.2)])

    def test_chatgpt_normalization_with_relative_reset(self):
        res = web_normalize.normalize_chatgpt(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 33,
                        "limit_window_seconds": 5 * 3600,
                        "reset_after_seconds": 3600,
                    },
                    "secondary_window": {
                        "used_percent": 72,
                        "limit_window_seconds": 7 * 24 * 3600,
                        "reset_at": int((NOW + timedelta(days=2)).timestamp()),
                    },
                }
            },
            now=NOW,
        )
        self.assertTrue(res["ok"])
        self.assertEqual([(w["label"], w["usedPercent"]) for w in res["windows"]], [("5시간", 33.0), ("주간", 72.0)])
        self.assertEqual(res["windows"][0]["resetsAt"], (NOW + timedelta(hours=1)).isoformat())

    def test_gemini_normalization_and_reset_text(self):
        self.assertEqual(
            web_normalize.parse_gemini_reset_text("2시간 30분 후 재설정", now=NOW),
            (NOW + timedelta(hours=2, minutes=30)).isoformat(),
        )
        self.assertIsNone(web_normalize.parse_gemini_reset_text("5시간마다 초기화", now=NOW))

        res = web_normalize.normalize_gemini(
            [
                {"label": "5시간 한도 남음", "percent": 80, "resetText": "1시간 후"},
                {"label": "주간 사용량", "percent": 12, "resetText": "Nov 25, 9:00 AM"},
            ],
            now=NOW,
        )
        self.assertTrue(res["ok"])
        self.assertEqual([(w["label"], w["usedPercent"]) for w in res["windows"]], [("5시간", 20.0), ("주간", 12.0)])
        self.assertIsNotNone(res["windows"][0]["resetsAt"])
        self.assertIsNotNone(res["windows"][1]["resetsAt"])


class BrowserWorkerTests(unittest.TestCase):
    def test_unparsable_response_carries_shape_to_store(self):
        raw = {"ok": True, "data": {"limits": {"pct": 37, "token": "secret-value-123"}}}
        res = browser_worker.process_raw_provider("claude", raw, now=NOW)
        self.assertFalse(res["ok"])
        self.assertEqual(res["responseShape"], '{"limits":{"pct":"37","token":"string"}}')
        self.assertNotIn("secret-value-123", res["responseShape"])
        store = Store()
        store.set_web_source("browser")
        store.update_browser_providers([res])
        [state] = store.snapshot()
        self.assertEqual(state.response_shape, res["responseShape"])

    def test_shape_is_kept_when_last_values_are_reused(self):
        store = Store()
        store.set_web_source("browser")
        ok = web_normalize.normalize_claude({"five_hour": {"utilization": 10}}, NOW)
        store.update_browser_providers([ok])
        err = browser_worker.process_raw_provider("claude", {"ok": True, "data": {"x": 1}}, now=NOW)
        store.update_browser_providers([err])
        [state] = store.snapshot()
        self.assertEqual(len(state.windows), 1)
        self.assertEqual(state.response_shape, '{"x":"1"}')

    def test_process_raw_provider_success_and_error(self):
        ok = browser_worker.process_raw_provider(
            "claude",
            {"ok": True, "data": {"five_hour": {"utilization": 25, "resets_at": "2026-11-20T12:00:00Z"}}},
            now=NOW,
        )
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["windows"][0]["usedPercent"], 25.0)

        err = browser_worker.process_raw_provider("chatgpt", {"ok": False, "error": "로그인 필요"}, now=NOW)
        self.assertFalse(err["ok"])
        self.assertEqual(err["error"], "로그인 필요")

    def test_wait_js_result_catches_initial_exception(self):
        class BrokenWindow:
            def evaluate_js(self, script):
                raise RuntimeError("webview detached")

        res = browser_worker._wait_js_result(BrokenWindow(), "1+1", timeout=1.0)
        self.assertFalse(res["ok"])
        self.assertIn("webview detached", res["error"])

    def _fake_clock(self):
        t = {"now": 0.0}

        def clock():
            return t["now"]

        def sleep(s):
            t["now"] += s

        return clock, sleep

    def test_wait_js_result_reinjects_after_page_reload(self):
        class ReloadingWindow:
            """첫 주입 직후 페이지가 다시 로드되어 결과 변수가 사라지는 창."""

            def __init__(self):
                self.injected = 0
                self.probes = 0

            def evaluate_js(self, script):
                if script == browser_worker._PROBE_JS:
                    self.probes += 1
                    if self.injected == 1:
                        rs = "loading" if self.probes < 3 else "complete"
                        return json.dumps({"r": None, "s": "", "rs": rs})
                    return json.dumps({"r": json.dumps({"ok": True, "data": {}}), "s": "usage", "rs": "complete"})
                self.injected += 1
                return None

        clock, sleep = self._fake_clock()
        win = ReloadingWindow()
        res = browser_worker._wait_js_result(win, "SCRIPT", timeout=16.0, name="ChatGPT", sleep=sleep, clock=clock)
        self.assertTrue(res["ok"])
        self.assertEqual(win.injected, 2)

    def test_wait_js_result_timeout_reports_stage_and_reloads(self):
        class StuckWindow:
            def evaluate_js(self, script):
                if script == browser_worker._PROBE_JS:
                    return json.dumps({"r": "", "s": "usage", "rs": "complete"})
                return None

        clock, sleep = self._fake_clock()
        res = browser_worker._wait_js_result(
            StuckWindow(), "SCRIPT", timeout=16.0, name="ChatGPT", load_timed_out=True, sleep=sleep, clock=clock
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "ChatGPT 응답 시간 초과 (사용량 요청 단계, 16초, 페이지 로드 15초 초과)")
        self.assertNotIn("로그인", res["error"])

        class AlwaysReloading:
            def evaluate_js(self, script):
                if script == browser_worker._PROBE_JS:
                    return json.dumps({"r": None, "s": "", "rs": "complete"})
                return None

        clock, sleep = self._fake_clock()
        res = browser_worker._wait_js_result(AlwaysReloading(), "SCRIPT", timeout=16.0, name="ChatGPT", sleep=sleep, clock=clock)
        self.assertTrue(res["error"].startswith("ChatGPT 페이지가 수집 중 계속 다시 로드돼요 (다시 로드 2회"))

    def test_fetch_scripts_have_stages_and_request_timeout(self):
        for js in (browser_worker.CHATGPT_FETCH_JS, browser_worker.build_claude_fetch_js("org")):
            self.assertIn("AbortController", js)
            self.assertIn(str(browser_worker.FETCH_TIMEOUT_MS), js)
            self.assertIn('window.__aiw_stage = "start"', js)
            self.assertNotIn("__FETCH_TIMEOUT", js)
        self.assertIn('window.__aiw_stage = "scan"', browser_worker.GEMINI_EXTRACT_JS)

    def test_collector_keeps_finished_results_when_worker_times_out(self):
        collector = browser_worker.BrowserCollector()

        class SlowProc:
            def __init__(self, out_path: Path):
                self._out_path = out_path
                self._done = False

            def poll(self):
                return 0 if self._done else None

            def wait(self, timeout=None):
                if self._done:
                    return 0
                # Claude까지 끝내고 ChatGPT 도중에 시간 초과
                self._out_path.write_text(
                    json.dumps({"providers": [web_normalize.make_result("claude", [], NOW)]}), encoding="utf-8"
                )
                raise browser_worker.subprocess.TimeoutExpired("worker", timeout)

            def terminate(self):
                self._done = True

            def kill(self):
                self._done = True

        def fake_popen(cmd, **kwargs):
            return SlowProc(Path(cmd[cmd.index("--output") + 1]))

        with mock.patch.object(browser_worker.subprocess, "Popen", side_effect=fake_popen):
            items = collector.fetch_providers(["claude", "chatgpt"])

        self.assertEqual([i["provider"] for i in items], ["claude", "chatgpt"])
        self.assertTrue(items[0]["ok"])
        self.assertFalse(items[1]["ok"])
        self.assertIn("전체 95초", items[1]["error"])

    def test_prehandle_no_taskbar_only_sets_before_handle_creation(self):
        class FakeWindow:
            def __init__(self):
                self.native = None

        class FakeForm:
            def __init__(self, handle_created: bool):
                self.IsHandleCreated = handle_created
                self.ShowInTaskbar = True

        win = FakeWindow()
        self.assertTrue(browser_worker._attach_prehandle_no_taskbar(win))
        self.assertIsNone(win.native)

        # 핸들 생성 전에는 ShowInTaskbar=False를 안전하게 적용한다.
        pre_form = FakeForm(handle_created=False)
        win.native = pre_form
        self.assertIs(win.native, pre_form)
        self.assertFalse(pre_form.ShowInTaskbar)

        # 핸들이 이미 생성된 뒤에는 RecreateHandle 방지를 위해 건드리지 않는다.
        post_form = FakeForm(handle_created=True)
        win.native = post_form
        self.assertIs(win.native, post_form)
        self.assertTrue(post_form.ShowInTaskbar)

    def test_collector_fetches_output_and_serializes_with_login(self):
        collector = browser_worker.BrowserCollector()

        class FakeFetchProc:
            def __init__(self, out_path: Path):
                self._done = False
                self._out_path = out_path

            def poll(self):
                return 0 if self._done else None

            def wait(self, timeout=None):
                self._out_path.write_text(
                    json.dumps(
                        {
                            "providers": [
                                web_normalize.make_result(
                                    "claude",
                                    [{"key": "five_hour", "label": "5시간", "usedPercent": 40, "resetsAt": None, "resetText": None}],
                                    NOW,
                                )
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                self._done = True
                return 0

            def terminate(self):
                self._done = True

        def fake_popen(cmd, **kwargs):
            out_idx = cmd.index("--output") + 1
            return FakeFetchProc(Path(cmd[out_idx]))

        with mock.patch.object(browser_worker.subprocess, "Popen", side_effect=fake_popen):
            items = collector.fetch_providers(["claude"])

        self.assertIsNotNone(items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["provider"], "claude")
        self.assertEqual(items[0]["windows"][0]["usedPercent"], 40)

        # 로그인 창이 열려 있는 동안에는 백그라운드 수집을 건너뛴다(None 반환).
        class AliveProc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        collector._login_proc = AliveProc()
        self.assertTrue(collector.is_login_open)
        self.assertIsNone(collector.fetch_providers(["claude"]))
        collector.stop()

    def test_store_isolates_extension_and_browser_sources(self):
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
        store.set_local_provider(ProviderState("antigravity", "Antigravity", True, [UsageWindow("주간", 22)], fetched_at=NOW))

        # 내장 브라우저 데이터 반영 후 전환
        store.update_browser_providers(
            [
                web_normalize.make_result(
                    "claude",
                    [{"key": "five_hour", "label": "5시간", "usedPercent": 15, "resetsAt": None, "resetText": None}],
                    NOW,
                )
            ]
        )
        # 아직 extension 모드이므로 80% 유지
        self.assertEqual([p.provider for p in store.snapshot()], ["claude", "antigravity"])
        self.assertEqual(store.snapshot()[0].windows[0].used_percent, 80.0)

        # browser 모드로 전환하면 15% 표시 + Antigravity 유지
        store.set_web_source("browser")
        self.assertEqual(store.snapshot()[0].windows[0].used_percent, 15.0)
        self.assertEqual(store.snapshot()[1].provider, "antigravity")

        # 일시 오류 시 마지막 윈도우(15%)는 흐리게 유지하고 오류 메시지 표시
        store.update_browser_providers([web_normalize.make_error("claude", "일시 오류", NOW)])
        claude_state = store.snapshot()[0]
        self.assertFalse(claude_state.ok)
        self.assertEqual(claude_state.error, "일시 오류")
        self.assertEqual(claude_state.windows[0].used_percent, 15.0)

        # 다시 extension 모드로 돌아오면 크롬 확장 데이터(80%) 즉시 복원
        store.set_web_source("extension")
        self.assertEqual(store.snapshot()[0].windows[0].used_percent, 80.0)


if __name__ == "__main__":
    unittest.main()


class FetchStatsStoreTests(unittest.TestCase):
    def test_store_counts_only_active_source_and_local_providers(self):
        store = Store()
        store.set_web_source("browser")
        store.update_browser_providers([web_normalize.make_result("chatgpt", [], NOW)])
        store.update_browser_providers([web_normalize.make_error("chatgpt", "응답 시간 초과", NOW)])
        # 브라우저 모드에서 확장이 보낸 데이터는 세지 않는다.
        store.update({"providers": [web_normalize.make_result("claude", [], NOW)]})
        store.set_local_provider(ProviderState(provider="antigravity", name="Antigravity", ok=True))
        summary = store.fetch_stats.summary()
        self.assertEqual((summary["chatgpt"]["ok"], summary["chatgpt"]["fail"]), (1, 1))
        self.assertEqual(summary["chatgpt"]["last_error"], "응답 시간 초과")
        self.assertEqual(summary["chatgpt"]["fail_streak"], 1)
        self.assertNotIn("claude", summary)
        self.assertEqual(summary["antigravity"]["ok"], 1)
