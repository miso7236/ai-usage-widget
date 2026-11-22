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


_EXHAUSTED_HINTS = ("hit your", "exhausted", "reached", "소진")
SHORT_RESET_MAX_HOURS = 5.2


def classify_bucket_label(
    bucket_id: str,
    display_name: str,
    window: str | None = None,
    resets_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """Antigravity quota 버킷을 위젯 표준 라벨('5시간', '주간', 'Gemini')로 정규화한다.

    명시적 주기(window) → 버킷 ID/이름 → 리셋까지 남은 시간 순으로 판단한다.
    """
    win = str(window or "").strip().lower()
    if win:
        if any(k in win for k in ("week", "7d", "7-day", "7 day")):
            return "주간"
        if any(k in win for k in ("5h", "5-hour", "5 hour", "five", "session", "short", "hour")):
            return "5시간"
    combined = " ".join(x for x in (bucket_id, display_name) if x).lower()
    if any(k in combined for k in ("week", "7d", "7-day", "7 day")):
        return "주간"
    if any(k in combined for k in ("5h", "5-hour", "5 hour", "five", "session", "short")):
        return "5시간"
    if resets_at is not None:
        ref = now or datetime.now(timezone.utc)
        hours = (resets_at - ref).total_seconds() / 3600.0
        if 0 < hours <= SHORT_RESET_MAX_HOURS:
            return "5시간"
        if hours > 24:
            return "주간"
    return "Gemini"


def _window_sort_key(w: UsageWindow) -> tuple[int, str]:
    order = {"5시간": 0, "주간": 1, "Gemini": 2}
    return (order.get(w.label, 9), w.label)


def _to_fraction(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        # protobuf oneof JSON: {"case": "remainingFraction", "value": 0.4}
        if "value" in value and str(value.get("case") or "remainingFraction") == "remainingFraction":
            return _to_fraction(value.get("value"))
        return _to_fraction(value.get("remainingFraction"))
    try:
        frac = float(value)
    except (TypeError, ValueError):
        return None
    if frac != frac:  # NaN
        return None
    return max(0.0, min(1.0, frac))


def _bucket_remaining_fraction(bucket: dict) -> float | None:
    """버킷의 남은 비율(0~1). proto3 JSON은 0을 생략하므로 소진 상태도 0으로 복원한다."""
    frac = _to_fraction(bucket.get("remainingFraction"))
    if frac is not None:
        return frac
    remaining = bucket.get("remaining")
    frac = _to_fraction(remaining)
    if frac is not None:
        return frac
    description = str(bucket.get("description") or "").lower()
    has_reset = bucket.get("resetTime") not in (None, "")
    if has_reset and (
        isinstance(remaining, dict) or any(h in description for h in _EXHAUSTED_HINTS)
    ):
        # remaining 객체는 있는데 값이 비었거나, 설명이 '한도 도달'이면 0(전부 사용)으로 본다.
        return 0.0
    return None


def _merge_window(windows: list[UsageWindow], new: UsageWindow) -> None:
    """같은 라벨이 이미 있으면 더 많이 사용한 쪽만 남긴다."""
    for idx, existing in enumerate(windows):
        if existing.label == new.label:
            if new.used_percent > existing.used_percent:
                windows[idx] = new
            return
    windows.append(new)


def _iter_summary_buckets(payload: dict):
    """(그룹 이름, 버킷) 쌍을 돌려준다. 그룹 없이 buckets만 오는 응답도 지원한다."""
    groups = payload.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("displayName") or group.get("name") or "").strip()
            buckets = group.get("buckets")
            if isinstance(buckets, list):
                for bucket in buckets:
                    if isinstance(bucket, dict):
                        yield group_name, bucket
    buckets = payload.get("buckets")
    if isinstance(buckets, list):
        for bucket in buckets:
            if isinstance(bucket, dict):
                yield "", bucket


def parse_quota_summary(data: dict, now: datetime | None = None) -> list[UsageWindow]:
    """RetrieveUserQuotaSummary 응답에서 기본 Gemini 윈도우(5시간/주간)를 추출한다.

    remainingFraction은 '남은' 비율이므로 위젯에는 사용률(1 - 남은 비율)로 표시한다.
    """
    if not isinstance(data, dict):
        return []
    if not _is_ok_code(data.get("code")):
        raise AntigravityError(str(data.get("message") or f"code={data.get('code')}"))

    payload = data.get("response") or data.get("summary") or data
    if not isinstance(payload, dict):
        return []

    windows: list[UsageWindow] = []
    for group_name, bucket in _iter_summary_buckets(payload):
        if bucket.get("disabled"):
            continue
        bucket_id = str(bucket.get("bucketId") or bucket.get("id") or "")
        bucket_name = str(bucket.get("displayName") or bucket.get("name") or "")
        if group_name and any(bad in group_name.lower() for bad in EXCLUDED_MODEL_KEYWORDS):
            continue
        if any(bad in bucket_id.lower() for bad in EXCLUDED_MODEL_KEYWORDS):
            continue
        if not (is_default_gemini_name(group_name) or is_default_gemini_name(f"{bucket_id} {bucket_name}")):
            continue
        frac = _bucket_remaining_fraction(bucket)
        if frac is None:
            continue
        used = clamp_percent((1.0 - frac) * 100.0)
        if used is None:
            continue
        resets_at = _parse_reset_time(bucket.get("resetTime"))
        reset_text = str(bucket.get("description") or "").strip() or None
        label = classify_bucket_label(
            bucket_id, bucket_name, bucket.get("window"), resets_at=resets_at, now=now
        )
        _merge_window(
            windows,
            UsageWindow(label=label, used_percent=round(used, 1), resets_at=resets_at, reset_text=reset_text),
        )
    windows.sort(key=_window_sort_key)
    return windows


def parse_model_configs(data: dict, now: datetime | None = None) -> list[UsageWindow]:
    """GetUserStatus / GetCommandModelConfigs 응답에서 기본 Gemini 용량을 추출한다.

    모델별 quotaInfo를 리셋 주기(5시간/주간)로 묶고, 주기마다 가장 많이 사용한 값을 쓴다.
    """
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

    windows: list[UsageWindow] = []
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
        frac = _bucket_remaining_fraction(quota)
        if frac is None:
            continue
        used = clamp_percent((1.0 - frac) * 100.0)
        if used is None:
            continue
        resets_at = _parse_reset_time(quota.get("resetTime"))
        win_label = classify_bucket_label("", "", quota.get("window"), resets_at=resets_at, now=now)
        _merge_window(windows, UsageWindow(label=win_label, used_percent=round(used, 1), resets_at=resets_at))

    windows.sort(key=_window_sort_key)
    return windows


# ---------------- 프로세스 & 포트 탐색 ----------------
_TOKEN_RE = re.compile(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""")
_FLAG_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


def parse_process_args(cmdline: str) -> dict[str, str]:
    """프로세스 명령줄에서 --csrf_token, --extension_server_port 등의 플래그 값을 추출한다.

    값 없는 불리언 플래그(--random_port, --enable_lsp 등) 뒤에 이어지는
    --csrf_token을 삼키지 않도록 토큰 단위로 분리해 파싱한다.
    """
    if not cmdline:
        return {}
    tokens = _TOKEN_RE.findall(cmdline)
    result: dict[str, str] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.startswith("--"):
            body = tok[2:]
        elif tok.startswith("-") and len(tok) > 2:
            body = tok[1:]
        else:
            i += 1
            continue

        if "=" in body:
            raw_key, raw_val = body.split("=", 1)
            key = raw_key.lower().replace("-", "_")
            val = _unquote(raw_val)
            if _FLAG_NAME_RE.match(raw_key) and val:
                result[key] = val
            i += 1
            continue

        if not _FLAG_NAME_RE.match(body):
            i += 1
            continue

        key = body.lower().replace("-", "_")
        if i + 1 < n and not tokens[i + 1].startswith("-"):
            val = _unquote(tokens[i + 1])
            if val:
                result[key] = val
            i += 2
        else:
            # 값 없는 불리언 플래그: 다음 토큰(--csrf_token 등)을 소비하지 않고 넘어간다.
            i += 1
    return result


def is_antigravity_process(name: str, cmdline: str) -> tuple[bool, bool]:
    """(Antigravity 프로세스 여부, CLI 여부)를 반환한다."""
    low_name = (name or "").strip().lower()
    low_cmd = (cmdline or "").lower()
    if low_name in ("agy", "agy.exe", "antigravity-cli", "antigravity-cli.exe", "antigravity_cli", "antigravity_cli.exe"):
        return True, True
    if (
        "language_server" in low_name
        or "language-server" in low_name
        or "language_server" in low_cmd
        or "language-server" in low_cmd
    ):
        if (
            "antigravity" in low_cmd
            or "\\gemini.app\\" in low_cmd
            or "/gemini.app/" in low_cmd
            or "jetski" in low_cmd
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
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match 'language[-_]server|agy|antigravity' "
            "-or ($_.CommandLine -and $_.CommandLine -match 'language[-_]server|csrf_token|antigravity[-_]cli') } | "
            "Select-Object ProcessId,Name,CommandLine | "
            "ConvertTo-Json -Compress"
        ),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
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
            errors="replace",
            timeout=5,
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
_LOOPBACK_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOOPBACK_HTTPS_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_INSECURE_SSL_CTX),
)


def _post_rpc(endpoint: LocalEndpoint, path: str, timeout: float = 2.5) -> dict:
    url = f"{endpoint.scheme}://127.0.0.1:{endpoint.port}{path}"
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if endpoint.csrf_token:
        headers["X-Codeium-Csrf-Token"] = endpoint.csrf_token
    req = urllib.request.Request(url, data=RPC_BODY, headers=headers, method="POST")
    opener = _LOOPBACK_HTTPS_OPENER if endpoint.scheme == "https" else _LOOPBACK_HTTP_OPENER
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _sanitize_for_debug(path: str, data) -> object:
    """진단 파일용으로 용량 관련 필드만 남긴다 (이메일·토큰 등 계정 정보 제외)."""
    if not isinstance(data, dict):
        return type(data).__name__
    if path.endswith("RetrieveUserQuotaSummary"):
        payload = data.get("response") or data.get("summary") or data
        if isinstance(payload, dict):
            return {k: payload.get(k) for k in ("groups", "buckets") if k in payload}
        return None
    configs = None
    user_status = data.get("userStatus")
    if isinstance(user_status, dict) and isinstance(user_status.get("cascadeModelConfigData"), dict):
        configs = user_status["cascadeModelConfigData"].get("clientModelConfigs")
    if configs is None:
        configs = data.get("clientModelConfigs")
    if not isinstance(configs, list):
        return {"keys": sorted(data.keys())[:20]}
    out = []
    for item in configs:
        if isinstance(item, dict):
            alias = item.get("modelOrAlias")
            out.append(
                {
                    "label": item.get("label"),
                    "model": alias.get("model") if isinstance(alias, dict) else None,
                    "quotaInfo": item.get("quotaInfo"),
                }
            )
    return {"clientModelConfigs": out}


def debug_path():
    from state import config_path

    return config_path().parent / "antigravity_debug.json"


def _write_debug(endpoint: LocalEndpoint, now: datetime, attempts: list[dict], windows: list[UsageWindow]) -> None:
    try:
        record = {
            "at": now.isoformat(),
            "endpoint": f"{endpoint.scheme}://127.0.0.1:{endpoint.port}",
            "attempts": attempts,
            "windows": [
                {
                    "label": w.label,
                    "usedPercent": w.used_percent,
                    "resetsAt": w.resets_at.isoformat() if w.resets_at else None,
                    "resetText": w.reset_text,
                }
                for w in windows
            ],
        }
        p = debug_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:  # 진단 기록 실패는 위젯 동작에 영향을 주지 않는다.
        pass


def _write_discovery_debug(now: datetime, discovery: list[dict]) -> None:
    """엔드포인트를 찾지 못했을 때 토큰 값 없이 프로세스·플래그 키·포트 수만 진단 기록한다."""
    try:
        record = {
            "at": now.isoformat(),
            "endpoint": None,
            "discovery": discovery,
            "windows": [],
        }
        p = debug_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _fetch_from_endpoint(endpoint: LocalEndpoint, now: datetime) -> list[UsageWindow]:
    attempts: list[dict] = []
    result: list[UsageWindow] = []
    parsers = [(RPC_PATHS[0], parse_quota_summary)] + [(p, parse_model_configs) for p in RPC_PATHS[1:]]
    # 1) RetrieveUserQuotaSummary (5시간 + 주간 그룹 요약) → 2) GetUserStatus / GetCommandModelConfigs 폴백
    for path, parser in parsers:
        name = path.rsplit("/", 1)[-1]
        try:
            data = _post_rpc(endpoint, path)
        except (OSError, ValueError, urllib.error.URLError) as e:
            attempts.append({"rpc": name, "error": type(e).__name__ + ": " + str(e)[:120]})
            continue
        try:
            windows = parser(data, now=now)
        except AntigravityError as e:
            attempts.append({"rpc": name, "error": str(e)[:120], "raw": _sanitize_for_debug(path, data)})
            continue
        attempts.append({"rpc": name, "parsed": len(windows), "raw": _sanitize_for_debug(path, data)})
        if windows:
            result = windows
            break
    if any("raw" in a for a in attempts):
        _write_debug(endpoint, now, attempts, result)
    return result


class AntigravityFetcher:
    """실행 중인 Antigravity에서 기본 Gemini 사용량을 주기적으로 조회한다."""

    def __init__(self) -> None:
        self._cached_endpoint: LocalEndpoint | None = None
        self._last_windows: list[UsageWindow] = []
        self._last_ok_at: datetime | None = None
        self._last_discovery: list[dict] = []

    def _discover_endpoints(self) -> list[LocalEndpoint]:
        if sys.platform == "win32":
            procs = _discover_processes_windows()
            port_fn = _discover_listening_ports_windows
        else:
            procs = _discover_processes_posix()
            port_fn = _discover_listening_ports_posix

        endpoints: list[LocalEndpoint] = []
        seen: set[LocalEndpoint] = set()
        discovery_log: list[dict] = []
        for pid, name, cmdline in procs:
            matched, is_cli = is_antigravity_process(name, cmdline)
            if not matched:
                if "language" in name.lower() or "antigravity" in name.lower() or "agy" in name.lower():
                    discovery_log.append({"name": name, "matched": False, "hasCmdline": bool(cmdline.strip())})
                continue
            args = parse_process_args(cmdline)
            ports = port_fn(pid)
            eps = build_candidate_endpoints(ports, args, is_cli=is_cli)
            discovery_log.append(
                {
                    "name": name,
                    "matched": True,
                    "isCli": is_cli,
                    "argKeys": sorted(args.keys()),
                    "portCount": len(ports),
                    "endpointCount": len(eps),
                }
            )
            for ep in eps:
                if ep not in seen:
                    seen.add(ep)
                    endpoints.append(ep)
        self._last_discovery = discovery_log
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

        if not endpoints:
            _write_discovery_debug(now, self._last_discovery)
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
