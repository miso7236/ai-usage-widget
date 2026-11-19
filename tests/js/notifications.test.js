import test from "node:test";
import assert from "node:assert/strict";
import { buildThresholds, evaluateNotifications } from "../../extension/lib/notifications.js";

function makeResult(usedPercent, resetsAt = "2026-11-19T18:00:00Z", ok = true) {
  return {
    provider: "claude",
    name: "Claude",
    ok,
    windows: [{ label: "5시간", usedPercent, resetsAt, resetText: null }],
    error: ok ? null : "오류",
    fetchedAt: "2026-11-19T15:00:00Z",
  };
}

test("임계치 목록 생성", () => {
  assert.deepEqual(buildThresholds(80), [80, 90, 100]);
  assert.deepEqual(buildThresholds(90), [90, 100]);
  assert.deepEqual(buildThresholds(95), [95, 100]);
  assert.deepEqual(buildThresholds(100), [100]);
  assert.deepEqual(buildThresholds(20), [50, 90, 100]);
});

test("임계치 돌파 시 알림을 보내고 같은 주기에서는 중복 발송하지 않는다", () => {
  const settings = { notificationsEnabled: true, notifyThreshold: 80, notifyOnReset: true };

  // 50% -> 알림 없음
  let { notifications, nextState } = evaluateNotifications({}, makeResult(50), settings);
  assert.equal(notifications.length, 0);

  // 82% -> 80% 알림 1건
  ({ notifications, nextState } = evaluateNotifications(nextState, makeResult(82), settings));
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].kind, "threshold");
  assert.equal(notifications[0].level, 80);

  // 85% (같은 주기) -> 중복 알림 없음
  ({ notifications, nextState } = evaluateNotifications(nextState, makeResult(85), settings));
  assert.equal(notifications.length, 0);

  // 92% -> 90% 알림 1건
  ({ notifications, nextState } = evaluateNotifications(nextState, makeResult(92), settings));
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].level, 90);
});

test("한 번에 여러 임계치를 넘으면 가장 높은 임계치 1건만 알린다", () => {
  const settings = { notificationsEnabled: true, notifyThreshold: 80, notifyOnReset: true };
  const { notifications, nextState } = evaluateNotifications({}, makeResult(96), settings);
  assert.equal(notifications.length, 1);
  assert.equal(notifications[0].level, 90);

  // 이후 97%에서는 80/90 모두 이미 기록되어 중복 알림 없음
  const second = evaluateNotifications(nextState, makeResult(97), settings);
  assert.equal(second.notifications.length, 0);
});

test("높은 사용률 상태에서 리셋되면 리셋 완료 알림을 보낸다", () => {
  const settings = { notificationsEnabled: true, notifyThreshold: 80, notifyOnReset: true };

  let { nextState } = evaluateNotifications({}, makeResult(95, "2026-11-19T18:00:00Z"), settings);
  const res = evaluateNotifications(nextState, makeResult(5, "2026-11-19T23:00:00Z"), settings);
  assert.equal(res.notifications.length, 1);
  assert.equal(res.notifications[0].kind, "reset");

  // 새 주기에서 다시 85%가 되면 임계치 알림이 정상 발송된다
  const again = evaluateNotifications(res.nextState, makeResult(85, "2026-11-19T23:00:00Z"), settings);
  assert.equal(again.notifications.length, 1);
  assert.equal(again.notifications[0].kind, "threshold");
});

test("사용량이 낮았던 상태의 리셋이나 오류 응답에서는 알림을 보내지 않는다", () => {
  const settings = { notificationsEnabled: true, notifyThreshold: 80, notifyOnReset: true };

  let { nextState } = evaluateNotifications({}, makeResult(40, "2026-11-19T18:00:00Z"), settings);
  let res = evaluateNotifications(nextState, makeResult(0, "2026-11-19T23:00:00Z"), settings);
  assert.equal(res.notifications.length, 0);

  // 오류 응답(ok: false)은 알림 없음
  res = evaluateNotifications(nextState, makeResult(100, "2026-11-19T18:00:00Z", false), settings);
  assert.equal(res.notifications.length, 0);
});

test("알림이 꺼져 있으면 발송하지 않지만 상태는 최신화한다", () => {
  const disabled = { notificationsEnabled: false, notifyThreshold: 80, notifyOnReset: true };
  const enabled = { notificationsEnabled: true, notifyThreshold: 80, notifyOnReset: true };

  const first = evaluateNotifications({}, makeResult(85), disabled);
  assert.equal(first.notifications.length, 0);

  // 알림을 다시 켜도 이미 지난 80% 구간은 중복 발송하지 않음
  const second = evaluateNotifications(first.nextState, makeResult(86), enabled);
  assert.equal(second.notifications.length, 0);
});
