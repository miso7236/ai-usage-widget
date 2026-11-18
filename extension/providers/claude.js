import { fetchJson } from "../lib/http.js";
import { normalizeClaude, pickClaudeOrg } from "../lib/normalize.js";

const BASE = "https://claude.ai/api";

// claude.ai의 내부 API. 공식 API가 아니므로 바뀔 수 있다.
// 확인 방법은 docs/ENDPOINTS.md 참고.
export async function fetchClaude(settings) {
  const orgs = await fetchJson(`${BASE}/organizations`, { siteName: "Claude" });
  const org = pickClaudeOrg(orgs, settings.claudeOrgId);
  if (!org) throw new Error("Claude 조직 정보를 찾지 못했어요 (로그인 확인)");
  const usage = await fetchJson(`${BASE}/organizations/${org.uuid}/usage`, { siteName: "Claude" });
  return normalizeClaude(usage);
}
