"""로컬 Hermes / Claude Code OAuth 자격증명으로 Claude 사용량을 직접 조회한다.

외부 패키지 없이 Python 표준 라이브러리만 사용하며,
자격증명 파일은 읽기 전용으로만 접근한다 (토큰 갱신·덮어쓰기를 하지 않아
Hermes나 Claude Code의 기존 세션을 끊지 않는다).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state import ProviderState, UsageWindow, clamp_percent, parse_iso

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "claude-code/2.1.0"

DEFAULT_POLL_SEC = 180
MAX_BACKOFF_SEC = 900
MANUAL_COOLDOWN_SEC = 15

CLAUDE_LABELS = {
    "five_hour": "5시간",
    "seven_day": "주간",
    "seven_day_sonnet": "주간 Sonnet",
    "seven_day_opus": "주간 Opus",
    "extra_usage": "추가 사용량",
}
CLAUDE_ORDER = ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus", "extra_usage")


@dataclass(frozen=True)
class OAuthCredential:
    token: str
    source: str  # "hermes" | "claude_code" | "env" | "custom"
    label: str
    path: str | None = None
    expires_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= now + timedelta(seconds=30)

    @property
    def display_name(self) -> str:
        if self.source == "hermes":
            return "Claude (Hermes)"
        if self.source == "claude_code":
            return "Claude (CLI)"
        return "Claude (OAuth)"


def _parse_timestamp(value) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            ts = value / 1000.0 if value > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        return parse_iso(value.strip())
    return None


def _is_usable_oauth_token(token: str | None, auth_type: str | None = None) -> bool:
    if not token or not isinstance(token, str):
        return False
    tok = token.strip()
    if len(tok) < 12:
        return False
    # 일반 Anthropic API 키는 구독 사용량(/api/oauth/usage)을 조회할 수 없다.
    if tok.startswith("sk-ant-api"):
        return False
    if auth_type:
        low = str(auth_type).strip().lower()
        if low in ("api_key", "api-key", "apikey") and not tok.startswith("sk-ant-oat"):
            return False
    return True


def _extract_from_json(data: object, source: str, path: str | None) -> list[OAuthCredential]:
    """Hermes auth.json / Claude Code .credentials.json 등 다양한 구조에서 OAuth 토큰을 추출한다."""
    if not isinstance(data, dict):
        return []
    found: list[OAuthCredential] = []

    def add(token, label: str, expires=None, auth_type=None, src: str = source) -> None:
        if not _is_usable_oauth_token(token, auth_type=auth_type):
            return
        found.append(
            OAuthCredential(
                token=str(token).strip(),
                source=src,
                label=label or src,
                path=path,
                expires_at=_parse_timestamp(expires),
            )
        )

    # 1) Hermes credential_pool.anthropic[]
    pool = data.get("credential_pool")
    if isinstance(pool, dict):
        entries = pool.get("anthropic")
        if isinstance(entries, list):
            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                if entry.get("disabled") or entry.get("revoked"):
                    continue
                tok = entry.get("access_token") or entry.get("accessToken") or entry.get("token")
                add(
                    tok,
                    label=str(entry.get("label") or entry.get("id") or f"Hermes #{idx + 1}"),
                    expires=entry.get("expires_at") or entry.get("expiresAt"),
                    auth_type=entry.get("auth_type") or entry.get("type"),
                    src="hermes" if source in ("hermes", "custom") else source,
                )

    # 2) Hermes providers.anthropic 또는 최상위 anthropic 객체
    for container in (data.get("providers"), data):
        if not isinstance(container, dict):
            continue
        anth = container.get("anthropic")
        if isinstance(anth, dict):
            tok = anth.get("access_token") or anth.get("accessToken") or anth.get("token")
            add(
                tok,
                label=str(anth.get("label") or "Hermes"),
                expires=anth.get("expires_at") or anth.get("expiresAt"),
                auth_type=anth.get("auth_type") or anth.get("type"),
                src="hermes" if source in ("hermes", "custom") else source,
            )

    # 3) Claude Code ~/.claude/.credentials.json -> claudeAiOauth.accessToken
    claude_oauth = data.get("claudeAiOauth")
    if isinstance(claude_oauth, dict):
        add(
            claude_oauth.get("accessToken") or claude_oauth.get("access_token"),
            label="Claude Code",
            expires=claude_oauth.get("expiresAt") or claude_oauth.get("expires_at"),
            src="claude_code" if source in ("claude_code", "custom") else source,
        )

    # 4) 단순 파일 (.anthropic_oauth.json 등 최상위 access_token / accessToken)
    top_tok = data.get("access_token") or data.get("accessToken")
    if top_tok:
        add(
            top_tok,
            label=source,
            expires=data.get("expires_at") or data.get("expiresAt"),
            auth_type=data.get("auth_type"),
            src=source,
        )

    return found


def _candidate_credential_files(custom_path: str | None = None) -> list[tuple[Path, str]]:
    """읽어볼 로컬 자격증명 파일 목록을 우선순위대로 돌려준다."""
    candidates: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(p: Path, source: str) -> None:
        key = str(p)
        if key not in seen:
            seen.add(key)
            candidates.append((p, source))

    if custom_path and str(custom_path).strip():
        add(Path(str(custom_path).strip()).expanduser(), "custom")

    hermes_dirs: list[Path] = []
    if os.environ.get("HERMES_HOME"):
        hermes_dirs.append(Path(os.environ["HERMES_HOME"]).expanduser())
    if os.environ.get("LOCALAPPDATA"):
        hermes_dirs.append(Path(os.environ["LOCALAPPDATA"]) / "hermes")
    hermes_dirs.append(Path.home() / ".hermes")

    for d in hermes_dirs:
        add(d / "auth.json", "hermes")
        add(d / ".anthropic_oauth.json", "hermes")

    if os.environ.get("CLAUDE_CONFIG_DIR"):
        add(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() / ".credentials.json", "claude_code")
    add(Path.home() / ".claude" / ".credentials.json", "claude_code")

    return candidates


def discover_oauth_tokens(
    custom_path: str | None = None,
    now: datetime | None = None,
) -> list[OAuthCredential]:
    """로컬 Hermes / Claude Code / 환경변수에서 OAuth 토큰을 읽기 전용으로 찾는다."""
    now = now or datetime.now(timezone.utc)
    found: list[OAuthCredential] = []
    seen_tokens: set[str] = set()

    for path, source in _candidate_credential_files(custom_path):
        try:
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
        except (OSError, ValueError):
            continue
        for cred in _extract_from_json(data, source=source, path=str(path)):
            if cred.token not in seen_tokens:
                seen_tokens.add(cred.token)
                found.append(cred)

    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if _is_usable_oauth_token(env_tok) and env_tok.strip() not in seen_tokens:
        found.append(OAuthCredential(token=env_tok.strip(), source="env", label="CLAUDE_CODE_OAUTH_TOKEN"))

    # 만료되지 않은 토큰을 먼저 시도한다.
    found.sort(key=lambda c: c.is_expired(now))
    return found


def parse_oauth_usage(data: dict) -> list[UsageWindow]:
    """Anthropic /api/oauth/usage 응답을 위젯 UsageWindow 목록으로 변환한다."""
    if not isinstance(data, dict):
        return []
    windows_by_key: list[tuple[int, UsageWindow]] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if value.get("is_enabled") is False:
            continue
        used = clamp_percent(value.get("utilization"))
        if used is None:
            continue
        label = CLAUDE_LABELS.get(key, str(key)[:20])
        resets_at = parse_iso(value.get("resets_at"))
        order = CLAUDE_ORDER.index(key) if key in CLAUDE_ORDER else 99
        windows_by_key.append(
            (order, UsageWindow(label=label, used_percent=round(used, 1), resets_at=resets_at))
        )
    windows_by_key.sort(key=lambda item: item[0])
    return [w for _, w in windows_by_key]


def _request_usage(token: str, timeout: float = 10.0) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA_HEADER,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


class ClaudeOAuthFetcher:
    """로컬 Hermes / Claude Code OAuth 토큰으로 Claude 사용량을 주기적으로 조회한다."""

    def __init__(self) -> None:
        self._last_windows: list[UsageWindow] = []
        self._last_name: str = "Claude (OAuth)"
        self._last_ok_at: datetime | None = None
        self._last_attempt_at: datetime | None = None
        self._backoff_sec: int = DEFAULT_POLL_SEC
        self._backoff_until: datetime | None = None

    def _error_state(self, message: str, now: datetime, name: str | None = None) -> ProviderState:
        return ProviderState(
            provider="claude",
            name=name or self._last_name,
            ok=False,
            windows=list(self._last_windows),
            error=message,
            fetched_at=self._last_ok_at or now,
        )

    def fetch(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
        custom_path: str | None = None,
    ) -> ProviderState:
        now = now or datetime.now(timezone.utc)

        # 429 백오프 중이거나 수동 새로고침 직후(15초 이내)에는 캐시된 값을 돌려준다.
        if not force and self._backoff_until and now < self._backoff_until and self._last_windows:
            return self._error_state("요청 한도 초과 — 잠시 후 재시도", now)
        if (
            force
            and self._last_attempt_at is not None
            and (now - self._last_attempt_at).total_seconds() < MANUAL_COOLDOWN_SEC
            and self._last_windows
            and self._last_ok_at is not None
        ):
            return ProviderState(
                provider="claude",
                name=self._last_name,
                ok=True,
                windows=list(self._last_windows),
                fetched_at=self._last_ok_at,
            )

        self._last_attempt_at = now
        creds = discover_oauth_tokens(custom_path=custom_path, now=now)
        if not creds:
            return self._error_state("로컬 OAuth 토큰 없음 (Hermes/Claude Code 확인)", now)

        last_err = "Claude OAuth 사용량을 읽지 못했어요"
        last_name = creds[0].display_name

        for cred in creds:
            last_name = cred.display_name
            try:
                data = _request_usage(cred.token)
                windows = parse_oauth_usage(data)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    last_err = "OAuth 토큰 만료 — Hermes/Claude Code 실행 필요"
                    continue
                if e.code == 429:
                    self._backoff_sec = min(MAX_BACKOFF_SEC, max(DEFAULT_POLL_SEC * 2, self._backoff_sec * 2))
                    self._backoff_until = now + timedelta(seconds=self._backoff_sec)
                    return self._error_state("요청 한도 초과 — 잠시 후 재시도", now, name=last_name)
                last_err = f"Claude OAuth 오류 (HTTP {e.code})"
                continue
            except (OSError, ValueError, urllib.error.URLError):
                last_err = "Claude OAuth 서버에 연결하지 못했어요"
                continue

            if not windows:
                last_err = "Claude OAuth 응답에 사용량 항목이 없어요"
                continue

            self._last_windows = windows
            self._last_name = cred.display_name
            self._last_ok_at = now
            self._backoff_sec = DEFAULT_POLL_SEC
            self._backoff_until = None
            return ProviderState(
                provider="claude",
                name=cred.display_name,
                ok=True,
                windows=windows,
                fetched_at=now,
            )

        return self._error_state(last_err, now, name=last_name)


def demo_claude_oauth_state(now: datetime | None = None) -> ProviderState:
    """--demo 모드용 Claude (Hermes/OAuth) 예시 데이터."""
    now = now or datetime.now(timezone.utc)
    return ProviderState(
        provider="claude",
        name="Claude (Hermes)",
        ok=True,
        windows=[
            UsageWindow("5시간", 18.0, resets_at=now + timedelta(hours=4, minutes=12)),
            UsageWindow("주간", 36.0, resets_at=now + timedelta(days=5, hours=1)),
        ],
        fetched_at=now,
    )
