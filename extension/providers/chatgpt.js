import { fetchJson } from "../lib/http.js";
import { normalizeChatGPT } from "../lib/normalize.js";

// chatgpt.com 내부 API. 쿠키만으로는 안 되고, 세션에서 받은 액세스 토큰이 필요하다.
// 12월 ChatGPT·Codex 사용량 통합 이후 주소나 형식이 바뀔 수 있다.
export async function fetchChatGPT() {
  const session = await fetchJson("https://chatgpt.com/api/auth/session", { siteName: "ChatGPT" });
  const token = session?.accessToken;
  if (!token) throw new Error("ChatGPT 로그인이 필요해요 (크롬에서 chatgpt.com 로그인 확인)");

  const headers = { Authorization: `Bearer ${token}` };
  const accountId = session?.account?.id;
  if (accountId) headers["ChatGPT-Account-Id"] = accountId;

  const usage = await fetchJson("https://chatgpt.com/backend-api/wham/usage", {
    siteName: "ChatGPT",
    headers,
  });
  return normalizeChatGPT(usage);
}
