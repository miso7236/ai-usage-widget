import { test } from "node:test";
import assert from "node:assert/strict";
import {
  TOKEN_HEADER,
  createTokenSource,
  parseTokenFile,
  widgetFetch,
  withToken,
} from "../../extension/lib/widget-auth.js";

const TOKEN = "ab".repeat(32);
const NEW_TOKEN = "cd".repeat(32);

function fakeFetch(statusFor) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return { status: statusFor(init.headers?.[TOKEN_HEADER]), ok: true };
  };
  fn.calls = calls;
  return fn;
}

test("토큰 파일 읽기", () => {
  assert.equal(parseTokenFile({ token: TOKEN, header: TOKEN_HEADER }), TOKEN);
  assert.equal(parseTokenFile({ token: "short" }), null);
  assert.equal(parseTokenFile({ token: TOKEN.toUpperCase() }), null);
  assert.equal(parseTokenFile(null), null);
  assert.equal(parseTokenFile({}), null);
});

test("헤더에 토큰 붙이기 (원래 헤더는 그대로)", () => {
  const base = { "Content-Type": "application/json" };
  assert.deepEqual(withToken(base, TOKEN), { "Content-Type": "application/json", [TOKEN_HEADER]: TOKEN });
  assert.deepEqual(base, { "Content-Type": "application/json" });
  assert.deepEqual(withToken(base, null), base);
  assert.deepEqual(withToken(undefined, TOKEN), { [TOKEN_HEADER]: TOKEN });
});

test("토큰은 한 번 읽고 기억, 파일이 없으면 다음에 다시 읽는다", async () => {
  let reads = 0;
  let file = null;
  const source = createTokenSource(async () => {
    reads++;
    if (!file) throw new Error("not found");
    return file;
  });
  assert.equal(await source.get(), null);
  file = { token: TOKEN };
  assert.equal(await source.get(), TOKEN);
  assert.equal(await source.get(), TOKEN);
  assert.equal(reads, 2);
  source.invalidate();
  file = { token: NEW_TOKEN };
  assert.equal(await source.get(), NEW_TOKEN);
});

test("위젯 요청마다 토큰 헤더를 보낸다", async () => {
  const fetchFn = fakeFetch(() => 200);
  const source = createTokenSource(async () => ({ token: TOKEN }));
  const { res, token } = await widgetFetch(fetchFn, source, "http://127.0.0.1:47823/update", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  assert.equal(res.status, 200);
  assert.equal(token, TOKEN);
  assert.equal(fetchFn.calls.length, 1);
  assert.equal(fetchFn.calls[0].init.method, "POST");
  assert.equal(fetchFn.calls[0].init.body, "{}");
  assert.equal(fetchFn.calls[0].init.headers[TOKEN_HEADER], TOKEN);
  assert.equal(fetchFn.calls[0].init.headers["Content-Type"], "application/json");
});

test("토큰 파일이 없으면 헤더 없이 보낸다 (옛 위젯·하위 호환)", async () => {
  const fetchFn = fakeFetch(() => 200);
  const source = createTokenSource(async () => {
    throw new Error("not found");
  });
  const { token } = await widgetFetch(fetchFn, source, "http://127.0.0.1:47823/wait-command", {});
  assert.equal(token, null);
  assert.equal(TOKEN_HEADER in fetchFn.calls[0].init.headers, false);
});

test("401이면 토큰 파일을 다시 읽어 한 번만 재시도", async () => {
  let file = { token: TOKEN };
  const source = createTokenSource(async () => file);
  await source.get(); // 옛 토큰을 기억한 상태
  file = { token: NEW_TOKEN }; // 위젯이 토큰을 새로 만듦
  const fetchFn = fakeFetch((t) => (t === NEW_TOKEN ? 200 : 401));
  const { res, token } = await widgetFetch(fetchFn, source, "http://x/update", {});
  assert.equal(res.status, 200);
  assert.equal(token, NEW_TOKEN);
  assert.equal(fetchFn.calls.length, 2);

  // 다시 읽어도 같은 토큰이면 재시도하지 않는다
  const always401 = fakeFetch(() => 401);
  const r2 = await widgetFetch(always401, source, "http://x/update", {});
  assert.equal(r2.res.status, 401);
  assert.equal(always401.calls.length, 1);
});
