import { normalizeGemini, makeError } from "../lib/normalize.js";
import {
  GEMINI_BATCHEXECUTE_URL,
  buildBatchExecuteRequest,
  parseGeminiBatchExecute,
} from "../lib/gemini-batchexecute.js";

// Gemini는 batchexecute 내부 API 패킷(rpcId + mapPayload)이 설정되면 직접 요청으로 조회하고,
// 아직 설정되지 않았거나 실패한 경우 gemini.google.com/usage 최소화된 창 DOM 스크래핑으로 폴백한다.

export const GEMINI_USAGE_URL = "https://gemini.google.com/usage";

// 실제 batchexecute 패킷이 확보되면 아래 설정에 rpcId와 mapPayload를 채워 직접 조회를 활성화할 수 있다.
export const GEMINI_BATCHEXECUTE_CONFIG = {
  rpcId: null,
  payload: [],
  mapPayload: null,
};

let pending = null; // { resolve, timer }

// content script가 보낸 메시지를 공통 형식으로 바꾼다.
export function resultFromGeminiMessage(msg) {
  try {
    if (msg.error) throw new Error(msg.error);
    return normalizeGemini(msg.items);
  } catch (e) {
    return makeError("gemini", e.message);
  }
}

// background.js가 메시지를 받으면 호출. 기다리는 갱신 요청이 있으면 끝낸다.
export function settlePending(result) {
  if (!pending) return;
  clearTimeout(pending.timer);
  pending.resolve(result);
  pending = null;
}

export async function fetchGeminiBatchExecute({
  rpcId = GEMINI_BATCHEXECUTE_CONFIG.rpcId,
  payload = GEMINI_BATCHEXECUTE_CONFIG.payload,
  mapPayload = GEMINI_BATCHEXECUTE_CONFIG.mapPayload,
  atToken = "",
  timeoutMs = 15000,
} = {}) {
  if (!rpcId || typeof mapPayload !== "function") {
    throw new Error("Gemini batchexecute 설정(rpcId, mapPayload)이 아직 없어요");
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    const url = `${GEMINI_BATCHEXECUTE_URL}?rpcids=${encodeURIComponent(rpcId)}`;
    res = await fetch(url, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: buildBatchExecuteRequest({ rpcId, payload, atToken }),
      signal: controller.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Gemini 응답 시간 초과");
    throw new Error(`Gemini에 연결하지 못했어요 (${e.message})`);
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401 || res.status === 403) {
    throw new Error("Gemini 로그인이 필요해요 (크롬에서 로그인했는지 확인)");
  }
  if (res.status === 429) {
    throw new Error("Gemini 요청이 너무 많아요. 잠시 후 다시 시도해요");
  }
  if (!res.ok) {
    throw new Error(`Gemini 오류 (HTTP ${res.status})`);
  }

  const text = await res.text();
  return parseGeminiBatchExecute(text, { rpcId, mapPayload });
}

export async function fetchGemini(options = {}) {
  const rpcId = options.rpcId ?? GEMINI_BATCHEXECUTE_CONFIG.rpcId;
  const mapPayload = options.mapPayload ?? GEMINI_BATCHEXECUTE_CONFIG.mapPayload;
  if (rpcId && typeof mapPayload === "function") {
    try {
      return await fetchGeminiBatchExecute({ ...options, rpcId, mapPayload });
    } catch (e) {
      if (options.fallbackToWindow === false) {
        return makeError("gemini", e.message);
      }
    }
  }
  return fetchGeminiViaWindow(options.timeoutMs);
}

export async function fetchGeminiViaWindow(timeoutMs = 30000) {
  if (pending) throw new Error("Gemini 갱신이 이미 진행 중이에요");

  let tabId = null;
  let winId = null;
  try {
    const tab = await chrome.tabs.create({ url: GEMINI_USAGE_URL, active: false });
    tabId = tab?.id ?? null;
  } catch {
    const win = await chrome.windows.create({
      url: GEMINI_USAGE_URL,
      state: "minimized",
      focused: false,
    });
    winId = win?.id ?? null;
  }

  try {
    return await new Promise((resolve) => {
      pending = {
        resolve,
        timer: setTimeout(() => {
          pending = null;
          resolve(
            makeError(
              "gemini",
              "Gemini 사용량 화면을 읽지 못했어요 (로그인 확인 또는 화면 구조 변경)"
            )
          );
        }, timeoutMs),
      };
    });
  } finally {
    try {
      if (tabId !== null) await chrome.tabs.remove(tabId);
      else if (winId !== null) await chrome.windows.remove(winId);
    } catch {
      // 사용자가 이미 닫은 경우
    }
  }
}
