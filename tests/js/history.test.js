import test from "node:test";
import assert from "node:assert/strict";
import {
  MAX_HISTORY_AGE_MS,
  attachPredictions,
  estimateDepletion,
  recordHistory,
} from "../../extension/lib/history.js";

const T0 = Date.parse("2026-11-19T12:00:00Z");
const MIN = 60 * 1000;

function makeResult(usedPercent, resetsAt = "2026-11-19T17:00:00Z", ok = true) {
  return {
    provider: "claude",
    name: "Claude",
    ok,
    windows: [{ label: "5시간", usedPercent, resetsAt, resetText: null }],
    error: ok ? null : "오류",
    fetchedAt: new Date(T0).toISOString(),
  };
}

test("히스토리 기록: 오래된 데이터 정리, 리셋 시 초기화, 짧은 중복 생략", () => {
  let hist = recordHistory({}, makeResult(20), T0 - MAX_HISTORY_AGE_MS - MIN);
  // 24시간 이전 데이터는 다음 기록 시 정리된다
  hist = recordHistory(hist, makeResult(30), T0);
  assert.equal(hist["claude:5시간"].length, 1);
  assert.equal(hist["claude:5시간"][0].p, 30);

  // 30초 미만 동일 퍼센트는 중복 기록하지 않음
  hist = recordHistory(hist, makeResult(30), T0 + 10_000);
  assert.equal(hist["claude:5시간"].length, 1);

  // 정상 증가 기록
  hist = recordHistory(hist, makeResult(50), T0 + 30 * MIN);
  assert.equal(hist["claude:5시간"].length, 2);

  // 리셋(새 주기) 시 새 시계열로 시작
  hist = recordHistory(hist, makeResult(5, "2026-11-19T22:00:00Z"), T0 + 60 * MIN);
  assert.equal(hist["claude:5시간"].length, 1);
  assert.equal(hist["claude:5시간"][0].p, 5);
});

test("소진 속도 예측: 리셋 전 100% 도달 예상 시 ETA를 계산한다", () => {
  // 30분 동안 20% -> 50% (시간당 60% 증가) -> 남은 50%는 약 50분 후 소진
  const points = [
    { t: T0, p: 20, r: "2026-11-19T17:00:00Z" },
    { t: T0 + 30 * MIN, p: 50, r: "2026-11-19T17:00:00Z" },
  ];
  const est = estimateDepletion(
    points,
    { label: "5시간", usedPercent: 50, resetsAt: "2026-11-19T17:00:00Z" },
    T0 + 30 * MIN
  );
  assert.ok(est);
  assert.equal(est.ratePerHour, 60);
  assert.equal(est.minutesLeft, 50);
  assert.equal(est.etaAt, new Date(T0 + 80 * MIN).toISOString());
});

test("소진 속도 예측: 리셋 전에 소진되지 않거나 증가가 없으면 null", () => {
  // 1시간 동안 10% -> 15% (시간당 5% 증가, 100%까지 17시간 -> 5시간 리셋 전 도달 불가)
  const slow = [
    { t: T0, p: 10, r: "2026-11-19T17:00:00Z" },
    { t: T0 + 60 * MIN, p: 15, r: "2026-11-19T17:00:00Z" },
  ];
  assert.equal(
    estimateDepletion(slow, { usedPercent: 15, resetsAt: "2026-11-19T17:00:00Z" }, T0 + 60 * MIN),
    null
  );

  // 증가가 0이면 null
  const flat = [
    { t: T0, p: 40, r: null },
    { t: T0 + 30 * MIN, p: 40, r: null },
  ];
  assert.equal(estimateDepletion(flat, { usedPercent: 40, resetsAt: null }, T0 + 30 * MIN), null);

  // 5분 미만 구간이면 null
  const shortSpan = [
    { t: T0, p: 40, r: null },
    { t: T0 + 2 * MIN, p: 60, r: null },
  ];
  assert.equal(estimateDepletion(shortSpan, { usedPercent: 60, resetsAt: null }, T0 + 2 * MIN), null);
});

test("attachPredictions가 etaAt과 sparkline을 붙인다", () => {
  let hist = recordHistory({}, makeResult(20), T0);
  hist = recordHistory(hist, makeResult(50), T0 + 30 * MIN);
  const enriched = attachPredictions(makeResult(50), hist, T0 + 30 * MIN);
  assert.ok(enriched.windows[0].etaAt);
  assert.deepEqual(enriched.windows[0].sparkline, [20, 50]);
});
