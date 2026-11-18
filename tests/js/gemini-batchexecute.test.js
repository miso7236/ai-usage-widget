import { test } from "node:test";
import assert from "node:assert/strict";
import {
  stripXssiPrefix,
  parseBatchExecuteFrames,
  buildBatchExecuteRequest,
  parseGeminiBatchExecute,
} from "../../extension/lib/gemini-batchexecute.js";

const NOW = "2026-11-18T14:00:00.000Z";

test("XSSI 접두사를 제거한다", () => {
  assert.equal(stripXssiPrefix(")]}'\n\n[[1,2]]"), "[[1,2]]");
  assert.equal(stripXssiPrefix("[[1,2]]"), "[[1,2]]");
});

test("batchexecute 응답에서 wrb.fr 프레임을 파싱한다", () => {
  const inner = JSON.stringify([[["5시간 한도", 35, "2시간 후"]]]);
  const chunk = JSON.stringify([["wrb.fr", "AbCdEf", inner, null, null, null, "generic"]]);
  const raw = `)]}'\n\n${chunk.length}\n${chunk}\n25\n[["di",12],["af.httprm",1]]`;

  const frames = parseBatchExecuteFrames(raw);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].rpcId, "AbCdEf");
  assert.deepEqual(frames[0].payload, [[["5시간 한도", 35, "2시간 후"]]]);
});

test("batchexecute 요청 본문을 인코딩한다", () => {
  const body = buildBatchExecuteRequest({
    rpcId: "AbCdEf",
    payload: [1, "ko"],
    atToken: "xsrf-token",
  });
  const params = new URLSearchParams(body);
  assert.equal(params.get("at"), "xsrf-token");
  assert.deepEqual(JSON.parse(params.get("f.req")), [[["AbCdEf", "[1,\"ko\"]", null, "generic"]]]);
});

test("parseGeminiBatchExecute가 mapPayload를 통해 정규화된 결과를 반환한다", () => {
  const inner = JSON.stringify({
    windows: [
      { title: "5시간 한도", used: 42, reset: "1시간 30분 후" },
      { title: "주간 한도", used: 70, reset: "3일 후" },
    ],
  });
  const chunk = JSON.stringify([["wrb.fr", "UsageRpc", inner, null, null, null, "generic"]]);
  const raw = `)]}'\n${chunk.length}\n${chunk}`;

  const result = parseGeminiBatchExecute(raw, {
    rpcId: "UsageRpc",
    nowIso: NOW,
    mapPayload: (payload) =>
      payload.windows.map((w) => ({
        label: w.title,
        percent: w.used,
        resetText: w.reset,
      })),
  });

  assert.equal(result.provider, "gemini");
  assert.equal(result.ok, true);
  assert.equal(result.windows.length, 2);
  assert.equal(result.windows[0].label, "5시간");
  assert.equal(result.windows[0].usedPercent, 42);
  assert.equal(result.windows[0].resetsAt, "2026-11-18T15:30:00.000Z");
  assert.equal(result.windows[1].label, "주간");
  assert.equal(result.windows[1].resetsAt, "2026-11-21T14:00:00.000Z");
});

test("batchexecute 응답에 오류 프레임이 있으면 예외를 던진다", () => {
  const chunk = JSON.stringify([["wrb.fr", "UsageRpc", null, null, null, [3], "generic"]]);
  const raw = `)]}'\n${chunk}`;
  assert.throws(
    () => parseGeminiBatchExecute(raw, { rpcId: "UsageRpc", mapPayload: () => [] }),
    /오류 응답/
  );
});
