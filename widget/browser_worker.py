"""Windows 내장 Edge WebView2(pywebview) 기반 온디맨드 브라우저 워커.

Tkinter 위젯 본체와 별도 서브프로세스로 실행되므로:
- 평소에는 WebView2 프로세스를 띄우지 않아 상시 메모리를 ~30MB로 유지한다.
- 로그인 창을 열거나 사용량을 수집할 때만 잠깐 실행되고, 수집이 끝나면 즉시 종료한다.
- pywebview가 없는 환경(예: 리눅스 CI 단위 테스트)에서도 모듈 임포트가 실패하지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from web_normalize import (
    PROVIDER_NAMES,
    make_error,
    normalize_chatgpt,
    normalize_claude,
    normalize_gemini,
)

SUPPORTED_PROVIDERS = ("claude", "chatgpt", "gemini")

LOGIN_URLS = {
    "claude": "https://claude.ai/",
    "chatgpt": "https://chatgpt.com/",
    "gemini": "https://gemini.google.com/usage",
}

FETCH_URLS = {
    "claude": "https://claude.ai/",
    "chatgpt": "https://chatgpt.com/",
    "gemini": "https://gemini.google.com/usage",
}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0"
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 페이지 안 fetch 한 번의 제한 시간. 결과 대기(16초)보다 짧아야 어느 요청이 늦었는지 알 수 있다.
FETCH_TIMEOUT_MS = 10_000
FETCH_TIMEOUT_S = FETCH_TIMEOUT_MS // 1000
# 페이지 로드 이벤트 대기 시간
PAGE_LOAD_TIMEOUT = 15.0
# 서비스 하나를 수집하는 데 드는 최대 시간(로드 15 + 대기 16 + 재주입 여유)으로 워커 전체 제한 시간을 정한다.
PER_PROVIDER_BUDGET = 40.0

# 수집 스크립트가 window.__aiw_stage에 남기는 단계 이름
STAGE_NAMES = {
    "start": "스크립트 시작",
    "orgs": "조직 정보 요청",
    "session": "세션 요청",
    "usage": "사용량 요청",
    "scan": "사용량 화면 읽기",
}

# 결과·단계·문서 상태를 한 번에 읽는다. 페이지가 다시 로드되면 __aiw_result가 undefined가 된다.
_PROBE_JS = (
    "JSON.stringify({r: (typeof window.__aiw_result === 'string') ? window.__aiw_result : null,"
    " s: window.__aiw_stage || '', rs: document.readyState})"
)


def profile_dir() -> Path:
    """내장 브라우저 쿠키·세션을 영구 저장할 프로필 폴더."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "AIUsageWidget" if base else Path.home() / ".ai-usage-widget"
    return root / "browser-profile"


def is_available() -> bool:
    """pywebview 패키지가 현재 실행 환경에 포함되어 있는지 빠르게 확인한다."""
    try:
        return importlib.util.find_spec("webview") is not None
    except (ImportError, ValueError):
        return False


def check_browser_runtime() -> tuple[bool, str]:
    """실제 pywebview 런타임(Windows에서는 EdgeChromium/WebView2 + WinForms 바인딩)을 로드할 수 있는지 검증한다."""
    try:
        import webview  # type: ignore[import-not-found]
        if sys.platform == "win32":
            import webview.platforms.edgechromium  # type: ignore[import-not-found] # noqa: F401
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            dummy_win = webview.create_window("check", html="", hidden=True, focus=False)
            if not _attach_prehandle_no_taskbar(dummy_win):
                return False, "pre-handle ShowInTaskbar hook failed to attach"
            form = WinForms.Form()
            try:
                dummy_win.native = form
                if form.IsHandleCreated:
                    return False, "ShowInTaskbar hook unexpectedly created WinForms handle early"
                if form.ShowInTaskbar is not False:
                    return False, f"expected ShowInTaskbar=False, got {form.ShowInTaskbar!r}"
                _ = form.Handle.ToInt64()
                if form.ShowInTaskbar is not False:
                    return False, "ShowInTaskbar reset after Handle creation"
            finally:
                form.Dispose()
                if dummy_win in webview.windows:
                    webview.windows.remove(dummy_win)
        return True, f"pywebview {getattr(webview, '__version__', 'unknown')} ready"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def worker_command(extra_args: list[str]) -> list[str]:
    """서브프로세스 워커 실행 명령줄을 만든다 (PyInstaller exe / 소스 실행 모두 지원)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra_args]
    script = Path(__file__).resolve().parent / "widget.py"
    return [sys.executable, str(script), *extra_args]


# ---------------- 페이지 내부 실행 JS ----------------
def build_claude_fetch_js(preferred_org_id: str | None = None) -> str:
    pref_json = json.dumps(preferred_org_id or "")
    return f"""(() => {{
  window.__aiw_result = "";
  window.__aiw_stage = "start";
  const preferredId = {pref_json};
  const done = (obj) => {{ window.__aiw_result = JSON.stringify(obj); }};
  async function getJson(url, stage, stageName) {{
    window.__aiw_stage = stage;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), {FETCH_TIMEOUT_MS});
    try {{
      const res = await fetch(url, {{ credentials: "include", cache: "no-store", headers: {{ Accept: "application/json" }}, signal: ctrl.signal }});
      return await readJson(res);
    }} catch (e) {{
      if (e && e.name === "AbortError") {{
        throw new Error("Claude 서버 응답이 늦어요 (" + stageName + " {FETCH_TIMEOUT_S}초 초과)");
      }}
      throw e;
    }} finally {{
      clearTimeout(timer);
    }}
  }}
  async function readJson(res) {{
    if (res.status === 401 || res.status === 403) {{
      throw new Error("Claude 로그인이 필요해요 (내장 브라우저에서 로그인)");
    }}
    if (res.status === 429) {{
      throw new Error("Claude 요청이 너무 많아요. 잠시 후 다시 시도해요");
    }}
    if (!res.ok) {{
      throw new Error("Claude 오류 (HTTP " + res.status + ")");
    }}
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    const text = await res.text();
    if (!ct.includes("json")) {{
      throw new Error("Claude 보안 확인이 필요해요 (로그인 창을 열어 통과해 주세요)");
    }}
    return JSON.parse(text);
  }}
  (async () => {{
    try {{
      if (location.hostname !== "claude.ai") {{
        return done({{ ok: false, error: "Claude 로그인이 필요해요 (내장 브라우저에서 로그인)" }});
      }}
      const orgs = await getJson("/api/organizations", "orgs", "조직 정보 요청");
      if (!Array.isArray(orgs) || orgs.length === 0) {{
        return done({{ ok: false, error: "Claude 조직 정보를 찾지 못했어요 (로그인 확인)" }});
      }}
      let org = preferredId ? orgs.find((o) => o && o.uuid === preferredId) : null;
      if (!org) org = orgs.find((o) => o && Array.isArray(o.capabilities) && o.capabilities.includes("chat"));
      if (!org) org = orgs[0];
      if (!org || !org.uuid) {{
        return done({{ ok: false, error: "Claude 조직 정보를 찾지 못했어요 (로그인 확인)" }});
      }}
      const usage = await getJson("/api/organizations/" + encodeURIComponent(org.uuid) + "/usage", "usage", "사용량 요청");
      done({{ ok: true, data: usage }});
    }} catch (e) {{
      done({{ ok: false, error: (e && e.message) ? String(e.message) : String(e) }});
    }}
  }})();
}})();"""


CHATGPT_FETCH_JS = """(() => {
  window.__aiw_result = "";
  window.__aiw_stage = "start";
  const done = (obj) => { window.__aiw_result = JSON.stringify(obj); };
  async function getJson(url, stage, stageName, extraHeaders) {
    window.__aiw_stage = stage;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), __FETCH_TIMEOUT_MS__);
    try {
      const res = await fetch(url, {
        credentials: "include",
        cache: "no-store",
        headers: Object.assign({ Accept: "application/json" }, extraHeaders || {}),
        signal: ctrl.signal,
      });
      return await readJson(res);
    } catch (e) {
      if (e && e.name === "AbortError") {
        throw new Error("ChatGPT 서버 응답이 늦어요 (" + stageName + " __FETCH_TIMEOUT_S__초 초과)");
      }
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }
  async function readJson(res) {
    if (res.status === 401 || res.status === 403) {
      throw new Error("ChatGPT 로그인이 필요해요 (내장 브라우저에서 로그인)");
    }
    if (res.status === 429) {
      throw new Error("ChatGPT 요청이 너무 많아요. 잠시 후 다시 시도해요");
    }
    if (!res.ok) {
      throw new Error("ChatGPT 오류 (HTTP " + res.status + ")");
    }
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    const text = await res.text();
    if (!ct.includes("json")) {
      throw new Error("ChatGPT 보안 확인이 필요해요 (로그인 창을 열어 통과해 주세요)");
    }
    return JSON.parse(text);
  }
  (async () => {
    try {
      if (!location.hostname.endsWith("chatgpt.com")) {
        return done({ ok: false, error: "ChatGPT 로그인이 필요해요 (내장 브라우저에서 로그인)" });
      }
      const session = await getJson("/api/auth/session", "session", "세션 요청");
      const token = session && session.accessToken;
      if (!token) {
        return done({ ok: false, error: "ChatGPT 로그인이 필요해요 (내장 브라우저에서 로그인)" });
      }
      const headers = { Authorization: "Bearer " + token };
      if (session.account && session.account.id) {
        headers["ChatGPT-Account-Id"] = session.account.id;
      }
      const usage = await getJson("/backend-api/wham/usage", "usage", "사용량 요청", headers);
      done({ ok: true, data: usage });
    } catch (e) {
      done({ ok: false, error: (e && e.message) ? String(e.message) : String(e) });
    }
  })();
})();""".replace("__FETCH_TIMEOUT_MS__", str(FETCH_TIMEOUT_MS)).replace("__FETCH_TIMEOUT_S__", str(FETCH_TIMEOUT_MS // 1000))


# extension/content/gemini-usage.js와 동일한 Shadow DOM + progressbar + % 추출 로직
GEMINI_EXTRACT_JS = """(() => {
  window.__aiw_result = "";
  window.__aiw_stage = "scan";
  const USAGE_PATH_RE = /^\\/(?:u\\/\\d+\\/)?usage\\b/;
  const RESET_WORDS = /(재설정|초기화|리셋|갱신|reset|refresh)/i;
  const REMAINING_WORDS = /(남음|남은|잔여|remaining|left)/i;
  const LOGIN_WORDS = /(로그인|sign in|log in)/i;

  function textOf(el) {
    return ((el && (el.innerText || el.textContent)) || "").trim();
  }
  function isVisibleElement(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
    if (el.closest && el.closest('[aria-hidden="true"], [hidden], script, style, noscript, svg')) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    return true;
  }
  function queryAllDeep(root, selector, out = []) {
    if (!root || !root.querySelectorAll) return out;
    for (const el of root.querySelectorAll(selector)) out.push(el);
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) queryAllDeep(el.shadowRoot, selector, out);
    }
    return out;
  }
  function walkTextDeep(root, visitor) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) visitor(node);
    if (root.querySelectorAll) {
      for (const el of root.querySelectorAll("*")) {
        if (el.shadowRoot) walkTextDeep(el.shadowRoot, visitor);
      }
    }
  }
  function isLoginRedirect() {
    if (location.hostname !== "gemini.google.com" || !USAGE_PATH_RE.test(location.pathname)) return true;
    const signInLink = document.querySelector('a[href*="accounts.google.com"]');
    if (signInLink && isVisibleElement(signInLink) && LOGIN_WORDS.test(textOf(signInLink))) {
      return queryAllDeep(document, '[role="progressbar"], progress[value]').length === 0;
    }
    return false;
  }
  function parentOf(el) {
    if (!el) return null;
    if (el.parentElement) return el.parentElement;
    const root = el.getRootNode && el.getRootNode();
    return root && root.host ? root.host : null;
  }
  function findContainer(el) {
    let cur = el;
    for (let i = 0; i < 6; i++) {
      const next = parentOf(cur);
      if (!next) break;
      cur = next;
      const t = textOf(cur);
      if (t.length >= 10 && t.length <= 300 && t.includes("\\n")) return cur;
    }
    return parentOf(el) || el;
  }
  function lines(el) {
    return textOf(el).split("\\n").map((s) => s.trim()).filter(Boolean);
  }
  function labelFrom(el, container) {
    const labelledBy = el.getAttribute && el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const root = (el.getRootNode && el.getRootNode()) || document;
      const ref = (root.getElementById && root.getElementById(labelledBy)) || document.getElementById(labelledBy);
      const refText = textOf(ref);
      if (refText && refText.length <= 60) return refText;
    }
    const aria = el.getAttribute && el.getAttribute("aria-label");
    if (aria && aria.length <= 60) return aria;
    const candidate = lines(container).find((l) => !l.includes("%") && !RESET_WORDS.test(l) && l.length >= 2 && l.length <= 40);
    return candidate || "";
  }
  function resetFrom(container) {
    return lines(container).find((l) => RESET_WORDS.test(l) && l.length <= 80) || null;
  }
  function dedupeItems(items) {
    const seen = new Set();
    return items.filter((item) => {
      const key = item.label + "|" + item.percent + "|" + (item.resetText || "");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }
  function extract() {
    if (!document.body) return [];
    const items = [];
    for (const el of queryAllDeep(document, '[role="progressbar"], progress[value]')) {
      if (!isVisibleElement(el)) continue;
      const rawNow = el.tagName === "PROGRESS" ? el.getAttribute("value") : el.getAttribute("aria-valuenow");
      const now = parseFloat(rawNow);
      if (!Number.isFinite(now)) continue;
      const rawMax = el.tagName === "PROGRESS" ? el.getAttribute("max") : el.getAttribute("aria-valuemax");
      const max = parseFloat(rawMax || "100");
      const percent = max > 0 ? (now / max) * 100 : now;
      const container = findContainer(el);
      items.push({
        label: labelFrom(el, container),
        percent,
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    }
    if (items.length > 0) return dedupeItems(items);
    walkTextDeep(document.body, (node) => {
      const m = node.textContent && node.textContent.match(/(\\d{1,3}(?:[.,]\\d+)?)\\s*%/);
      if (!m) return;
      const parent = node.parentElement;
      if (!isVisibleElement(parent)) return;
      const container = findContainer(parent);
      items.push({
        label: labelFrom(parent, container),
        percent: parseFloat(m[1].replace(",", ".")),
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    });
    return dedupeItems(items);
  }

  let finished = false;
  let lastJson = "";
  let settleTimer = null;
  let pollTimer = null;
  let timeoutTimer = null;

  function finish(payload) {
    if (finished) return;
    finished = true;
    clearTimeout(settleTimer);
    clearTimeout(timeoutTimer);
    clearInterval(pollTimer);
    window.__aiw_result = JSON.stringify(payload);
  }

  function check() {
    if (finished) return;
    if (isLoginRedirect()) {
      return finish({ ok: false, error: "Gemini 로그인이 필요해요 (내장 브라우저에서 로그인)" });
    }
    const items = extract();
    if (items.length === 0) {
      lastJson = "";
      clearTimeout(settleTimer);
      return;
    }
    const j = JSON.stringify(items);
    if (j !== lastJson) {
      lastJson = j;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => finish({ ok: true, items }), 800);
    }
  }

  timeoutTimer = setTimeout(() => {
    finish({ ok: false, error: "Gemini 사용량 화면에서 숫자를 찾지 못했어요 (" + location.pathname + ")" });
  }, 14000);
  pollTimer = setInterval(check, 600);
  check();
})();"""


def process_raw_provider(
    provider: str,
    raw: object,
    now: datetime | None = None,
) -> dict:
    """브라우저 워커 JS가 돌려준 원시 결과를 위젯 표준 provider dict로 변환한다."""
    if not isinstance(raw, dict):
        return make_error(provider, f"{PROVIDER_NAMES.get(provider, provider)} 응답을 읽지 못했어요", now)
    if not raw.get("ok"):
        return make_error(
            provider,
            str(raw.get("error") or f"{PROVIDER_NAMES.get(provider, provider)} 로그인이 필요해요"),
            now,
        )
    try:
        if provider == "claude":
            return normalize_claude(raw.get("data"), now)
        if provider == "chatgpt":
            return normalize_chatgpt(raw.get("data"), now)
        if provider == "gemini":
            return normalize_gemini(raw.get("items"), now)
        return make_error(provider, f"지원하지 않는 서비스: {provider}", now)
    except ValueError as e:
        return make_error(provider, str(e), now, getattr(e, "response_shape", None))


# ---------------- 워커 프로세스 실행부 ----------------
def _webview_gui() -> str | None:
    return "edgechromium" if sys.platform == "win32" else None


def run_login(provider: str, storage_path: Path | None = None) -> int:
    """사용자가 직접 로그인할 수 있도록 내장 브라우저(WebView2) 창을 띄운다."""
    if provider not in LOGIN_URLS:
        return 2
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return 3

    store_dir = Path(storage_path) if storage_path else profile_dir()
    store_dir.mkdir(parents=True, exist_ok=True)
    title = f"{PROVIDER_NAMES.get(provider, provider)} 로그인 — 로그인 후 창을 닫으세요"
    webview.create_window(
        title,
        url=LOGIN_URLS[provider],
        width=1040,
        height=760,
        resizable=True,
    )
    webview.start(
        gui=_webview_gui(),
        private_mode=False,
        storage_path=str(store_dir),
        user_agent=DEFAULT_USER_AGENT,
    )
    return 0


def _read_probe(window) -> dict | None:
    raw = window.evaluate_js(_PROBE_JS)
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def format_timeout_error(name: str, stage: str, elapsed: float, reloads: int = 0, load_timed_out: bool = False) -> str:
    """시간 초과 오류 문구: 어느 단계에서 얼마나 기다렸는지와 페이지 상태를 함께 적는다."""
    parts = [f"{STAGE_NAMES.get(stage, '스크립트 실행 전')} 단계", f"{round(elapsed)}초"]
    if reloads:
        parts.append(f"페이지 다시 로드됨 {reloads}회")
    if load_timed_out:
        parts.append(f"페이지 로드 {round(PAGE_LOAD_TIMEOUT)}초 초과")
    prefix = f"{name} " if name else ""
    return f"{prefix}응답 시간 초과 ({', '.join(parts)})"


def _wait_js_result(
    window,
    script: str,
    timeout: float = 16.0,
    *,
    name: str = "",
    load_timed_out: bool = False,
    reinject_limit: int = 1,
    reinject_budget: float = 12.0,
    sleep=time.sleep,
    clock=time.monotonic,
) -> dict:
    """스크립트를 넣고 window.__aiw_result가 채워질 때까지 기다린다.

    주입 뒤 페이지가 다시 로드되면(ChatGPT 새 버전 반영, 보안 확인 등) 결과 변수가 사라지므로
    이를 감지해 문서 로드가 끝난 뒤 스크립트를 한 번 다시 넣는다.
    """
    started = clock()
    try:
        window.evaluate_js(script)
    except Exception as e:
        return {"ok": False, "error": f"브라우저 스크립트 실행 실패: {type(e).__name__}: {e}"}
    deadline = started + timeout
    reloads = 0
    lost = False
    stage = ""
    while clock() < deadline:
        try:
            probe = _read_probe(window)
        except Exception as e:
            return {"ok": False, "error": f"브라우저 스크립트 오류: {type(e).__name__}: {e}"}
        if probe is not None:
            if probe.get("s"):
                stage = str(probe["s"])
            result = probe.get("r")
            if result is None:
                # 페이지가 새로 로드되어 스크립트 상태가 사라졌다.
                if not lost:
                    lost = True
                    reloads += 1
                if reloads > reinject_limit:
                    prefix = f"{name} " if name else ""
                    return {
                        "ok": False,
                        "error": f"{prefix}페이지가 수집 중 계속 다시 로드돼요 (다시 로드 {reloads}회, {round(clock() - started)}초)",
                    }
                if probe.get("rs") == "complete":
                    # 새 문서 로드가 끝났으면 스크립트를 다시 넣는다.
                    try:
                        window.evaluate_js(script)
                    except Exception as e:
                        return {"ok": False, "error": f"브라우저 스크립트 실행 실패: {type(e).__name__}: {e}"}
                    lost = False
                    stage = ""
                    deadline = max(deadline, clock() + reinject_budget)
            elif isinstance(result, str) and result:
                try:
                    return json.loads(result)
                except ValueError:
                    return {"ok": False, "error": "브라우저 응답 JSON 파싱 실패"}
        sleep(0.3)
    return {
        "ok": False,
        "error": format_timeout_error(name, stage, clock() - started, reloads, load_timed_out),
    }


def _attach_prehandle_no_taskbar(window) -> bool:
    """BrowserForm 핸들이 생성되기 직전(window.native 대입 시점)에 작업 표시줄 숨김을 적용한다.

    WinForms에서는 Handle이 이미 만들어진 뒤 ShowInTaskbar를 바꾸면 RecreateHandle()이
    발생해 WebView2 컨트롤이 분리되므로, 반드시 IsHandleCreated == False일 때만 적용한다.
    """
    base_cls = type(window)
    if getattr(base_cls, "_aiw_no_taskbar_window", False):
        return True

    has_native = "native" in window.__dict__
    existing = window.__dict__.pop("native", None)
    window.__dict__["_aiw_native"] = existing

    class _NoTaskbarWindow(base_cls):
        _aiw_no_taskbar_window = True

        @property
        def native(self):
            return self.__dict__.get("_aiw_native")

        @native.setter
        def native(self, form):
            self.__dict__["_aiw_native"] = form
            if form is not None and not getattr(form, "IsHandleCreated", True):
                try:
                    form.ShowInTaskbar = False
                except Exception:
                    pass

    try:
        window.__class__ = _NoTaskbarWindow
        return True
    except TypeError:
        window.__dict__.pop("_aiw_native", None)
        if has_native:
            window.__dict__["native"] = existing
        return False


def _hide_native_window_taskbar(win) -> None:
    """WinForms 핸들을 재생성하지 않고 Win32 확장 스타일만으로 작업 표시줄에서 숨긴다."""
    if sys.platform != "win32":
        return
    try:
        import tray

        native = getattr(win, "native", None)
        handle = getattr(native, "Handle", None)
        if handle is not None:
            hwnd = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
            tray.hide_from_taskbar(hwnd, resolve_parent=False)
    except Exception:
        pass


def check_hidden_browser_runtime(output_path: Path | None = None) -> tuple[bool, str]:
    """숨김 WebView2 창이 작업 표시줄 숨김(ShowInTaskbar=False) 상태로 JS를 정상 실행하는지 검증한다."""
    result: dict[str, object] = {"ok": False, "error": "초기화되지 않음"}
    try:
        import webview  # type: ignore[import-not-found]
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if output_path is not None:
            Path(output_path).write_text(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), encoding="utf-8")
        return False, msg

    loaded = threading.Event()
    with tempfile.TemporaryDirectory(prefix="aiw_check_", ignore_cleanup_errors=True) as tmp_dir:
        window = webview.create_window(
            "AIUsageWidget SelfCheck",
            html="<!doctype html><html><body><script>window.__aiw_ping = 42;</script></body></html>",
            width=320,
            height=240,
            hidden=True,
            focus=False,
        )
        _attach_prehandle_no_taskbar(window)
        window.events.loaded += lambda: loaded.set()

        def worker(win):
            try:
                loaded.wait(timeout=15.0)
                native = getattr(win, "native", None)
                show_in_taskbar = getattr(native, "ShowInTaskbar", None)
                val = win.evaluate_js("window.__aiw_ping")
                ok = val == 42 and (sys.platform != "win32" or show_in_taskbar is False)
                result.clear()
                result.update(
                    {
                        "ok": ok,
                        "js_value": val,
                        "show_in_taskbar": show_in_taskbar,
                    }
                )
                if not ok:
                    result["error"] = f"unexpected check result: js={val!r}, ShowInTaskbar={show_in_taskbar!r}"
            except Exception as e:
                result.clear()
                result.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
            finally:
                try:
                    win.destroy()
                except Exception:
                    pass

        webview.start(
            func=worker,
            args=(window,),
            gui=_webview_gui(),
            private_mode=True,
            storage_path=tmp_dir,
            user_agent=DEFAULT_USER_AGENT,
        )

    if output_path is not None:
        try:
            Path(output_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    if result.get("ok"):
        return True, f"hidden WebView2 ok (ShowInTaskbar={result.get('show_in_taskbar')}, js={result.get('js_value')})"
    return False, str(result.get("error") or "hidden WebView2 check failed")


def _write_results(output_path: Path, results: list[dict]) -> None:
    try:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps({"providers": results}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out)
    except OSError:
        pass


def run_fetch(
    providers: list[str],
    output_path: Path,
    claude_org_id: str | None = None,
    storage_path: Path | None = None,
) -> int:
    """숨겨진 WebView2 창 1개로 지정된 서비스들의 사용량을 순차 수집하고 종료한다."""
    valid = [p for p in providers if p in FETCH_URLS]
    if not valid:
        return 2
    try:
        import webview  # type: ignore[import-not-found]
    except ImportError:
        return 3

    store_dir = Path(storage_path) if storage_path else profile_dir()
    store_dir.mkdir(parents=True, exist_ok=True)

    loaded = threading.Event()
    results: list[dict] = []

    window = webview.create_window(
        "AIUsageWidget Worker",
        url=FETCH_URLS[valid[0]],
        width=1024,
        height=768,
        hidden=True,
        focus=False,
    )
    _attach_prehandle_no_taskbar(window)

    def on_before_show():
        # 사전 핸들 훅이 적용되지 않은 환경에 대비한 안전한 보조 스타일 적용
        _hide_native_window_taskbar(window)

    def on_loaded():
        loaded.set()

    if hasattr(window.events, "before_show"):
        window.events.before_show += on_before_show
    window.events.loaded += on_loaded

    def worker(win):
        try:
            _hide_native_window_taskbar(win)
            for idx, provider in enumerate(valid):
                try:
                    if idx > 0:
                        loaded.clear()
                        win.load_url(FETCH_URLS[provider])
                    load_timed_out = not loaded.wait(timeout=PAGE_LOAD_TIMEOUT)
                    time.sleep(0.8)
                    name = PROVIDER_NAMES.get(provider, provider)
                    if provider == "claude":
                        script, wait = build_claude_fetch_js(claude_org_id), 16.0
                    elif provider == "chatgpt":
                        script, wait = CHATGPT_FETCH_JS, 16.0
                    else:
                        script, wait = GEMINI_EXTRACT_JS, 18.0
                    raw = _wait_js_result(win, script, timeout=wait, name=name, load_timed_out=load_timed_out)
                    results.append(process_raw_provider(provider, raw, now=datetime.now(timezone.utc)))
                except Exception as e:
                    results.append(
                        make_error(provider, f"내장 브라우저 오류 ({type(e).__name__}: {e})", now=datetime.now(timezone.utc))
                    )
                # 워커 전체가 시간 초과로 끊겨도 끝난 서비스 결과는 남도록 매번 저장한다.
                _write_results(output_path, results)
        finally:
            _write_results(output_path, results)
            try:
                win.destroy()
            except Exception:
                pass

    webview.start(
        func=worker,
        args=(window,),
        gui=_webview_gui(),
        private_mode=False,
        storage_path=str(store_dir),
        user_agent=DEFAULT_USER_AGENT,
    )
    return 0


# ---------------- 메인 위젯 측 서브프로세스 관리자 ----------------
class BrowserCollector:
    """메인 Tkinter 위젯에서 온디맨드 WebView2 서브프로세스를 안전하게 조율한다.

    WebView2는 프로필 폴더(storage_path)를 점유하므로 로그인 창과 백그라운드 수집이
    동시에 돌지 않도록 단일 락으로 직렬화한다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fetch_proc: subprocess.Popen | None = None
        self._login_proc: subprocess.Popen | None = None

    @property
    def is_login_open(self) -> bool:
        with self._lock:
            return self._login_proc is not None and self._login_proc.poll() is None

    def _terminate_proc(self, proc: subprocess.Popen | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def open_login(self, provider: str, on_closed=None) -> bool:
        """로그인 창 서브프로세스를 띄운다. 이미 로그인 창이 열려 있으면 False."""
        if provider not in LOGIN_URLS:
            return False
        with self._lock:
            if self._login_proc is not None and self._login_proc.poll() is None:
                return False
            # 프로필 잠금 충돌을 막기 위해 실행 중인 백그라운드 수집 워커를 먼저 끝낸다.
            self._terminate_proc(self._fetch_proc)
            self._fetch_proc = None
            cmd = worker_command(["--browser-login", provider])
            proc = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
            self._login_proc = proc

        def watch():
            try:
                proc.wait()
            finally:
                with self._lock:
                    if self._login_proc is proc:
                        self._login_proc = None
                if callable(on_closed):
                    on_closed()

        threading.Thread(target=watch, daemon=True).start()
        return True

    def fetch_providers(
        self,
        providers: list[str],
        *,
        claude_org_id: str | None = None,
        timeout: float | None = None,
    ) -> list[dict] | None:
        """온디맨드 워커를 잠깐 띄워 사용량을 수집하고 결과를 돌려준다.

        로그인 창이 열려 있거나 이미 수집 중이면 None을 반환한다.
        """
        valid = [p for p in providers if p in FETCH_URLS]
        if not valid:
            return []

        fd, tmp_name = tempfile.mkstemp(prefix="aiw_browser_", suffix=".json")
        os.close(fd)
        out_path = Path(tmp_name)

        try:
            with self._lock:
                if self._login_proc is not None and self._login_proc.poll() is None:
                    return None
                if self._fetch_proc is not None and self._fetch_proc.poll() is None:
                    return None
                extra = ["--browser-fetch", ",".join(valid), "--output", str(out_path)]
                if claude_org_id:
                    extra.extend(["--claude-org-id", str(claude_org_id)])
                proc = subprocess.Popen(worker_command(extra), creationflags=CREATE_NO_WINDOW)
                self._fetch_proc = proc

            if timeout is None:
                # 브라우저 시작 여유 15초 + 서비스당 최대 수집 시간
                timeout = 15.0 + PER_PROVIDER_BUDGET * len(valid)
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_proc(proc)
                timed_out = True
            finally:
                with self._lock:
                    if self._fetch_proc is proc:
                        self._fetch_proc = None

            items: list = []
            if out_path.is_file() and out_path.stat().st_size > 0:
                data = json.loads(out_path.read_text(encoding="utf-8", errors="replace"))
                got = data.get("providers") if isinstance(data, dict) else None
                items = [i for i in got if isinstance(i, dict)] if isinstance(got, list) else []
            if timed_out:
                # 끝난 서비스 결과는 살리고, 못 끝낸 서비스만 오류로 채운다.
                done = {i.get("provider") for i in items}
                msg = f"내장 브라우저 응답 시간 초과 (전체 {round(timeout)}초)"
                return items + [make_error(p, msg) for p in valid if p not in done]
            if items:
                return items
            if not out_path.is_file() or out_path.stat().st_size == 0:
                return [make_error(p, "내장 브라우저 수집 실패 (WebView2 런타임 확인)") for p in valid]
            return [make_error(p, "내장 브라우저에서 데이터를 읽지 못했어요") for p in valid]
        except (OSError, ValueError) as e:
            return [make_error(p, f"내장 브라우저 오류: {e}") for p in valid]
        finally:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass

    def stop(self) -> None:
        with self._lock:
            self._terminate_proc(self._fetch_proc)
            self._terminate_proc(self._login_proc)
            self._fetch_proc = None
            self._login_proc = None
