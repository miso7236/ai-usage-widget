"""파일 로그와 진단 정보 보고서.

비공식 API가 바뀌어 조회가 실패했을 때 원인을 바로 볼 수 있도록
오류를 파일에 남기고, 이슈에 붙여 넣을 수 있는 진단 텍스트를 만든다.
외부 패키지 없이 Python 표준 라이브러리만 사용한다.

토큰·계정 식별자·사용자 폴더 이름은 로그와 보고서에 남기지 않는다.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

from state import ProviderState, config_path
from version import __version__

LOGGER_NAME = "aiw"
LOG_FILE_NAME = "widget.log"
MAX_LOG_BYTES = 512 * 1024
LOG_BACKUPS = 2
REPORT_LOG_LINES = 40

log = logging.getLogger(LOGGER_NAME)

# 설정 값 중 보고서에 그대로 실어도 되는 항목 (경로·식별자는 제외)
_SAFE_CONFIG_KEYS = (
    "alpha",
    "topmost",
    "compact",
    "transparent_bg",
    "adaptive_compact_color",
    "scale",
    "compact_scale",
    "font",
    "port",
    "auto_update_check",
    "show_antigravity",
    "show_pace",
    "usage_tracking",
    "web_source",
    "browser_interval_min",
    "claude_source",
)

_SECRET_PATTERNS = (
    # Authorization 헤더 값
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [가림]"),
    # Anthropic / OpenAI 형식 키
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "[가림]"),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), "[가림]"),
    # "token": "..." / csrf_token=... 같은 키-값
    (
        re.compile(
            r"(?i)([\"']?[a-z_-]*(?:token|secret|password|cookie|session_?key|api_?key)[\"']?\s*[:=]\s*[\"']?)"
            r"[^\s\"',;&}]{6,}"
        ),
        r"\1[가림]",
    ),
    # 조직·계정 UUID
    (
        re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
        "[uuid]",
    ),
    # 이메일
    (re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "[email]"),
)


def log_dir() -> Path:
    return config_path().parent


def log_path() -> Path:
    return log_dir() / LOG_FILE_NAME


def redact(text: str, home: str | None = None) -> str:
    """토큰·식별자·사용자 폴더 경로를 가린다."""
    out = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    home = home if home is not None else str(Path.home())
    if home and len(home) > 3:
        out = re.sub(re.escape(home), "~", out, flags=re.IGNORECASE)
        alt = home.replace("\\", "/")
        if alt != home:
            out = re.sub(re.escape(alt), "~", out, flags=re.IGNORECASE)
    return out


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(path: Path | None = None) -> Path | None:
    """파일 로그를 켠다. 폴더를 만들 수 없으면 조용히 건너뛰고 None을 돌려준다."""
    path = path or log_path()
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=True
        )
    except OSError:
        return None
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return path


def install_exception_hooks(tk_root=None) -> None:
    """잡히지 않은 예외를 로그에 남긴다 (메인·스레드·tkinter 콜백)."""
    prev_sys = sys.excepthook
    prev_thread = threading.excepthook

    def _sys_hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            log.error("처리되지 않은 예외", exc_info=(exc_type, exc, tb))
        prev_sys(exc_type, exc, tb)

    def _thread_hook(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread else "?"
            log.error("스레드 예외 (%s)", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        prev_thread(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook

    if tk_root is not None:

        def _tk_hook(exc_type, exc, tb):
            log.error("화면 콜백 예외", exc_info=(exc_type, exc, tb))

        tk_root.report_callback_exception = _tk_hook


def _format_duration(delta: timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}초"
    mins = secs // 60
    if mins < 60:
        return f"{mins}분"
    hours, mins = divmod(mins, 60)
    return f"{hours}시간 {mins}분" if mins else f"{hours}시간"


def _recovery_note(stats, provider: str) -> str:
    rec = stats.last_recovery(provider) if stats is not None else None
    if not rec:
        return ""
    count, since, until = rec
    return f", {count}회 실패·{_format_duration(until - since)} 동안"


class ProviderChangeLogger:
    """서비스별 조회 상태(성공/오류 문구)가 바뀔 때만 한 줄씩 기록한다."""

    def __init__(self) -> None:
        self._last: dict[str, tuple[bool, str | None]] = {}

    def observe(self, providers: Iterable[ProviderState], stats=None) -> list[str]:
        """stats(state.FetchStats)를 주면 복구 줄에 실패 횟수·지속 시간을 붙인다."""
        lines: list[str] = []
        seen: set[str] = set()
        for p in providers:
            seen.add(p.provider)
            key = (bool(p.ok), p.error)
            if self._last.get(p.provider) == key:
                continue
            first = p.provider not in self._last
            self._last[p.provider] = key
            if p.ok:
                if first:
                    line = f"{p.provider}: 조회 성공 (구간 {len(p.windows)}개)"
                else:
                    line = f"{p.provider}: 정상으로 돌아옴 (구간 {len(p.windows)}개{_recovery_note(stats, p.provider)})"
                log.info(line)
            else:
                line = f"{p.provider}: 조회 실패 - {p.error or '원인 없음'}"
                if p.response_shape:
                    line += f" | 응답 구조: {p.response_shape}"
                log.warning(line)
            lines.append(line)
        for gone in set(self._last) - seen:
            del self._last[gone]
        return lines


def read_log_tail(path: Path | None = None, lines: int = REPORT_LOG_LINES) -> list[str]:
    path = path or log_path()
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def _format_provider(p: ProviderState, now: datetime) -> str:
    if p.fetched_at:
        age_min = max(0, int((now - p.fetched_at).total_seconds() // 60))
        age = f"{age_min}분 전"
    else:
        age = "시각 없음"
    if p.ok:
        labels = ", ".join(f"{w.label} {round(w.used_percent)}%" for w in p.windows) or "구간 없음"
        return f"- {p.provider}: 정상 · {age} · {labels}"
    line = f"- {p.provider}: 오류 · {age} · {p.error or '원인 없음'}"
    if p.response_shape:
        line += f"\n  - 응답 구조: `{p.response_shape}`"
    return line


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "없음"
    return f"{_format_duration(now - at)} 전"


def _format_stats(provider: str, s: dict, now: datetime) -> str:
    line = (
        f"- {provider}: 성공 {s.get('ok', 0)} / 실패 {s.get('fail', 0)}"
        f" · 마지막 성공 {_ago(s.get('last_ok_at'), now)}"
        f" · 마지막 실패 {_ago(s.get('last_fail_at'), now)}"
    )
    if s.get("fail_streak"):
        line += f" · 지금 {s['fail_streak']}회 연속 실패"
    if s.get("last_fail_at") and s.get("last_error"):
        line += f"\n  - 마지막 실패 원인: {s['last_error']}"
    return line


def build_report(
    config: dict,
    providers: Iterable[ProviderState],
    *,
    extra: dict | None = None,
    log_lines: list[str] | None = None,
    now: datetime | None = None,
    stats: dict | None = None,
) -> str:
    """이슈에 붙여 넣을 수 있는 진단 텍스트를 만든다."""
    now = now or datetime.now(timezone.utc)
    frozen = bool(getattr(sys, "frozen", False))
    out = [
        "### AI 사용량 위젯 진단 정보",
        f"- 위젯 버전: {__version__} ({'exe' if frozen else '소스 실행'})",
        f"- OS: {platform.system()} {platform.release()} ({platform.version()})",
        f"- Python: {platform.python_version()}",
        f"- 생성 시각 (UTC): {now.strftime('%Y-%m-%d %H:%M')}",
    ]
    for key, value in (extra or {}).items():
        out.append(f"- {key}: {value}")

    out.append("")
    out.append("#### 설정")
    for key in _SAFE_CONFIG_KEYS:
        if key in config:
            out.append(f"- {key}: {config[key]}")
    out.append(f"- claude_oauth_path: {'지정됨' if config.get('claude_oauth_path') else '기본값'}")

    out.append("")
    out.append("#### 서비스 상태")
    items = list(providers)
    if items:
        out.extend(_format_provider(p, now) for p in items)
    else:
        out.append("- 받은 데이터 없음")

    if stats is not None:
        out.append("")
        out.append("#### 조회 기록 (위젯 실행 후 최근 24시간)")
        if stats:
            out.extend(_format_stats(name, s, now) for name, s in stats.items())
        else:
            out.append("- 아직 조회 기록 없음")

    tail = read_log_tail() if log_lines is None else log_lines
    out.append("")
    out.append(f"#### 최근 로그 (마지막 {len(tail)}줄)")
    out.append("```")
    out.extend(tail or ["(로그 없음)"])
    out.append("```")
    return redact("\n".join(out))
