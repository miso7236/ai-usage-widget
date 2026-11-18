import { test } from "node:test";
import assert from "node:assert/strict";
import { compareVersions, decideSelfUpdate } from "../../extension/lib/version.js";

test("버전 비교", () => {
  assert.ok(compareVersions("0.10.0", "0.9.9") > 0);
  assert.equal(compareVersions("v0.2.0", "0.2.0"), 0);
  assert.ok(compareVersions("0.2", "0.2.1") < 0);
});

test("확장 자동 업데이트 판단", () => {
  assert.equal(decideSelfUpdate("0.3.0", "0.2.0", null), "reload");
  // 다시 로드했는데도 버전이 그대로면 무한 반복하지 않고 사용자에게 알린다
  assert.equal(decideSelfUpdate("0.3.0", "0.2.0", "0.3.0"), "manual");
  assert.equal(decideSelfUpdate("0.2.0", "0.2.0", "0.2.0"), "none");
  assert.equal(decideSelfUpdate("0.1.0", "0.2.0", null), "none");
  assert.equal(decideSelfUpdate(null, "0.2.0", null), "none");
});
