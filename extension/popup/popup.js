const ORDER = ["claude", "chatgpt", "gemini"];
const NAMES = { claude: "Claude", chatgpt: "ChatGPT", gemini: "Gemini" };

function send(msg) {
  return chrome.runtime.sendMessage(msg);
}

function el(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text !== undefined) e.textContent = text;
  return e;
}

function level(p) {
  return p >= 90 ? "danger" : p >= 70 ? "warn" : "ok";
}

function resetLabel(w) {
  if (w.resetsAt) {
    const ms = Date.parse(w.resetsAt) - Date.now();
    if (ms <= 0) return "곧 리셋";
    const min = Math.round(ms / 60000);
    const d = Math.floor(min / 1440);
    const h = Math.floor((min % 1440) / 60);
    const m = min % 60;
    const left = d > 0 ? `${d}일 ${h}시간` : h > 0 ? `${h}시간 ${m}분` : `${m}분`;
    return `${left} 후 리셋`;
  }
  return w.resetText || "";
}

function timeAgo(iso) {
  if (!iso) return "";
  const min = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return min <= 0 ? "방금" : `${min}분 전`;
}

function etaLabel(w) {
  if (!w.etaAt) return "";
  const ms = Date.parse(w.etaAt) - Date.now();
  if (!Number.isFinite(ms) || ms <= 0) return "";
  const min = Math.max(1, Math.round(ms / 60000));
  const h = Math.floor(min / 60);
  const m = min % 60;
  const left = h > 0 ? `${h}시간 ${m}분` : `${m}분`;
  return `약 ${left} 후 소진 예상`;
}

function render(state) {
  const box = document.getElementById("providers");
  box.replaceChildren();
  const { settings, results, widgetStatus } = state;

  for (const name of ORDER) {
    if (!settings.enabled[name]) continue;
    const r = results[name];
    const section = el("div", "provider");
    const hdr = el("div", "provider-header");
    hdr.append(el("div", "provider-name", NAMES[name]));
    if (r?.fetchedAt) hdr.append(el("span", "muted", `${timeAgo(r.fetchedAt)}`));
    section.append(hdr);

    if (!r) {
      section.append(el("div", "muted", name === "gemini" ? "'Gemini 갱신'을 눌러 주세요" : "아직 데이터가 없어요"));
      box.append(section);
      continue;
    }

    for (const w of r.windows) {
      const row = el("div", "row" + (r.ok ? "" : " stale"));
      row.append(el("span", "", w.label));
      const bar = el("div", "bar");
      const fill = el("div", level(w.usedPercent));
      fill.style.width = `${w.usedPercent}%`;
      bar.append(fill);
      row.append(bar);
      row.append(el("span", "pct", `${Math.round(w.usedPercent)}%`));
      const reset = resetLabel(w);
      const eta = r.ok ? etaLabel(w) : "";
      if (reset || eta) {
        row.append(el("span"));
        const meta = el("span", "reset");
        if (reset) meta.append(el("span", "", reset));
        if (eta) {
          if (reset) meta.append(el("span", "", " · "));
          meta.append(el("span", "eta", eta));
        }
        row.append(meta);
      }
      if (Array.isArray(w.sparkline) && w.sparkline.length >= 2) {
        row.append(el("span"));
        const spark = el("div", "sparkline");
        for (const v of w.sparkline) {
          const b = el("span", level(v));
          b.style.height = `${Math.max(15, Math.min(100, v))}%`;
          spark.append(b);
        }
        row.append(spark);
      }
      section.append(row);
    }
    if (r.error) section.append(el("div", "error", r.error));
    box.append(section);
  }

  const ws = document.getElementById("widget-status");
  const connected = Boolean(widgetStatus?.ok);
  ws.textContent = connected ? "위젯 연결됨" : "위젯 꺼짐";
  ws.classList.toggle("connected", connected);

  const notice = document.getElementById("notice");
  notice.hidden = !state.extUpdateManual;
  if (state.extUpdateManual) {
    notice.textContent =
      `확장 새 버전(v${state.extUpdateManual})이 있지만 자동으로 바뀌지 않았어요. ` +
      "위젯 오른쪽 클릭 → '크롬 확장 폴더 열기'의 폴더로 확장을 다시 설치해 주세요.";
  }
  const widgetVer = widgetStatus?.widgetVersion ? ` · 위젯 v${widgetStatus.widgetVersion}` : "";
  document.getElementById("version").textContent = `확장 v${state.version}${widgetVer}`;

  for (const input of document.querySelectorAll("input[data-provider]")) {
    input.checked = !!settings.enabled[input.dataset.provider];
  }
  document.getElementById("gemini-auto").checked = !!settings.geminiAuto;
  document.getElementById("interval").value = settings.intervalMin;
  document.getElementById("notify-enabled").checked = settings.notificationsEnabled !== false;
  document.getElementById("notify-threshold").value = settings.notifyThreshold ?? 80;
  document.getElementById("notify-reset").checked = settings.notifyOnReset !== false;
}

async function reload() {
  render(await send({ type: "get-state" }));
}

async function withBusy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "갱신 중…";
  try {
    await fn();
  } finally {
    button.disabled = false;
    button.textContent = label;
    await reload();
  }
}

document.getElementById("refresh").addEventListener("click", (e) =>
  withBusy(e.currentTarget, () => send({ type: "refresh" }))
);
document.getElementById("refresh-gemini").addEventListener("click", (e) =>
  withBusy(e.currentTarget, () => send({ type: "refresh-gemini" }))
);

for (const input of document.querySelectorAll("input[data-provider]")) {
  input.addEventListener("change", async () => {
    await send({ type: "save-settings", settings: { enabled: { [input.dataset.provider]: input.checked } } });
    await reload();
  });
}
document.getElementById("gemini-auto").addEventListener("change", async (e) => {
  await send({ type: "save-settings", settings: { geminiAuto: e.target.checked } });
});
document.getElementById("interval").addEventListener("change", async (e) => {
  const v = Math.max(1, Math.min(60, Number(e.target.value) || 3));
  await send({ type: "save-settings", settings: { intervalMin: v } });
});
document.getElementById("notify-enabled").addEventListener("change", async (e) => {
  await send({ type: "save-settings", settings: { notificationsEnabled: e.target.checked } });
});
document.getElementById("notify-threshold").addEventListener("change", async (e) => {
  const v = Math.max(50, Math.min(100, Number(e.target.value) || 80));
  await send({ type: "save-settings", settings: { notifyThreshold: v } });
});
document.getElementById("notify-reset").addEventListener("change", async (e) => {
  await send({ type: "save-settings", settings: { notifyOnReset: e.target.checked } });
});

reload();
