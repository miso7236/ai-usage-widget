"""Google Antigravity 로컬 언어 서버에서 기본 Gemini 용량(사용률·리셋 시간)을 읽어온다.

외부 패키지 없이 Python 표준 라이브러리만 사용하며,
실행 중인 Antigravity(앱 / IDE / agy CLI)의 로컬 loopback RPC만 조회한다.
기본 Gemini 용량만 표시하고 Argon·Opus·Claude·GPT 등 다른 모델은 제외한다.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from state import ProviderState, UsageWindow, clamp_percent, parse_iso

RPC_PATHS = (
    "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
    "/exa.language_server_pb.LanguageServerService/GetUserStatus",
    "/exa.language_server_pb.LanguageServerService/GetCommandModelConfigs",
)
RPC_BODY = json.dumps(
    {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}
).encode("utf-8")

EXCLUDED_MODEL_KEYWORDS = (
    "argon",
    "opus",
    "claude",
    "sonnet",
    "gpt",
    "openai",
    "imagen",
    "veo",
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class AntigravityError(Exception):
    pass


@dataclass(frozen=True)
class LocalEndpoint:
    scheme: str
    port: int
    csrf_token: str = ""


def is_default_gemini_name(text: str | None) -> bool:
    """기본 Gemini 모델/그룹 여부 (Argon, Opus, Claude, GPT 등은 제외)."""
    if not text or not isinstance(text, str):
        return False
    low = text.lower()
    if "gemini" not in low:
        return False
    return not any(bad in low for bad in EXCLUDED_MODEL_KEYWORDS)


def _parse_reset_time(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        try:
            # 밀리초 단위 epoch 대응
            ts = value / 1000.0 if value > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        return parse_iso(value.strip())
    return None


def _is_ok_code(code) -> bool:
    if code is None:
        return True
    if isinstance(code, int):
        return code == 0
    if isinstance(code, str):
        return code.strip().lower() in ("", "0", "ok", "success")
    return True


def classify_bucket_label(
    bucket_id: str,
    display_name: str,
    window: str | None = None,
    resets_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Antigravity quota 버킷을 위젯 표준 라벨('5시간', '주간', 'Gemini')로 정규화한다."""
    combined = " ".join(x for x in (bucket_id, display_name, window or "") if x).lower()
    if any(k in combined for k in ("5h", "5-hour", "5 hour", "five", "session", "short")):
        return "5시간"
    if any(k in combined for k in ("week", "7d", "7-day", "7 day")):
        return "주간"
    if resets_at is not None:
        ref = now or datetime.now(timezone.utc)
        hours = (resets_at - ref).total_seconds() / 3600.0
        if 0 < hours <= 12:
            return "5시간"
        if hours > 24:
            return "주간"
    return "Gemini"


def _window_sort_key(w: UsageWindow) -> tuple[int, str]:
    order = {"5시간": 0, "주간": 1, "Gemini": 2}
    return (order.get(w.label, 9), w.label)


def parse_quota_summary(data: dict, now: datetime | None = None) -> list[UsageWindow]:
    """RetrieveUserQuotaSummary 응답에서 기본 Gemini 윈도우(5시간/주간)를 추출한다."""
    if not isinstance(data, dict):
        return []
    if not _is_ok_code(data.get("code")):
        raise AntigravityError(str(data.get("message") or f"code={data.get('code')}"))

    payload = data.get("response") or data.get("summary") or data
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return []

    windows: list[UsageWindow] = []
    seen_labels: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("displayName") or group.get("name") or "").strip()
        if not is_default_gemini_name(group_name):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            raw_frac = bucket.get("remainingFraction")
            if raw_frac is None and isinstance(bucket.get("remaining"), dict):
                raw_frac = bucket["remaining"].get("remainingFraction")
            if raw_frac is None:
                continue
            try:
                frac = max(0.0, min(1.0, float(raw_frac)))
            except (TypeError, ValueError):
                continue
            used = clamp_percent((1.0 - frac) * 100.0)
            if used is None:
                continue
            resets_at = _parse_reset_time(bucket.get("resetTime"))
            reset_text = str(bucket.get("description") or "").strip() or None
            label = classify_bucket_label(
                str(bucket.get("bucketId") or bucket.get("id") or ""),
                str(bucket.get("displayName") or bucket.get("name") or ""),
                bucket.get("window"),
                resets_at=resets_at,
                now=now,
            )
            if label in seen_labels:
                # 같은 라벨 버킷이 중복되면 더 많이 사용한 쪽을 유지한다.
                for idx, existing in enumerate(windows):
                    if existing.label == label and used > existing.used_percent:
                        windows[idx] = UsageWindow(
                            label=label,
                            used_percent=round(used, 1),
                            resets_at=resets_at,
                            reset_text=reset_text,
                        )
                continue
            seen_labels.add(label)
            windows.append(
                UsageWindow(
                    label=label,
                    used_percent=round(used, 1),
                    resets_at=resets_at,
                    reset_text=reset_text,
                )
            )
    windows.sort(key=_window_sort_key)
    return windows


def parse_model_configs(data: dict, now: datetime | None = None) -> list[UsageWindow]:
    """GetUserStatus / GetCommandModelConfigs 응답에서 기본 Gemini 대표 용량을 추출한다."""
    if not isinstance(data, dict):
        return []
    if not _is_ok_code(data.get("code")):
        raise AntigravityError(str(data.get("message") or f"code={data.get('code')}"))

    configs = None
    user_status = data.get("userStatus")
    if isinstance(user_status, dict):
        cascade = user_status.get("cascadeModelConfigData")
        if isinstance(cascade, dict):
            configs = cascade.get("clientModelConfigs")
    if configs is None:
        configs = data.get("clientModelConfigs")
    if not isinstance(configs, list):
        return []

    candidates: list[tuple[float, int, datetime | None, str | None]] = []
    for item in configs:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        alias = item.get("modelOrAlias")
        model_id = str(alias.get("model") or "") if isinstance(alias, dict) else ""
        combined_name = f"{label} {model_id}".strip()
        if not is_default_gemini_name(combined_name):
            continue
        quota = item.get("quotaInfo")
        if not isinstance(quota, dict):
            continue
        raw_frac = quota.get("remainingFraction")
        if raw_frac is None:
            continue
        try:
            frac = max(0.0, min(1.0, float(raw_frac)))
        except (TypeError, ValueError):
            continue
        used = clamp_percent((1.0 - frac) * 100.0)
        if used is None:
            continue
        resets_at = _parse_reset_time(quota.get("resetTime"))
        # 동률일 때 Pro > Flash 순으로 대표 모델 선택
        pro_rank = 1 if "pro" in combined_name.lower() else 0
        candidates.append((round(used, 1), pro_rank, resets_at, None))

    if not candidates:
        return []

    # 가장 사용률이 높은 기본 Gemini 풀을 대표값으로 표시
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    used, _rank, resets_at, reset_text = candidates[0]
    win_label = classify_bucket_label("", "", None, resets_at=resets_at, now=now)
    return [
        UsageWindow(
            label=win_label,
            used_percent=used,
            resets_at=resets_at,
            reset_text=reset_text,
        )
    ]


# ---------------- 프로세스 & 포트 탐색 ----------------
_FLAG_RE = re.compile(
    r"""--([a-zA-Z0-9_-]+)(?:=|\s+)(?:"([^"]+)"|'([^']+)'|(\S+))"""
)


def parse_process_args(cmdline: str) -> dict[str, str]:
    """프로세스 명령줄에서 --csrf_token, --extension_server_port 등의 플래그 값을 추출한다."""
    if not cmdline:
        return {}
    result: dict[str, str] = {}
    for m in _FLAG_RE.finditer(cmdline):
        key = m.group(1).lower().replace("-", "_")
        val = m.group(2) or m.group(3) or m.group(4) or ""
        if val and not val.startswith("--"):
            result[key] = val
    return result


def is_antigravity_process(name: str, cmdline: str) -> tuple[bool, bool]:
    """(Antigravity 프로세스 여부, CLI 여부)를 반환한다."""
    low_name = (name or "").strip().lower()
    low_cmd = (cmdline or "").lower()
    if low_name in ("agy", "agy.exe"):
        return True, True
    if "language_server" in low_name or "language_server" in low_cmd:
        if (
            "antigravity" in low_cmd
            or "\\gemini.app\\" in low_cmd
            or "/gemini.app/" in low_cmd
        ):
            return True, False
    return False, False


def _parse_port(value: str | None) -> int | None:
    if not value:
        return None
    try:
        p = int(value)
    except (TypeError, ValueError):
        return None
    return p if 1 <= p <= 65535 else None


def _extract_port_from_addr(addr: str) -> int | None:
    if not addr or ":" not in addr:
        return None
    port_str = addr.rsplit(":", 1)[-1].strip()
    return _parse_port(port_str)


def parse_netstat_ports(output: str, pid: int) -> list[int]:
    """Windows `netstat -ano -p TCP` 출력에서 지정 PID의 LISTENING 포트를 추출한다."""
    ports: list[int] = []
    seen: set[int] = set()
    pid_str = str(pid)
    for line in (output or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[-1] != pid_str:
            continue
        proto = parts[0].upper()
        if not proto.startswith("TCP"):
            continue
        local_addr = parts[1]
        foreign_addr = parts[2]
        state = parts[3].upper() if len(parts) >= 5 else ""
        # 언어 설정에 무관하게 LISTENING 상태 또는 foreign addr *:0 / [::]:0 을 허용
        is_listening = (
            state == "LISTENING"
            or foreign_addr.endswith(":0")
            or foreign_addr in ("*:*", "0.0.0.0:0", "[::]:0")
        )
        if not is_listening:
            continue
        port = _extract_port_from_addr(local_addr)
        if port and port not in seen:
            seen.add(port)
            ports.append(port)
    return ports


def _discover_processes_windows() -> list[tuple[int, str, str]]:
    cmd = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match 'language_server|agy' } | "
            "Select-Object ProcessId,Name,CommandLine | "
            "ConvertTo-Json -Compress"
        ),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    raw = (proc.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    result: list[tuple[int, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("ProcessId")
        if not isinstance(pid, int):
            continue
        result.append((pid, str(row.get("Name") or ""), str(row.get("CommandLine") or "")))
    return result


def _discover_listening_ports_windows(pid: int) -> list[int]:
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_netstat_ports(proc.stdout or "", pid)


def _discover_processes_posix() -> list[tuple[int, str, str]]:
    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    result: list[tuple[int, str, str]] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        result.append((pid, parts[1], parts[2]))
    return result


def _discover_listening_ports_posix(pid: int) -> list[int]:
    try:
        proc = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        ports: list[int] = []
        seen: set[int] = set()
        for line in (proc.stdout or "").splitlines()[1:]:
            for tok in reversed(line.split()):
                if ":" in tok:
                    cleaned = tok.split("->", 1)[0]
                    port = _extract_port_from_addr(cleaned)
                    if port and port not in seen:
                        seen.add(port)
                        ports.append(port)
                    break
        return ports
    except (OSError, subprocess.SubprocessError):
        return []


def build_candidate_endpoints(
    listening_ports: list[int],
    args: dict[str, str],
    is_cli: bool = False,
) -> list[LocalEndpoint]:
    """발견된 포트와 CLI 인자에서 시도할 loopback 엔드포인트 후보 목록을 만든다."""
    csrf = args.get("csrf_token", "")
    ext_csrf = args.get("extension_server_csrf_token", "")
    ext_port = _parse_port(args.get("extension_server_port"))
    https_port = _parse_port(args.get("https_server_port"))
    http_port = _parse_port(args.get("http_server_port"))

    if not is_cli and not csrf and not ext_csrf:
        return []

    endpoints: list[LocalEndpoint] = []
    seen: set[LocalEndpoint] = set()

    def add(scheme: str, port: int | None, token: str) -> None:
        if not port:
            return
        ep = LocalEndpoint(scheme=scheme, port=port, csrf_token=token)
        if ep not in seen:
            seen.add(ep)
            endpoints.append(ep)

    if https_port:
        add("https", https_port, csrf)
    for p in listening_ports:
        add("https", p, csrf)
    for p in listening_ports:
        add("http", p, csrf)
    if http_port:
        add("http", http_port, csrf)
    if ext_port:
        if ext_csrf:
            add("http", ext_port, ext_csrf)
        if csrf and csrf != ext_csrf:
            add("http", ext_port, csrf)
    return endpoints


# ---------------- HTTP RPC 호출 & Fetcher ----------------
_INSECURE_SSL_CTX = ssl._create_unverified_context()


def _post_rpc(endpoint: LocalEndpoint, path: str, timeout: float = 2.5) -> dict:
    url = f"{endpoint.scheme}://127.0.0.1:{endpoint.port}{path}"
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if endpoint.csrf_token:
        headers["X-Codeium-Csrf-Token"] = endpoint.csrf_token
    req = urllib.request.Request(url, data=RPC_BODY, headers=headers, method="POST")
    ctx = _INSECURE_SSL_CTX if endpoint.scheme == "https" else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _fetch_from_endpoint(endpoint: LocalEndpoint, now: datetime) -> list[UsageWindow]:
    # 1) RetrieveUserQuotaSummary (5시간 + 주간 그룹 요약)
    try:
        data = _post_rpc(endpoint, RPC_PATHS[0])
        windows = parse_quota_summary(data, now=now)
        if windows:
            return windows
    except (OSError, ValueError, urllib.error.URLError, AntigravityError):
        pass

    # 2) GetUserStatus / GetCommandModelConfigs 폴백
    for path in RPC_PATHS[1:]:
        try:
            data = _post_rpc(endpoint, path)
            windows = parse_model_configs(data, now=now)
            if windows:
                return windows
        except (OSError, ValueError, urllib.error.URLError, AntigravityError):
            continue
    return []


class AntigravityFetcher:
    """실행 중인 Antigravity에서 기본 Gemini 사용량을 주기적으로 조회한다."""

    def __init__(self) -> None:
        self._cached_endpoint: LocalEndpoint | None = None
        self._last_windows: list[UsageWindow] = []
        self._last_ok_at: datetime | None = None

    def _discover_endpoints(self) -> list[LocalEndpoint]:
        if sys.platform == "win32":
            procs = _discover_processes_windows()
            port_fn = _discover_listening_ports_windows
        else:
            procs = _discover_processes_posix()
            port_fn = _discover_listening_ports_posix

        endpoints: list[LocalEndpoint] = []
        seen: set[LocalEndpoint] = set()
        for pid, name, cmdline in procs:
            matched, is_cli = is_antigravity_process(name, cmdline)
            if not matched:
                continue
            args = parse_process_args(cmdline)
            ports = port_fn(pid)
            for ep in build_candidate_endpoints(ports, args, is_cli=is_cli):
                if ep not in seen:
                    seen.add(ep)
                    endpoints.append(ep)
        return endpoints

    def fetch(self, now: datetime | None = None) -> ProviderState:
        now = now or datetime.now(timezone.utc)

        # 캐시된 엔드포인트 먼저 빠르게 시도 (프로세스 탐색 생략)
        if self._cached_endpoint is not None:
            windows = _fetch_from_endpoint(self._cached_endpoint, now)
            if windows:
                self._last_windows = windows
                self._last_ok_at = now
                return ProviderState(
                    provider="antigravity",
                    name="Antigravity",
                    ok=True,
                    windows=windows,
                    fetched_at=now,
                )
            self._cached_endpoint = None

        endpoints = self._discover_endpoints()
        for ep in endpoints:
            windows = _fetch_from_endpoint(ep, now)
            if windows:
                self._cached_endpoint = ep
                self._last_windows = windows
                self._last_ok_at = now
                return ProviderState(
                    provider="antigravity",
                    name="Antigravity",
                    ok=True,
                    windows=windows,
                    fetched_at=now,
                )

        err = "Antigravity가 실행 중이 아니에요" if not endpoints else "Antigravity Gemini 용량을 읽지 못했어요"
        return ProviderState(
            provider="antigravity",
            name="Antigravity",
            ok=False,
            windows=list(self._last_windows),
            error=err,
            fetched_at=self._last_ok_at or now,
        )


def demo_antigravity_state(now: datetime | None = None) -> ProviderState:
    """--demo 모드용 Antigravity 예시 데이터."""
    now = now or datetime.now(timezone.utc)
    return ProviderState(
        provider="antigravity",
        name="Antigravity",
        ok=True,
        windows=[
            UsageWindow("5시간", 28.0, resets_at=now + timedelta(hours=3, minutes=40)),
            UsageWindow("주간", 54.0, resets_at=now + timedelta(days=4, hours=2)),
        ],
        fetched_at=now,
    )
