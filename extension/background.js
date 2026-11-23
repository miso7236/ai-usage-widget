import { fetchClaude } from "./providers/claude.js";
import { fetchChatGPT } from "./providers/chatgpt.js";
import {
  fetchGeminiViaWindow,
  resultFromGeminiMessage,
  settlePending,
} from "./providers/gemini.js";
import { makeError, maxUsed } from "./lib/normalize.js";
import { decideSelfUpdate } from "./lib/version.js";
import { evaluateNotifications } from "./lib/notifications.js";
import { attachPredictions, recordHistory } from "./lib/history.js";
import { TOKEN_FILE, createTokenSource, widgetFetch } from "./lib/widget-auth.js";

const DEFAULT_SETTINGS = {
  enabled: { claude: true, chatgpt: true, gemini: true },
  intervalMin: 3, // Claude·ChatGPT 조회 간격(분)
  geminiAuto: true, // Gemini 화면을 백그라운드 탭으로 자동 갱신할지
  geminiIntervalMin: 15,
  widgetPort: 47823,
  claudeOrgId: null,
  notificationsEnabled: true,
  notifyThreshold: 80,
  notifyOnReset: true,
};

// ---------------- 저장소 ----------------
async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return {
    ...DEFAULT_SETTINGS,
    ...(settings || {}),
    enabled: { ...DEFAULT_SETTINGS.enabled, ...(settings?.enabled || {}) },
  };
}

async function getResults() {
  const { results } = await chrome.storage.local.get("results");
  return results || {};
}

// 여러 서비스 결과가 동시에 저장될 때 서로 덮어쓰지 않도록 순서대로 처리한다.
let saveChain = Promise.resolve();
function saveResult(result) {
  saveChain = saveChain.then(() => doSave(result)).catch((e) => console.error(e));
  return saveChain;
}

async function doSave(result) {
  const [results, { usageHistory }] = await Promise.all([
    getResults(),
    chrome.storage.local.get("usageHistory"),
  ]);
  const prev = results[result.provider];
  // 실패했을 때는 마지막으로 성공한 값을 흐리게라도 보여 주기 위해 남겨 둔다.
  if (!result.ok && prev?.windows?.length) {
    result.windows = prev.windows;
    result.lastOkAt = prev.ok ? prev.fetchedAt : prev.lastOkAt ?? null;
  }
  const nowMs = Date.now();
  const nextHistory = recordHistory(usageHistory || {}, result, nowMs);
  const enriched = attachPredictions(result, nextHistory, nowMs);
  results[enriched.provider] = enriched;
  await chrome.storage.local.set({ results, usageHistory: nextHistory });
  await maybeNotify(enriched);
  await publish(results);
}

async function maybeNotify(result) {
  const [settings, { notifyState }] = await Promise.all([
    getSettings(),
    chrome.storage.local.get("notifyState"),
  ]);
  if (!settings.enabled[result.provider]) return;
  const { notifications, nextState } = evaluateNotifications(notifyState || {}, result, settings);
  await chrome.storage.local.set({ notifyState: nextState });
  if (!chrome.notifications?.create) return;
  for (const n of notifications) {
    chrome.notifications.create(n.id, {
      type: "basic",
      iconUrl: "icon.png",
      title: n.title,
      message: n.message,
    });
  }
}

// ---------------- 위젯·배지로 내보내기 ----------------
async function publish(results) {
  const settings = await getSettings();
  const list = Object.values(results).filter((r) => settings.enabled[r.provider]);
  await updateBadge(list);
  await pushToWidget(list, settings.widgetPort);
}

let lastRefreshSeq = null;
let watchingCommands = false;

// 위젯이 이 확장 폴더에 써 둔 local-token.json (위젯 로컬 서버 인증용)
const widgetToken = createTokenSource(async () => {
  const res = await fetch(chrome.runtime.getURL(TOKEN_FILE), { cache: "no-store" });
  return res.ok ? res.json() : null;
});

async function pushToWidget(providers, port) {
  let widgetStatus;
  let remoteRefreshSeq = null;
  try {
    const { res, token } = await widgetFetch(fetch, widgetToken, `http://127.0.0.1:${port}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ providers, sentAt: new Date().toISOString() }),
    });
    const body = await res.json().catch(() => ({}));
    if (typeof body.refreshSeq === "number") remoteRefreshSeq = body.refreshSeq;
    widgetStatus = {
      ok: res.ok,
      at: new Date().toISOString(),
      error: res.ok ? null : res.status === 401 ? "위젯 연결 토큰이 맞지 않아요" : `HTTP ${res.status}`,
      widgetVersion: body.widgetVersion ?? null,
      extensionVersion: body.extensionVersion ?? null,
      // 토큰 파일이 없어 하위 호환으로만 연결된 상태 (팝업에 다시 설치 안내)
      tokenMissing: !token,
    };
  } catch {
    widgetStatus = { ok: false, at: new Date().toISOString(), error: "위젯이 실행 중이 아니에요" };
  }
  await chrome.storage.local.set({ widgetStatus });
  if (widgetStatus.ok) {
    await maybeSelfUpdate(widgetStatus.extensionVersion);
    if (remoteRefreshSeq !== null) {
      if (lastRefreshSeq === null) {
        lastRefreshSeq = remoteRefreshSeq;
      } else if (remoteRefreshSeq > lastRefreshSeq) {
        lastRefreshSeq = remoteRefreshSeq;
        pollAll().catch((e) => console.error(e));
      }
    }
    ensureCommandWatcher();
  }
}

// 위젯 헤더의 '↻' 버튼 클릭을 즉시 감지하기 위해 /wait-command를 짧게 대기한다.
// 위젯이 꺼져 있으면 바로 종료하고 다음 알람 때 다시 시도한다.
function ensureCommandWatcher() {
  if (watchingCommands) return;
  watchingCommands = true;
  watchWidgetCommands()
    .catch(() => {})
    .finally(() => {
      watchingCommands = false;
    });
}

async function watchWidgetCommands() {
  for (let i = 0; i < 6; i++) {
    const settings = await getSettings();
    const after = lastRefreshSeq ?? 0;
    const { res } = await widgetFetch(
      fetch,
      widgetToken,
      `http://127.0.0.1:${settings.widgetPort}/wait-command?after=${after}&timeout=20`,
      { cache: "no-store" }
    );
    if (!res.ok) return;
    const body = await res.json().catch(() => null);
    if (!body || !body.ok) return;
    if (typeof body.refreshSeq === "number") {
      const triggered = lastRefreshSeq !== null && body.refreshSeq > lastRefreshSeq;
      lastRefreshSeq = body.refreshSeq;
      if (triggered || (body.refresh && after > 0)) {
        await pollAll();
      }
    }
  }
}

// 위젯이 업데이트되면서 확장 폴더의 파일이 새 버전으로 바뀌면, 확장이 스스로 다시 로드한다.
async function maybeSelfUpdate(remoteVersion) {
  const local = chrome.runtime.getManifest().version;
  const { lastReloadFor } = await chrome.storage.local.get("lastReloadFor");
  const action = decideSelfUpdate(remoteVersion, local, lastReloadFor);
  if (action === "reload") {
    await chrome.storage.local.set({ lastReloadFor: remoteVersion, extUpdateManual: null });
    chrome.runtime.reload();
  } else if (action === "manual") {
    await chrome.storage.local.set({ extUpdateManual: remoteVersion });
  } else {
    await chrome.storage.local.set({ extUpdateManual: null });
  }
}

async function updateBadge(list) {
  const max = maxUsed(list);
  if (max === null) {
    const anyError = list.some((r) => !r.ok);
    await chrome.action.setBadgeText({ text: anyError ? "!" : "" });
    await chrome.action.setBadgeBackgroundColor({ color: "#757575" });
    return;
  }
  const color = max >= 90 ? "#c62828" : max >= 70 ? "#ef6c00" : "#2e7d32";
  await chrome.action.setBadgeText({ text: String(Math.round(max)) });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// ---------------- 조회 ----------------
const FETCHERS = {
  claude: (s) => fetchClaude(s),
  chatgpt: () => fetchChatGPT(),
};

async function pollAll() {
  const settings = await getSettings();
  const jobs = Object.entries(FETCHERS)
    .filter(([name]) => settings.enabled[name])
    .map(async ([name, fn]) => {
      let result;
      try {
        result = await fn(settings);
      } catch (e) {
        result = makeError(name, e.message, undefined, e.responseShape);
      }
      await saveResult(result);
    });
  await Promise.all(jobs);
}

async function refreshGemini() {
  const settings = await getSettings();
  if (!settings.enabled.gemini) return;
  // 결과 저장은 content script 메시지를 받을 때 처리된다. 시간 초과만 여기서 저장한다.
  const result = await fetchGeminiViaWindow();
  if (!result.ok) await saveResult(result);
}

// ---------------- 알람 ----------------
async function setupAlarms() {
  const settings = await getSettings();
  await chrome.alarms.clearAll();
  chrome.alarms.create("poll", { periodInMinutes: Math.max(1, settings.intervalMin) });
  if (settings.enabled.gemini && settings.geminiAuto) {
    chrome.alarms.create("gemini", {
      periodInMinutes: Math.max(5, settings.geminiIntervalMin),
      delayInMinutes: 0.5,
    });
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "poll") pollAll();
  if (alarm.name === "gemini") refreshGemini().catch((e) => console.error(e));
});

chrome.runtime.onInstalled.addListener(async () => {
  await setupAlarms();
  await pollAll();
});

chrome.runtime.onStartup.addListener(async () => {
  await setupAlarms();
  await pollAll();
});

// ---------------- 메시지 (팝업, content script) ----------------
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg, sender)
    .then(sendResponse)
    .catch((e) => sendResponse({ error: e.message }));
  return true; // 비동기 응답
});

async function handleMessage(msg) {
  switch (msg?.type) {
    case "gemini-usage": {
      const settings = await getSettings();
      const result = resultFromGeminiMessage(msg);
      if (settings.enabled.gemini) await saveResult(result);
      settlePending(result);
      return { ok: true };
    }
    case "get-state": {
      const [settings, results, { widgetStatus, extUpdateManual }] = await Promise.all([
        getSettings(),
        getResults(),
        chrome.storage.local.get(["widgetStatus", "extUpdateManual"]),
      ]);
      return {
        settings,
        results,
        widgetStatus: widgetStatus || null,
        extUpdateManual: extUpdateManual || null,
        version: chrome.runtime.getManifest().version,
      };
    }
    case "refresh":
      await pollAll();
      return { ok: true };
    case "refresh-gemini":
      await refreshGemini();
      return { ok: true };
    case "save-settings": {
      const current = await getSettings();
      const next = { ...current, ...msg.settings, enabled: { ...current.enabled, ...(msg.settings?.enabled || {}) } };
      await chrome.storage.local.set({ settings: next });
      // 꺼진 서비스는 결과에서 지운다.
      const results = await getResults();
      for (const name of Object.keys(results)) {
        if (!next.enabled[name]) delete results[name];
      }
      await chrome.storage.local.set({ results });
      await setupAlarms();
      await publish(results);
      return { ok: true, settings: next };
    }
    default:
      return { error: "알 수 없는 메시지" };
  }
}
