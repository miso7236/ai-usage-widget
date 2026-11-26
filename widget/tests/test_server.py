"""로컬 서버의 토큰 확인 (server.check_auth, X-AIW-Token)"""

import json
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import AUTH_LEGACY, AUTH_TOKEN, check_auth, start_server  # noqa: E402
from state import Store, demo_payload  # noqa: E402

TOKEN = "cd" * 32
EXT = "chrome-extension://abcdefghijklmnop"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class CheckAuthTests(unittest.TestCase):
    def test_valid_token(self):
        for endpoint in ("/update", "/refresh", "/wait-command"):
            self.assertEqual(check_auth(TOKEN, TOKEN, EXT, endpoint), (200, AUTH_TOKEN))
            self.assertEqual(check_auth(TOKEN, TOKEN, None, endpoint), (200, AUTH_TOKEN))

    def test_wrong_token_rejected_even_from_extension(self):
        self.assertEqual(check_auth(TOKEN, "ef" * 32, EXT, "/update"), (401, "invalid token"))
        self.assertEqual(check_auth(TOKEN, "", EXT, "/update"), (401, "invalid token"))
        # 서버에 토큰이 없으면 어떤 토큰도 맞지 않는다
        self.assertEqual(check_auth(None, TOKEN, EXT, "/update"), (401, "invalid token"))

    def test_web_origin_rejected_even_with_token(self):
        self.assertEqual(check_auth(TOKEN, TOKEN, "https://evil.example", "/update")[0], 403)
        self.assertEqual(check_auth(TOKEN, TOKEN, "null", "/wait-command")[0], 403)

    def test_legacy_tokenless(self):
        # 옛 확장: POST /update는 chrome-extension Origin이면 받는다
        self.assertEqual(check_auth(TOKEN, None, EXT, "/update", allow_legacy=True), (200, AUTH_LEGACY))
        # Origin 없는 POST는 확장이 아니다 (브라우저는 POST에 항상 Origin을 붙인다)
        self.assertEqual(check_auth(TOKEN, None, None, "/update", allow_legacy=True)[0], 401)
        # 옛 확장의 GET /wait-command는 Origin이 없을 수 있다
        self.assertEqual(check_auth(TOKEN, None, None, "/wait-command", allow_legacy=True), (200, AUTH_LEGACY))
        self.assertEqual(check_auth(TOKEN, None, EXT, "/wait-command", allow_legacy=True), (200, AUTH_LEGACY))
        # /refresh는 확장이 쓰지 않으므로 처음부터 토큰이 필요하다
        self.assertEqual(check_auth(TOKEN, None, EXT, "/refresh", allow_legacy=True)[0], 401)
        self.assertEqual(check_auth(TOKEN, None, None, "/refresh", allow_legacy=True)[0], 401)

    def test_strict_mode_requires_token(self):
        for endpoint in ("/update", "/refresh", "/wait-command"):
            for origin in (EXT, None):
                self.assertEqual(
                    check_auth(TOKEN, None, origin, endpoint, allow_legacy=False),
                    (401, "token required"),
                )


class ServerAuthTestBase(unittest.TestCase):
    allow_legacy = True

    @classmethod
    def setUpClass(cls):
        cls.store = Store()
        cls.server = start_server(cls.store, 0, token=TOKEN, allow_legacy=cls.allow_legacy)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method="GET", token=None, origin=None, body=None):
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token is not None:
            req.add_header("X-AIW-Token", token)
        if origin is not None:
            req.add_header("Origin", origin)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())


class ServerAuthTests(ServerAuthTestBase):
    def test_health_open_without_token(self):
        status, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["app"], "ai-usage-widget")

    def test_update_with_token(self):
        status, body = self.request("/update", "POST", token=TOKEN, origin=EXT, body=demo_payload(NOW))
        self.assertEqual((status, body["auth"]), (200, "token"))
        self.assertEqual(self.store.extension_auth, "token")
        self.assertFalse(self.store.needs_extension_reload())

    def test_wrong_token_rejected(self):
        status, body = self.request("/update", "POST", token="00" * 32, origin=EXT, body=demo_payload(NOW))
        self.assertEqual((status, body["error"]), (401, "invalid token"))

    def test_other_local_program_rejected(self):
        # Origin도 토큰도 없는 로컬 프로그램(curl 등)은 가짜 사용량을 넣을 수 없다
        status, _ = self.request("/update", "POST", body=demo_payload(NOW))
        self.assertEqual(status, 401)
        status, _ = self.request("/refresh", "POST")
        self.assertEqual(status, 401)
        status, _ = self.request("/refresh", "POST", origin=EXT)
        self.assertEqual(status, 401)

    def test_refresh_with_token(self):
        status, body = self.request("/refresh", "POST", token=TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("refreshSeq", body)

    def test_legacy_extension_then_reloaded(self):
        status, body = self.request("/update", "POST", origin=EXT, body=demo_payload(NOW))
        self.assertEqual((status, body["auth"]), (200, "legacy"))
        self.assertTrue(self.store.needs_extension_reload())  # 위젯에 "확장 다시 로드" 안내
        # 내장 브라우저 모드에서는 확장을 쓰지 않으므로 안내하지 않는다
        self.store.set_web_source("browser")
        self.assertFalse(self.store.needs_extension_reload())
        self.store.set_web_source("extension")
        # 다시 로드된 확장이 토큰을 보내면 안내가 사라진다
        self.request("/update", "POST", token=TOKEN, origin=EXT, body=demo_payload(NOW))
        self.assertFalse(self.store.needs_extension_reload())

    def test_legacy_wait_command(self):
        status, _ = self.request("/wait-command?after=999999&timeout=0")
        self.assertEqual(status, 200)

    def test_preflight_allows_token_header(self):
        req = urllib.request.Request(f"{self.base}/update", method="OPTIONS")
        req.add_header("Origin", EXT)
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertIn("X-AIW-Token", r.headers["Access-Control-Allow-Headers"])
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], EXT)

    def test_unknown_post_path(self):
        status, _ = self.request("/nope", "POST", origin=EXT)
        self.assertEqual(status, 404)
        status, _ = self.request("/nope", "POST", origin="https://evil.example")
        self.assertEqual(status, 403)


class StrictServerAuthTests(ServerAuthTestBase):
    allow_legacy = False

    def test_tokenless_extension_rejected(self):
        status, body = self.request("/update", "POST", origin=EXT, body=demo_payload(NOW))
        self.assertEqual((status, body["error"]), (401, "token required"))
        status, _ = self.request("/wait-command?after=0&timeout=0", origin=EXT)
        self.assertEqual(status, 401)

    def test_rejection_readable_when_body_arrives_late(self):
        """헤더와 본문이 따로 도착해도 클라이언트가 거절 응답을 끝까지 읽을 수 있어야 한다.

        서버가 본문을 읽지 않고 연결을 닫으면 Windows에서 RST가 나가 응답을 읽기 전에 끊긴다.
        """
        body = json.dumps(demo_payload(NOW)).encode()
        head = (
            "POST /update HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Origin: {EXT}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        with socket.create_connection(self.server.server_address, timeout=5) as s:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(head)
            time.sleep(0.05)
            s.sendall(body)
            time.sleep(0.05)
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        status_line, _, _ = data.partition(b"\r\n")
        self.assertIn(b" 401 ", status_line)
        self.assertEqual(json.loads(data.split(b"\r\n\r\n", 1)[1])["error"], "token required")

    def test_token_still_works(self):
        status, _ = self.request("/wait-command?after=999999&timeout=0", token=TOKEN, origin=EXT)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
