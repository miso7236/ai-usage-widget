"""위젯 ↔ 크롬 확장 사이의 로컬 토큰.

위젯이 처음 켜질 때 무작위 토큰을 만들어 %APPDATA%\\AIUsageWidget\\local-token.txt 에 저장하고,
크롬 확장 폴더에 local-token.json 으로 복사해 둔다. 확장은 자기 폴더의 이 파일을 읽어
위젯에 보내는 요청마다 X-AIW-Token 헤더로 붙이고, 로컬 서버는 토큰이 맞는 요청만 받는다.

다른 크롬 확장이나 웹페이지는 이 확장 폴더의 파일을 읽을 수 없으므로 토큰을 알 수 없다.
같은 사용자 계정으로 도는 프로그램은 파일을 직접 읽을 수 있어서 막지 못한다.
tkinter에 의존하지 않아서 테스트할 수 있다.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path

TOKEN_HEADER = "X-AIW-Token"
EXTENSION_TOKEN_FILE = "local-token.json"
_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

_lock = threading.Lock()
_cached: str | None = None


def token_path() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) / "AIUsageWidget" if base else Path.home() / ".ai-usage-widget"
    return root / "local-token.txt"


def is_valid_token(value) -> bool:
    return isinstance(value, str) and bool(_TOKEN_RE.match(value))


def new_token() -> str:
    return secrets.token_hex(32)  # 256비트


def load_or_create_token(path: Path | None = None) -> str:
    """저장된 토큰을 읽고, 없거나 깨졌으면 새로 만들어 저장한다.

    저장에 실패해도 만든 토큰을 돌려준다 (이번 실행 동안만 쓰인다).
    """
    path = path or token_path()
    try:
        saved = path.read_text(encoding="utf-8").strip()
        if is_valid_token(saved):
            return saved
    except OSError:
        pass
    token = new_token()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return token


def get_token() -> str:
    """이번 실행에서 쓰는 토큰 (처음 한 번만 파일을 읽거나 만든다)."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = load_or_create_token()
        return _cached


def write_extension_token(folder: Path, token: str) -> bool:
    """크롬 확장 폴더에 local-token.json을 쓴다. 내용이 같으면 그대로 둔다.

    manifest.json이 없는 폴더(확장이 아님)에는 쓰지 않는다. 파일이 준비됐으면 True.
    """
    if not is_valid_token(token) or not (folder / "manifest.json").is_file():
        return False
    target = folder / EXTENSION_TOKEN_FILE
    data = json.dumps({"token": token, "header": TOKEN_HEADER}, indent=2) + "\n"
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == data:
            return True
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
        return True
    except OSError:
        return False


def token_matches(expected: str | None, given: str | None) -> bool:
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))
