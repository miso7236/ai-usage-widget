import { normalizeGemini } from "./normalize.js";

export const GEMINI_BATCHEXECUTE_URL =
  "https://gemini.google.com/_/BardChatUi/data/batchexecute";

/**
 * Google batchexecute 응답 앞의 XSSI 방지 접두사(")]}'")를 제거한다.
 */
export function stripXssiPrefix(text) {
  if (typeof text !== "string") return "";
  return text.replace(/^\)\]\}'\r?\n?/, "").trimStart();
}

/**
 * batchexecute 응답 본문에서 wrb.fr 프레임 목록을 파싱한다.
 * Google은 보통 길이 줄(숫자) + JSON 배열 줄이 번갈아 나오는 청크 형식을 쓴다.
 */
export function parseBatchExecuteFrames(rawText) {
  const cleaned = stripXssiPrefix(rawText);
  if (!cleaned) return [];

  const frames = [];
  const lines = cleaned.split(/\r?\n/);

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || /^\d+$/.test(trimmed)) continue;

    let parsed;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      continue;
    }

    collectWrbFrames(parsed, frames);
  }

  return frames;
}

function collectWrbFrames(node, out) {
  if (!Array.isArray(node)) return;
  if (node[0] === "wrb.fr" && typeof node[1] === "string") {
    let payload = null;
    if (typeof node[2] === "string") {
      try {
        payload = JSON.parse(node[2]);
      } catch {
        payload = node[2];
      }
    } else if (node[2] !== undefined) {
      payload = node[2];
    }
    out.push({
      rpcId: node[1],
      payload,
      error: node[5] ?? null,
      index: node[6] ?? null,
    });
    return;
  }
  for (const item of node) {
    if (Array.isArray(item)) collectWrbFrames(item, out);
  }
}

/**
 * batchexecute POST 요청 본문(application/x-www-form-urlencoded)을 만든다.
 */
export function buildBatchExecuteRequest({ rpcId, payload = [], atToken = "" }) {
  if (!rpcId) throw new Error("batchexecute rpcId가 필요해요");
  const serializedPayload = typeof payload === "string" ? payload : JSON.stringify(payload);
  const fReq = JSON.stringify([[[rpcId, serializedPayload, null, "generic"]]]);
  const params = new URLSearchParams({ "f.req": fReq });
  if (atToken) params.set("at", atToken);
  return params.toString();
}

/**
 * 파싱된 batchexecute 프레임에서 특정 rpcId의 사용량 항목을 추출해 normalizeGemini로 변환한다.
 * 실제 패킷의 배열 인덱스가 확정되면 mapPayload(payload)만 연결하면 된다.
 */
export function parseGeminiBatchExecute(
  rawText,
  { rpcId, mapPayload, nowIso = new Date().toISOString() } = {}
) {
  const frames = parseBatchExecuteFrames(rawText);
  if (frames.length === 0) {
    throw new Error("Gemini batchexecute 응답 프레임을 읽지 못했어요");
  }

  const target = rpcId ? frames.find((f) => f.rpcId === rpcId) : frames[0];
  if (!target) {
    throw new Error(`Gemini batchexecute 응답에서 rpcId(${rpcId})를 찾지 못했어요`);
  }
  if (target.error != null) {
    throw new Error(`Gemini batchexecute 오류 응답 (${JSON.stringify(target.error)})`);
  }
  if (typeof mapPayload !== "function") {
    throw new Error("Gemini batchexecute 패킷 매퍼(mapPayload)가 설정되지 않았어요");
  }

  const items = mapPayload(target.payload);
  return normalizeGemini(items, nowIso);
}
