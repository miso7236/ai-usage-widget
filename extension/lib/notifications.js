// 사용량 임계치 도달 및 리셋 완료 알림 판단 (순수 함수).
// chrome.* API에 의존하지 않아 Node.js 단위 테스트로 검증할 수 있다.

export function buildThresholds(baseThreshold = 80) {
  const n = Number(baseThreshold);
  const base = Number.isFinite(n) ? Math.max(50, Math.min(100, Math.round(n))) : 80;
  const levels = [base];
  if (base < 90) levels.push(90);
  if (base < 100) levels.push(100);
  return levels;
}

export function evaluateNotifications(prevState, result, settings = {}) {
  const nextState = { ...(prevState || {}) };
  const notifications = [];

  if (!result || !result.ok || !Array.isArray(result.windows)) {
    return { notifications, nextState };
  }

  const enabled = settings.notificationsEnabled !== false;
  const notifyOnReset = settings.notifyOnReset !== false;
  const thresholds = buildThresholds(settings.notifyThreshold ?? 80);
  const baseThreshold = thresholds[0];
  const providerName = result.name || result.provider;

  for (const w of result.windows) {
    const used = Number(w?.usedPercent);
    if (!Number.isFinite(used)) continue;

    const label = w.label || "사용량";
    const key = `${result.provider}:${label}`;
    const prev = nextState[key] || null;
    const cycleKey = w.resetsAt || w.resetText || null;

    const cycleChanged = Boolean(prev?.lastCycleKey && cycleKey && prev.lastCycleKey !== cycleKey);
    const significantDrop = prev !== null && prev.lastPercent - used >= 30 && used <= 15;
    const isReset =
      Boolean(prev?.wasHigh) &&
      (significantDrop || (cycleChanged && used < prev.lastPercent - 20));

    let notifiedLevels = isReset || cycleChanged ? [] : [...(prev?.notifiedLevels || [])];

    if (isReset && enabled && notifyOnReset) {
      notifications.push({
        id: `reset:${key}:${cycleKey || Math.round(used)}`,
        kind: "reset",
        title: `${providerName} 사용량 리셋`,
        message: `${label} 한도가 리셋되었어요 (현재 ${Math.round(used)}%)`,
      });
    }

    const crossed = thresholds.filter((t) => used >= t && !notifiedLevels.includes(t));
    if (crossed.length > 0) {
      notifiedLevels = [...notifiedLevels, ...crossed];
      const highest = crossed[crossed.length - 1];
      if (enabled) {
        const title =
          highest >= 100
            ? `${providerName} 한도 도달`
            : `${providerName} 사용량 ${highest}% 도달`;
        notifications.push({
          id: `threshold:${key}:${highest}:${cycleKey || "none"}`,
          kind: "threshold",
          level: highest,
          title,
          message: `${label} 사용량이 ${Math.round(used)}%에 도달했어요.`,
        });
      }
    }

    const wasHigh =
      used >= baseThreshold || (!isReset && !cycleChanged && Boolean(prev?.wasHigh));

    nextState[key] = {
      lastPercent: used,
      lastCycleKey: cycleKey,
      notifiedLevels,
      wasHigh,
    };
  }

  return { notifications, nextState };
}
