"""크롬 확장이 사용량을 보내 주는 로컬 HTTP 서버.

127.0.0.1에서만 열리므로 다른 컴퓨터에서는 접근할 수 없다.
웹페이지가 몰래 값을 보내지 못하도록, Origin 헤더가 있으면 크롬 확장에서 온 요청만 받는다.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from state import Store

APP_NAME = "ai-usage-widget"
VERSION = "0.1.0"
MAX_BODY = 64 * 1024


def _origin_allowed(origin: str | None) -> bool:
    # Origin이 없으면 curl 같은 로컬 도구. 있으면 크롬 확장만 허용.
    return origin is None or origin.startswith("chrome-extension://")


def make_handler(store: Store, on_update: Callable[[], None] | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 콘솔에 접속 기록을 찍지 않음
            pass

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
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "app": APP_NAME, "version": VERSION})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if self.path != "/update":
                self._send(404, {"ok": False, "error": "not found"})
                return
            if not _origin_allowed(self.headers.get("Origin")):
                self._send(403, {"ok": False, "error": "forbidden origin"})
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
            if on_update:
                on_update()
            self._send(200, {"ok": True, "providers": count})

    return Handler


def start_server(store: Store, port: int, on_update: Callable[[], None] | None = None) -> ThreadingHTTPServer:
    """백그라운드 스레드에서 서버를 시작한다. 포트가 이미 쓰이면 OSError가 난다."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store, on_update))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="usage-server", daemon=True)
    thread.start()
    return server
