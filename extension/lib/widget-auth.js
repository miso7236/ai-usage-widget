// 위젯 로컬 서버(127.0.0.1)에 보내는 요청에 붙이는 로컬 토큰.
// 위젯이 이 확장 폴더에 local-token.json을 써 두고, 확장은 그 값을 X-AIW-Token 헤더로 보낸다.
// 다른 확장이나 웹페이지는 이 폴더의 파일을 읽을 수 없어서 위젯에 가짜 값을 넣지 못한다.

export const TOKEN_FILE = "local-token.json";
export const TOKEN_HEADER = "X-AIW-Token";

const TOKEN_RE = /^[0-9a-f]{64}$/;

export function parseTokenFile(data) {
  const token = data?.token;
  return typeof token === "string" && TOKEN_RE.test(token) ? token : null;
}

// loadJson: 토큰 파일 JSON을 돌려주는 함수 (없으면 throw 하거나 null).
// 읽은 토큰은 기억해 두고, 파일이 없을 때는 다음 요청에서 다시 읽는다
// (위젯을 나중에 처음 실행해서 파일이 늦게 생길 수 있음).
export function createTokenSource(loadJson) {
  let cached = null;
  return {
    async get() {
      if (cached) return cached;
      try {
        cached = parseTokenFile(await loadJson());
      } catch {
        cached = null;
      }
      return cached;
    },
    invalidate() {
      cached = null;
    },
  };
}

export function withToken(headers, token) {
  const next = { ...(headers || {}) };
  if (token) next[TOKEN_HEADER] = token;
  return next;
}

// 토큰을 붙여 요청하고, 401이면 토큰 파일을 다시 읽어 한 번만 더 시도한다
// (위젯이 토큰을 새로 만들었을 수 있음). { res, token } 을 돌려준다.
export async function widgetFetch(fetchFn, tokenSource, url, init = {}) {
  let token = await tokenSource.get();
  let res = await fetchFn(url, { ...init, headers: withToken(init.headers, token) });
  if (res.status === 401) {
    tokenSource.invalidate();
    const fresh = await tokenSource.get();
    if (fresh && fresh !== token) {
      token = fresh;
      res = await fetchFn(url, { ...init, headers: withToken(init.headers, token) });
    }
  }
  return { res, token };
}
