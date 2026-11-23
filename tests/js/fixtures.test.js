// tests/fixtures/normalize/*.json 공용 샘플로 크롬 확장의 응답 해석을 검증한다.
// 같은 샘플을 widget/tests/test_shared_fixtures.py가 내장 브라우저용 web_normalize.py에도 돌린다.
// 두 구현 중 한쪽만 고치면 둘 중 하나가 실패한다.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { normalizeClaude, normalizeChatGPT, normalizeGemini, makeError } from "../../extension/lib/normalize.js";

const DIR = fileURLToPath(new URL("../fixtures/normalize/", import.meta.url));
const NORMALIZERS = { claude: normalizeClaude, chatgpt: normalizeChatGPT, gemini: normalizeGemini };

// 분 단위 UTC 오프셋 → IANA 고정 오프셋 이름 (Etc/GMT는 부호가 반대)
function tzForOffset(minutes) {
  if (!minutes) return "UTC";
  if (minutes % 60 !== 0) throw new Error("정시 단위 오프셋만 지원해요");
  const h = minutes / 60;
  return `Etc/GMT${h > 0 ? "-" : "+"}${Math.abs(h)}`;
}

const epoch = (iso) => (iso === null || iso === undefined ? null : Date.parse(iso));

const files = readdirSync(DIR).filter((f) => f.endsWith(".json")).sort();

test("공용 샘플 파일이 있다", () => {
  assert.ok(files.length >= 10, `샘플이 ${files.length}개뿐이에요`);
});

for (const file of files) {
  const fx = JSON.parse(readFileSync(join(DIR, file), "utf8"));
  test(`샘플 ${file}: ${fx.description}`, () => {
    const normalize = NORMALIZERS[fx.provider];
    assert.ok(normalize, `알 수 없는 provider: ${fx.provider}`);
    const prevTz = process.env.TZ;
    process.env.TZ = tzForOffset(fx.utcOffsetMinutes);
    try {
      if (fx.error) {
        assert.throws(
          () => normalize(fx.input, new Date(fx.now).toISOString()),
          (e) => e.message.includes(fx.error) && (fx.shape === undefined || e.responseShape === fx.shape)
        );
        if (fx.shape !== undefined) {
          // 오류 결과에 실려 위젯까지 가는지
          let err;
          try {
            normalize(fx.input, new Date(fx.now).toISOString());
          } catch (e) {
            err = e;
          }
          assert.equal(err.responseShape, fx.shape);
          assert.equal(makeError(fx.provider, err.message, fx.now, err.responseShape).responseShape, fx.shape);
        }
        return;
      }
      const r = normalize(fx.input, new Date(fx.now).toISOString());
      assert.equal(r.ok, true);
      assert.equal(r.provider, fx.provider);
      const actual = r.windows.map((w) => ({ ...w, resetsAt: epoch(w.resetsAt) }));
      const expected = fx.expected.map((w) => ({ ...w, resetsAt: epoch(w.resetsAt) }));
      assert.deepEqual(actual, expected);
    } finally {
      if (prevTz === undefined) delete process.env.TZ;
      else process.env.TZ = prevTz;
    }
  });
}
