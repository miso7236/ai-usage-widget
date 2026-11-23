"""크롬 확장이 사용량을 보내 주는 로컬 HTTP 서버.

127.0.0.1에서만 열리므로 다른 컴퓨터에서는 접근할 수 없다.

요청 확인 규칙
- 웹페이지(Origin이 http/https 등)에서 온 요청은 언제나 거절한다.
- /update, /refresh, /wait-command 는 X-AIW-Token 헤더의 로컬 토큰(local_token.py)이 맞아야 한다.
  토큰 헤더가 있는데 틀리면 401.
- 하위 호환(LEGACY_TOKENLESS_ALLOWED): 토큰 파일이 없는 옛 확장을 한 버전 동안 받아 준다.
  · POST /update: Origin이 chrome-extension:// 이면 토큰 없이도 받고, 위젯에 "확장 다시 로드" 안내를 띄운다.
    (브라우저는 POST에 항상 Origin을 붙이므로 Origin 없는 POST는 확장이 아니다.)
  · GET /wait-command: Origin이 없거나 chrome-extension:// 이면 받는다.
  · POST /refresh 는 확장이 쓰지 않으므로 처음부터 토큰이 있어야 한다.
- /health 는 버전 정보만 돌려주므로 토큰 없이 열려 있다.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from local_token import TOKEN_HEADER, token_matches
from state import Store
from version import __version__

APP_NAME = "ai-usage-widget"
MAX_BODY = 64 * 1024

# 다음 버전에서 False로 바꾸면 토큰 없는 요청을 모두 거절한다.
LEGACY_TOKENLESS_ALLOWED = True

AUTH_TOKEN = "token"
AUTH_LEGACY = "legacy"


def _origin_allowed(origin: str | None) -> bool:
    # Origin이 없으면 브라우저 밖의 로컬 프로그램이거나 확장의 GET 요청. 있으면 크롬 확장만 허용.
    return origin is None or origin.startswith("chrome-extension://")


def check_auth(
    expected_token: str | None,
    given_token: str | None,
    origin: str | None,
    endpoint: str,
    allow_legacy: bool = LEGACY_TOKENLESS_ALLOWED,
) -> tuple[int, str | None]:
    """요청을 받을지 판단한다. (HTTP 상태, 인증 방식 또는 오류 메시지)

    200이면 두 번째 값은 AUTH_TOKEN / AUTH_LEGACY, 아니면 오류 메시지.
    """
    if not _origin_allowed(origin):
        return 403, "forbidden origin"
    if given_token is not None:
        if token_matches(expected_token, given_token):
            return 200, AUTH_TOKEN
        return 401, "invalid token"
    if allow_legacy:
        if endpoint == "/update" and origin is not None:  # origin은 chrome-extension://
            return 200, AUTH_LEGACY
        if endpoint == "/wait-command":
            return 200, AUTH_LEGACY
    return 401, "token required"


def make_handler(
    store: Store,
    on_update: Callable[[], None] | None = None,
    info: Callable[[], dict] | None = None,
    token: str | None = None,
    allow_legacy: bool = LEGACY_TOKENLESS_ALLOWED,
):
    def extra() -> dict:
        # 위젯 버전, 크롬 확장이 로드해야 할 버전, 새로고침 시퀀스 등.
        base = {"widgetVersion": __version__, "refreshSeq": store.refresh_seq}
        if info:
            try:
                base.update(info())
            except Exception:
                pass
        return base

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 콘솔에 접속 기록을 찍지 않음
            pass

        def _auth(self, endpoint: str) -> str | None:
            """인증 방식을 돌려준다. 거절했으면 응답을 보내고 None."""
            status, result = check_auth(
                token,
                self.headers.get(TOKEN_HEADER),
                self.headers.get("Origin"),
                endpoint,
                allow_legacy,
            )
            if status != 200:
                self._send(status, {"ok": False, "error": result})
                return None
            return result

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            origin = self.headers.get("Origin")
            if origin and _origin_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            origin = self.headers.get("Origin")
            if not _origin_allowed(origin):
                self._send(403, {"ok": False, "error": "forbidden origin"})
                return
            self.send_response(204)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", f"Content-Type, {TOKEN_HEADER}")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True, "app": APP_NAME, **extra()})
            elif parsed.path == "/wait-command":
                if self._auth("/wait-command") is None:
                    return
                qs = parse_qs(parsed.query)
                try:
                    after = int((qs.get("after") or ["0"])[0])
                except ValueError:
                    after = 0
                try:
                    timeout = float((qs.get("timeout") or ["20"])[0])
                except ValueError:
                    timeout = 20.0
                timeout = max(0.0, min(timeout, 25.0))
                triggered, seq = store.wait_for_refresh(after, timeout=timeout)
                self._send(200, {"ok": True, "refresh": triggered, "refreshSeq": seq})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path not in ("/refresh", "/update"):
                if not _origin_allowed(self.headers.get("Origin")):
                    self._send(403, {"ok": False, "error": "forbidden origin"})
                else:
                    self._send(404, {"ok": False, "error": "not found"})
                return
            auth = self._auth(parsed.path)
            if auth is None:
                return
            if parsed.path == "/refresh":
                seq = store.request_refresh()
                self._send(200, {"ok": True, "refreshSeq": seq})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._send(413 if length > MAX_BODY else 400, {"ok": False, "error": "bad body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                count = store.update(payload)
            except (ValueError, UnicodeDecodeError) as e:
                self._send(400, {"ok": False, "error": str(e)})
                return
            # 토큰 없는 옛 확장이면 위젯에 "확장 다시 로드" 안내를 띄운다.
            store.set_extension_auth(auth)
            if on_update:
                on_update()
            self._send(200, {"ok": True, "providers": count, "auth": auth, **extra()})

    return Handler


def start_server(
    store: Store,
    port: int,
    on_update: Callable[[], None] | None = None,
    info: Callable[[], dict] | None = None,
    token: str | None = None,
    allow_legacy: bool = LEGACY_TOKENLESS_ALLOWED,
) -> ThreadingHTTPServer:
    """백그라운드 스레드에서 서버를 시작한다. 포트가 이미 쓰이면 OSError가 난다.

    token이 None이면 토큰이 맞는 요청은 없고, 하위 호환 규칙으로만 받는다.
    """
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(store, on_update, info, token, allow_legacy)
    )
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="usage-server", daemon=True)
    thread.start()
    return server
