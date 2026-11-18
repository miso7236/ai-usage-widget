import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeClaude,
  normalizeChatGPT,
  normalizeGemini,
  pickClaudeOrg,
  labelForWindowSeconds,
  toIso,
  maxUsed,
  makeError,
} from "../../extension/lib/normalize.js";

const NOW = "2026-11-18T14:00:00.000Z";

test("Claude 응답을 정규화한다", () => {
  const r = normalizeClaude(
    {
      seven_day: { utilization: 71, resets_at: "2026-11-21T18:00:00Z" },
      five_hour: { utilization: 42.25, resets_at: "2026-11-18T16:13:00Z" },
      seven_day_opus: null,
      extra_usage: { is_enabled: false, utilization: 0 },
    },
    NOW
  );
  assert.equal(r.ok, true);
  assert.deepEqual(
    r.windows.map((w) => [w.label, w.usedPercent]),
    [
      ["5시간", 42.3],
      ["주간", 71],
    ]
  );
  assert.equal(r.windows[0].resetsAt, "2026-11-18T16:13:00.000Z");
});

test("Claude 응답에 사용량이 없으면 오류", () => {
  assert.throws(() => normalizeClaude({ foo: 1 }, NOW), /찾지 못했어요/);
});

test("Claude 조직 선택: 지정 > chat 권한 > 첫 번째", () => {
  const orgs = [
    { uuid: "a", capabilities: ["api"] },
    { uuid: "b", capabilities: ["chat", "claude_pro"] },
  ];
  assert.equal(pickClaudeOrg(orgs).uuid, "b");
  assert.equal(pickClaudeOrg(orgs, "a").uuid, "a");
  assert.equal(pickClaudeOrg([{ uuid: "x" }]).uuid, "x");
  assert.equal(pickClaudeOrg([]), null);
});

test("ChatGPT(wham/usage) 응답을 정규화한다", () => {
  const r = normalizeChatGPT(
    {
      plan_type: "plus",
      rate_limit: {
        primary_window: { used_percent: 12, limit_window_seconds: 18000, reset_after_seconds: 3600 },
        secondary_window: { used_percent: 93, limit_window_seconds: 604800, reset_at: 1763560800 },
      },
    },
    NOW
  );
  assert.deepEqual(
    r.windows.map((w) => [w.label, w.usedPercent]),
    [
      ["5시간", 12],
      ["주간", 93],
    ]
  );
  assert.equal(r.windows[0].resetsAt, "2026-11-18T15:00:00.000Z");
  assert.equal(r.windows[1].resetsAt, new Date(1763560800 * 1000).toISOString());
});

test("ChatGPT 형식이 바뀌어도 used_percent를 찾아낸다", () => {
  const r = normalizeChatGPT({ limits: { combined: { used_percent: 55, limit_window_seconds: 86400 } } }, NOW);
  assert.equal(r.windows[0].usedPercent, 55);
  assert.equal(r.windows[0].label, "일간");
});

test("ChatGPT 응답에 사용량이 없으면 오류", () => {
  assert.throws(() => normalizeChatGPT({ rate_limit: null }, NOW), /찾지 못했어요/);
});

test("Gemini: 남은 비율은 사용 비율로 바꾸고 라벨을 줄인다", () => {
  const r = normalizeGemini(
    [
      { label: "5시간 한도", percent: 40, resetText: "오후 3:00에 재설정" },
      { label: "Weekly remaining", percent: 80 },
      { label: "5시간 한도", percent: 40 }, // 중복
    ],
    NOW
  );
  assert.deepEqual(
    r.windows.map((w) => [w.label, w.usedPercent]),
    [
      ["5시간", 40],
      ["주간", 20],
    ]
  );
  assert.equal(r.windows[0].resetText, "오후 3:00에 재설정");
});

test("Gemini: 항목이 없으면 오류", () => {
  assert.throws(() => normalizeGemini([], NOW));
});

test("도우미 함수", () => {
  assert.equal(labelForWindowSeconds(18000), "5시간");
  assert.equal(labelForWindowSeconds(604800), "주간");
  assert.equal(labelForWindowSeconds(7200), "2시간");
  assert.equal(labelForWindowSeconds(2592000), "월간");
  assert.equal(toIso(1763560800), toIso(1763560800000));
  assert.equal(toIso("not a date"), null);
  const ok = normalizeChatGPT({ rate_limit: { primary_window: { used_percent: 30 } } }, NOW);
  assert.equal(maxUsed([ok, makeError("claude", "x", NOW)]), 30);
  assert.equal(maxUsed([makeError("claude", "x", NOW)]), null);
});
