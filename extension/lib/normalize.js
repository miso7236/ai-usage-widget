// 서비스마다 다른 응답을 하나의 공통 형식으로 바꾸는 순수 함수 모음.
// 공통 형식:
// {
//   provider: "claude" | "chatgpt" | "gemini",
//   name: "Claude",
//   ok: true,
//   windows: [{ key, label, usedPercent, resetsAt, resetText }],
//   error: null,
//   fetchedAt: "2026-11-18T14:00:00.000Z"
// }
// usedPercent는 항상 "사용한 비율"(0~100)로 통일한다.

export const PROVIDER_NAMES = {
  claude: "Claude",
  chatgpt: "ChatGPT",
  gemini: "Gemini",
};

export function clampPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Math.max(0, Math.min(100, Math.round(n * 10) / 10));
}

// 숫자(초 또는 밀리초) 또는 문자열 날짜를 ISO 문자열로 바꾼다.
export function toIso(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") {
    const ms = value < 1e12 ? value * 1000 : value; // 초 단위면 밀리초로
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function labelForWindowSeconds(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return null;
  if (s === 5 * 3600) return "5시간";
  if (s === 7 * 24 * 3600) return "주간";
  if (s === 24 * 3600) return "일간";
  if (s === 30 * 24 * 3600) return "월간";
  if (s % (24 * 3600) === 0) return `${s / (24 * 3600)}일`;
  if (s % 3600 === 0) return `${s / 3600}시간`;
  return `${Math.round(s / 60)}분`;
}

export function makeResult(provider, windows, nowIso = new Date().toISOString()) {
  return {
    provider,
    name: PROVIDER_NAMES[provider] ?? provider,
    ok: true,
    windows,
    error: null,
    fetchedAt: nowIso,
  };
}

export function makeError(provider, message, nowIso = new Date().toISOString()) {
  return {
    provider,
    name: PROVIDER_NAMES[provider] ?? provider,
    ok: false,
    windows: [],
    error: String(message),
    fetchedAt: nowIso,
  };
}

// ---------------- Claude ----------------
// 예상 응답 (claude.ai/api/organizations/{org}/usage):
// { five_hour: { utilization: 23, resets_at: "..." }, seven_day: {...}, seven_day_sonnet: {...} }
const CLAUDE_LABELS = {
  five_hour: "5시간",
  seven_day: "주간",
  seven_day_sonnet: "주간 Sonnet",
  seven_day_opus: "주간 Opus",
  extra_usage: "추가 사용량",
};
const CLAUDE_ORDER = ["five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"];

export function normalizeClaude(json, nowIso) {
  if (!json || typeof json !== "object") {
    throw new Error("Claude 응답 형식을 알 수 없어요");
  }
  const windows = [];
  for (const [key, value] of Object.entries(json)) {
    if (!value || typeof value !== "object") continue;
    // 추가 사용량(유료 초과 사용)은 켜져 있을 때만 보여 준다.
    if (value.is_enabled === false) continue;
    const used = clampPercent(value.utilization);
    if (used === null) continue;
    windows.push({
      key,
      label: CLAUDE_LABELS[key] ?? key,
      usedPercent: used,
      resetsAt: toIso(value.resets_at),
      resetText: null,
    });
  }
  windows.sort((a, b) => orderIndex(CLAUDE_ORDER, a.key) - orderIndex(CLAUDE_ORDER, b.key));
  if (windows.length === 0) {
    throw new Error("Claude 응답에서 사용량을 찾지 못했어요 (형식이 바뀌었을 수 있어요)");
  }
  return makeResult("claude", windows, nowIso);
}

// 여러 조직 중 채팅 구독이 있는 조직을 고른다.
export function pickClaudeOrg(orgs, preferredId) {
  if (!Array.isArray(orgs) || orgs.length === 0) return null;
  if (preferredId) {
    const found = orgs.find((o) => o.uuid === preferredId);
    if (found) return found;
  }
  const withChat = orgs.find(
    (o) => Array.isArray(o.capabilities) && o.capabilities.includes("chat")
  );
  return withChat ?? orgs[0];
}

// ---------------- ChatGPT ----------------
// 예상 응답 (chatgpt.com/backend-api/wham/usage):
// { plan_type, rate_limit: { primary_window: { used_percent, limit_window_seconds,
//   reset_after_seconds, reset_at }, secondary_window: {...} } }
export function normalizeChatGPT(json, nowIso = new Date().toISOString()) {
  if (!json || typeof json !== "object") {
    throw new Error("ChatGPT 응답 형식을 알 수 없어요");
  }
  const windows = [];
  const rl = json.rate_limit;
  if (rl && typeof rl === "object") {
    for (const [key, fallback] of [
      ["primary_window", "5시간"],
      ["secondary_window", "주간"],
    ]) {
      const w = rl[key];
      if (!w || typeof w !== "object") continue;
      const win = chatgptWindow(key, w, fallback, nowIso);
      if (win) windows.push(win);
    }
  }
  // 형식이 바뀐 경우를 대비해 used_percent를 가진 객체를 전부 찾아본다.
  if (windows.length === 0) {
    for (const { path, obj } of findObjectsWithKey(json, "used_percent")) {
      const win = chatgptWindow(path, obj, path, nowIso);
      if (win) windows.push(win);
    }
  }
  if (windows.length === 0) {
    throw new Error("ChatGPT 응답에서 사용량을 찾지 못했어요 (형식이 바뀌었을 수 있어요)");
  }
  return makeResult("chatgpt", windows, nowIso);
}

function chatgptWindow(key, w, fallbackLabel, nowIso) {
  const used = clampPercent(w.used_percent);
  if (used === null) return null;
  let resetsAt = toIso(w.reset_at);
  if (!resetsAt && Number.isFinite(Number(w.reset_after_seconds))) {
    resetsAt = new Date(Date.parse(nowIso) + Number(w.reset_after_seconds) * 1000).toISOString();
  }
  return {
    key,
    label: labelForWindowSeconds(w.limit_window_seconds) ?? fallbackLabel,
    usedPercent: used,
    resetsAt,
    resetText: null,
  };
}

// ---------------- Gemini ----------------
// 확장의 content script가 gemini.google.com/usage 화면에서 읽어 온 항목:
// [{ label: "5시간 한도", percent: 40, resetText: "오후 3:00에 재설정" }]
const REMAINING_WORDS = /(남음|남은|잔여|remaining|left)/i;

export function normalizeGemini(items, nowIso) {
  if (!Array.isArray(items) || items.length === 0) {
    throw new Error("Gemini 사용량 화면에서 숫자를 찾지 못했어요");
  }
  const windows = [];
  const seen = new Set();
  items.forEach((item, i) => {
    const p = clampPercent(item?.percent);
    if (p === null) return;
    const label = String(item.label ?? "").trim() || `항목 ${i + 1}`;
    const used = REMAINING_WORDS.test(label) || item.isRemaining ? clampPercent(100 - p) : p;
    const dedupeKey = `${label}|${used}`;
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);
    windows.push({
      key: `item${i}`,
      label: shortenGeminiLabel(label),
      usedPercent: used,
      resetsAt: null,
      resetText: item.resetText ? String(item.resetText).slice(0, 60) : null,
    });
  });
  if (windows.length === 0) {
    throw new Error("Gemini 사용량 화면에서 숫자를 찾지 못했어요");
  }
  return makeResult("gemini", windows, nowIso);
}

export function shortenGeminiLabel(label) {
  if (/5\s*(시간|hour|hr)/i.test(label)) return "5시간";
  if (/(주간|week)/i.test(label)) return "주간";
  if (/(일간|daily|day)/i.test(label)) return "일간";
  return label.length > 14 ? label.slice(0, 14) + "…" : label;
}

// ---------------- 공통 도우미 ----------------
function orderIndex(order, key) {
  const i = order.indexOf(key);
  return i === -1 ? order.length : i;
}

export function findObjectsWithKey(root, keyName, path = "", out = [], depth = 0) {
  if (!root || typeof root !== "object" || depth > 6) return out;
  if (!Array.isArray(root) && keyName in root) out.push({ path: path || "root", obj: root });
  for (const [k, v] of Object.entries(root)) {
    if (v && typeof v === "object") {
      findObjectsWithKey(v, keyName, path ? `${path}.${k}` : k, out, depth + 1);
    }
  }
  return out;
}

// 배지에 표시할 가장 높은 사용률
export function maxUsed(results) {
  let max = null;
  for (const r of results) {
    if (!r?.ok) continue;
    for (const w of r.windows) {
      if (max === null || w.usedPercent > max) max = w.usedPercent;
    }
  }
  return max;
}
