// 브라우저의 로그인 쿠키를 그대로 사용해 JSON을 가져온다.
// 확장에 host_permissions가 있으므로 CORS 제한 없이 요청할 수 있다.

export class FetchError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function fetchJson(url, { headers = {}, siteName = "사이트", timeoutMs = 15000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(url, {
      method: "GET",
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json", ...headers },
      signal: controller.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") throw new FetchError(`${siteName} 응답 시간 초과`);
    throw new FetchError(`${siteName}에 연결하지 못했어요 (${e.message})`);
  } finally {
    clearTimeout(timer);
  }

  if (res.status === 401 || res.status === 403) {
    throw new FetchError(`${siteName} 로그인이 필요해요 (크롬에서 로그인했는지 확인)`, res.status);
  }
  if (res.status === 429) {
    throw new FetchError(`${siteName} 요청이 너무 많아요. 잠시 후 다시 시도해요`, res.status);
  }
  if (!res.ok) {
    throw new FetchError(`${siteName} 오류 (HTTP ${res.status})`, res.status);
  }

  const type = res.headers.get("content-type") || "";
  const text = await res.text();
  if (!type.includes("json")) {
    // Cloudflare 확인 화면 등 HTML이 돌아온 경우
    throw new FetchError(`${siteName}가 JSON 대신 웹페이지를 보냈어요. 사이트를 한 번 열어 주세요`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new FetchError(`${siteName} 응답을 읽지 못했어요`);
  }
}
