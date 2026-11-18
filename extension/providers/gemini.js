import { normalizeGemini, makeError } from "../lib/normalize.js";

// Gemini는 사용량을 바로 주는 내부 API가 아직 확인되지 않았다.
// 그래서 gemini.google.com/usage 화면을 최소화된 창으로 열고,
// content script(content/gemini-usage.js)가 화면에서 숫자를 읽어 보내 주면 그 값을 쓴다.
// 사용자가 직접 그 페이지를 열어도 같은 방식으로 갱신된다.

export const GEMINI_USAGE_URL = "https://gemini.google.com/usage";

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

export async function fetchGeminiViaWindow(timeoutMs = 30000) {
  if (pending) throw new Error("Gemini 갱신이 이미 진행 중이에요");

  const win = await chrome.windows.create({
    url: GEMINI_USAGE_URL,
    state: "minimized",
    focused: false,
  });

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
      await chrome.windows.remove(win.id);
    } catch {
      // 사용자가 이미 닫은 경우
    }
  }
}
