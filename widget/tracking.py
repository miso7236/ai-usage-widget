"""사용량 변동 추적: 값이 늘고 있는 서비스만 잠시 더 자주 조회하도록 간격을 정한다.

조회 자체는 하지 않는다. 조회 결과를 받아(observe) 다음 조회까지 기다릴 시간(interval)만 알려 준다.
시각은 밖에서 초 단위 숫자로 넘겨받으므로 시계 없이 테스트할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FAST_SEC = 15.0  # 추적 중 조회 간격
COOL_SEC = 30.0  # 변동이 뜸해졌거나 추적이 길어졌을 때의 간격
COOL_AFTER_SEC = 120.0  # 이만큼 변동이 없으면 간격을 늦춘다
OFF_AFTER_SEC = 300.0  # 이만큼 변동이 없으면 추적을 끝낸다
LONG_RUN_SEC = 1800.0  # 추적이 이만큼 이어지면 간격을 늦춘다
MAX_FAILURES = 2  # 연속 실패가 이만큼이면 추적을 끝낸다
RESET_DROP = 5.0  # 값이 이만큼 넘게 떨어지면 초기화로 본다 (작은 흔들림과 구분)


@dataclass
class _Entry:
    high: dict[str, float] = field(default_factory=dict)  # 항목(5시간, 주간 등)별로 본 가장 높은 값
    base: dict[str, float] = field(default_factory=dict)  # 추적을 시작할 때의 값 (늘어난 양을 재는 기준)
    seen_at: float | None = None
    changed_at: float | None = None
    started_at: float | None = None
    failures: int = 0


class Tracker:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def observe(
        self,
        provider: str,
        values: dict[str, float] | None,
        now: float,
        *,
        base_sec: float,
        allow_start: bool = True,
    ) -> bool:
        """조회 결과 하나를 반영한다. values가 None이면 실패. 이번 결과로 추적이 새로 켜졌으면 True.

        base_sec은 이 서비스의 평소 조회 간격이다. 그보다 훨씬 오래 소식이 없었다면
        (절전에서 깨어난 직후 등) 그사이 변한 값은 변동으로 치지 않고 기준만 새로 잡는다.
        allow_start가 False면(화면 잠금 등) 값이 늘어도 추적을 새로 켜지 않는다.
        """
        entry = self._entries.setdefault(provider, _Entry())
        if values is None:
            entry.failures += 1
            if entry.failures >= MAX_FAILURES:
                self._stop(entry)
            return False
        entry.failures = 0

        long_gap = entry.seen_at is not None and now - entry.seen_at > base_sec * 2 + 60
        first = entry.seen_at is None
        entry.seen_at = now
        if first or long_gap:
            entry.high = dict(values)
            if long_gap:
                self._stop(entry)
            return False

        rose = False
        before = dict(entry.high)
        for label, value in values.items():
            high = entry.high.get(label)
            if high is None or value < high - RESET_DROP:
                entry.high[label] = value  # 새 항목이거나 초기화됨: 기준만 다시 잡는다
                before[label] = value
                if label in entry.base:
                    entry.base[label] = value
            elif value > high:
                entry.high[label] = value
                rose = True
        for label in [k for k in entry.high if k not in values]:
            del entry.high[label]

        if not rose:
            return False
        was_tracking = self.is_tracking(provider, now)
        if not was_tracking and not allow_start:
            return False
        entry.changed_at = now
        if not was_tracking:
            entry.started_at = now
            entry.base = before
        return not was_tracking

    def baseline(self, provider: str, now: float) -> dict[str, float]:
        """추적 중이면 추적을 시작할 때의 항목별 값. 아니면 빈 dict."""
        if not self.is_tracking(provider, now):
            return {}
        return dict(self._entries[provider].base)

    def is_tracking(self, provider: str, now: float) -> bool:
        entry = self._entries.get(provider)
        if entry is None or entry.changed_at is None:
            return False
        return now - entry.changed_at < OFF_AFTER_SEC

    def interval(self, provider: str, base_sec: float, now: float, *, fast_sec: float = FAST_SEC) -> float:
        """다음 조회까지 기다릴 시간(초). 추적 중이 아니면 평소 간격 그대로."""
        if not self.is_tracking(provider, now):
            return base_sec
        entry = self._entries[provider]
        quiet = now - (entry.changed_at or now)
        running = now - (entry.started_at or now)
        wanted = fast_sec if quiet < COOL_AFTER_SEC and running < LONG_RUN_SEC else max(fast_sec, COOL_SEC)
        return min(base_sec, wanted)

    def tracking(self, now: float) -> frozenset[str]:
        return frozenset(p for p in self._entries if self.is_tracking(p, now))

    def stop_all(self) -> None:
        for entry in self._entries.values():
            self._stop(entry)

    @staticmethod
    def _stop(entry: _Entry) -> None:
        entry.changed_at = None
        entry.started_at = None
        entry.base = {}
