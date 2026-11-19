// 사용량 시계열 히스토리 기록 및 소진 속도 예측 (순수 함수).
// chrome.* API에 의존하지 않아 Node.js 단위 테스트로 검증할 수 있다.

export const MAX_HISTORY_AGE_MS = 24 * 60 * 60 * 1000; // 24시간
export const MAX_POINTS_PER_WINDOW = 200;
const MIN_SAMPLE_GAP_MS = 30 * 1000;
const RATE_LOOKBACK_MS = 3 * 60 * 60 * 1000; // 최근 3시간 추세
const MIN_SPAN_MS = 5 * 60 * 1000; // 최소 5분 이상 경과 시 예측
const MAX_ETA_HOURS = 72;

export function recordHistory(prevHistory, result, nowMs = Date.now()) {
  const next = { ...(prevHistory || {}) };
  if (!result || !result.ok || !Array.isArray(result.windows)) {
    return next;
  }

  const cutoff = nowMs - MAX_HISTORY_AGE_MS;

  for (const w of result.windows) {
    const used = Number(w?.usedPercent);
    if (!Number.isFinite(used)) continue;

    const label = w.label || "사용량";
    const key = `${result.provider}:${label}`;
    const cycleKey = w.resetsAt || w.resetText || null;
    const prevPoints = Array.isArray(next[key]) ? next[key] : [];
    let points = prevPoints.filter((pt) => pt && typeof pt.t === "number" && pt.t >= cutoff);

    const last = points[points.length - 1];
    if (last) {
      const cycleChanged = Boolean(last.r && cycleKey && last.r !== cycleKey);
      const sharpDrop = last.p - used >= 20;
      if (cycleChanged || sharpDrop) {
        points = [];
      } else if (nowMs - last.t < MIN_SAMPLE_GAP_MS && Math.round(last.p) === Math.round(used)) {
        next[key] = points;
        continue;
      }
    }

    points = [...points, { t: nowMs, p: Math.round(used * 10) / 10, r: cycleKey }];
    if (points.length > MAX_POINTS_PER_WINDOW) {
      points = points.slice(points.length - MAX_POINTS_PER_WINDOW);
    }
    next[key] = points;
  }

  return next;
}

export function estimateDepletion(points, currentWindow, nowMs = Date.now()) {
  if (!Array.isArray(points) || points.length < 2 || !currentWindow) return null;
  const used = Number(currentWindow.usedPercent);
  if (!Number.isFinite(used) || used >= 100) return null;

  const recent = points.filter((pt) => pt && pt.t >= nowMs - RATE_LOOKBACK_MS && pt.t <= nowMs);
  if (recent.length < 2) return null;

  const oldest = recent[0];
  const latest = recent[recent.length - 1];
  const spanMs = latest.t - oldest.t;
  if (spanMs < MIN_SPAN_MS) return null;

  const delta = latest.p - oldest.p;
  const hours = spanMs / 3_600_000;
  const ratePerHour = delta / hours;
  if (ratePerHour <= 0.5) return null;

  const hoursToFull = (100 - used) / ratePerHour;
  if (!Number.isFinite(hoursToFull) || hoursToFull <= 0 || hoursToFull > MAX_ETA_HOURS) {
    return null;
  }

  const etaMs = nowMs + hoursToFull * 3_600_000;
  if (currentWindow.resetsAt) {
    const resetMs = Date.parse(currentWindow.resetsAt);
    if (Number.isFinite(resetMs) && etaMs >= resetMs) {
      return null; // 소진 전에 리셋되므로 경고할 필요 없음
    }
  }

  return {
    etaAt: new Date(etaMs).toISOString(),
    minutesLeft: Math.round(hoursToFull * 60),
    ratePerHour: Math.round(ratePerHour * 10) / 10,
  };
}

export function attachPredictions(result, history, nowMs = Date.now()) {
  if (!result || !Array.isArray(result.windows)) return result;
  const windows = result.windows.map((w) => {
    const key = `${result.provider}:${w.label || "사용량"}`;
    const points = Array.isArray(history?.[key]) ? history[key] : [];
    const est = result.ok ? estimateDepletion(points, w, nowMs) : null;
    const sparkline = points.slice(-12).map((pt) => Math.round(pt.p));
    return {
      ...w,
      etaAt: est ? est.etaAt : null,
      sparkline,
    };
  });
  return { ...result, windows };
}
