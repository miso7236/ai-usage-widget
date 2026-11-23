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
  // null·빈 문자열·true/false를 0%로 오해하지 않도록 먼저 거른다 (Python clamp_percent와 같은 규칙)
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
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
// 확장의 content script가 gemini.google.com/usage 화면에서 읽어 온 항목 또는 batchexecute 파싱 결과:
// [{ label: "5시간 한도", percent: 40, resetText: "오후 3:00에 재설정", resetsAt?: "..." }]
const REMAINING_WORDS = /(남음|남은|잔여|remaining|left)/i;

const MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };
const MONTH_RE = "(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\.?";

// "오후 3:00", "3:00 PM", "15:30" → { hour, minute } (로컬 시각). 없으면 null.
function parseClock(text) {
  const ko = /(오전|오후)\s*(\d{1,2}):(\d{2})/.exec(text);
  const en = /(\d{1,2}):(\d{2})\s*(AM|PM)/i.exec(text);
  const plain = /(?:^|[^\d:])(\d{1,2}):(\d{2})(?!\d)/.exec(text);
  let hour;
  let minute;
  if (ko) {
    hour = Number(ko[2]);
    minute = Number(ko[3]);
    if (ko[1] === "오후" && hour < 12) hour += 12;
    if (ko[1] === "오전" && hour === 12) hour = 0;
  } else if (en) {
    hour = Number(en[1]);
    minute = Number(en[2]);
    const ampm = en[3].toUpperCase();
    if (ampm === "PM" && hour < 12) hour += 12;
    if (ampm === "AM" && hour === 12) hour = 0;
  } else if (plain) {
    hour = Number(plain[1]);
    minute = Number(plain[2]);
  } else {
    return null;
  }
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
  return { hour, minute };
}

// "11월 24일", "Nov 24", "24 Nov" → { month(0~11), day }. 없으면 null.
function parseMonthDay(text) {
  const ko = /(\d{1,2})\s*월\s*(\d{1,2})\s*일/.exec(text);
  if (ko) return { month: Number(ko[1]) - 1, day: Number(ko[2]) };
  const enMonthFirst = new RegExp(`\\b${MONTH_RE}\\s+(\\d{1,2})(?![\\d:])`, "i").exec(text);
  if (enMonthFirst) return { month: MONTHS[enMonthFirst[1].toLowerCase()], day: Number(enMonthFirst[2]) };
  const enDayFirst = new RegExp(`\\b(\\d{1,2})\\s+${MONTH_RE}`, "i").exec(text);
  if (enDayFirst) return { month: MONTHS[enDayFirst[2].toLowerCase()], day: Number(enDayFirst[1]) };
  return null;
}

/**
 * Gemini 화면의 리셋 문구를 ISO 타임스탬프로 변환한다. 해석할 수 없으면 null.
 * - 날짜+시각: "11월 24일 오전 9:00에 재설정", "Resets Nov 24, 9:00 AM"
 * - 상대 시간: "2시간 10분 후", "Resets in 1 hr 20 min"
 * - 시각만:   "오후 3:00에 재설정" (이미 지났으면 다음 날)
 * 시각은 브라우저의 로컬 시간대로 해석한다.
 */
export function parseGeminiResetText(resetText, nowIso = new Date().toISOString()) {
  if (!resetText || typeof resetText !== "string") return null;
  const text = resetText.trim();
  if (!text) return null;

  const baseMs = Date.parse(nowIso);
  if (Number.isNaN(baseMs)) return null;
  const base = new Date(baseMs);

  // 1) 날짜가 있으면 그 날짜로 계산한다. ("24일"을 24일 뒤로 오해하지 않도록 가장 먼저 확인)
  const md = parseMonthDay(text);
  if (md) {
    const clock = parseClock(text) ?? { hour: 0, minute: 0 };
    const build = (year) => new Date(year, md.month, md.day, clock.hour, clock.minute, 0, 0);
    let target = build(base.getFullYear());
    // 12월에 "1월 2일"처럼 해가 바뀌는 경우
    if (target.getTime() < baseMs - 24 * 3600 * 1000) target = build(base.getFullYear() + 1);
    if (target.getMonth() !== md.month || target.getDate() !== md.day) return null; // 2월 30일 같은 잘못된 날짜
    return target.toISOString();
  }

  // "5시간마다", "every 5 hours"는 리셋까지 남은 시간이 아니라 주기라서 계산하지 않는다.
  if (/(마다|every)/i.test(text)) return null;

  // 2) 상대 시간
  if (!/:/.test(text)) {
    let totalMs = 0;
    const unitRegex = /(\d+)\s*(일|days?|d|시간|hours?|hrs?|hr|h|분|minutes?|mins?|min|m)(?![a-zA-Z가-힣])/gi;
    for (const m of text.matchAll(unitRegex)) {
      const value = Number(m[1]);
      const unit = m[2].toLowerCase();
      if (unit === "일" || unit.startsWith("day") || unit === "d") {
        totalMs += value * 24 * 3600 * 1000;
      } else if (unit === "시간" || unit.startsWith("hour") || unit.startsWith("hr") || unit === "h") {
        totalMs += value * 3600 * 1000;
      } else {
        totalMs += value * 60 * 1000;
      }
    }
    if (totalMs > 0) return new Date(baseMs + totalMs).toISOString();
  }

  // 3) 시각만 있으면 오늘(이미 지났으면 내일) 그 시각
  const clock = parseClock(text);
  if (clock) {
    const target = new Date(baseMs);
    target.setHours(clock.hour, clock.minute, 0, 0);
    if (target.getTime() <= baseMs) target.setDate(target.getDate() + 1);
    return target.toISOString();
  }

  return null;
}

export function normalizeGemini(items, nowIso = new Date().toISOString()) {
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
    const resetText = item.resetText ? String(item.resetText).slice(0, 60) : null;
    const resetsAt = toIso(item.resetsAt) ?? parseGeminiResetText(resetText, nowIso);
    windows.push({
      key: item.key ? String(item.key) : `item${i}`,
      label: shortenGeminiLabel(label),
      usedPercent: used,
      resetsAt,
      resetText,
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
