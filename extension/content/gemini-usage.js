// gemini.google.com/usage 화면에서 사용량 숫자를 읽어 확장 백그라운드로 보낸다.
// 화면 구조는 Google이 언제든 바꿀 수 있어서, 특정 클래스 이름에 의존하지 않고
// 1) 진행 막대(role="progressbar")의 aria 값, 2) "40%" 같은 글자 순서로 찾는다.
// 실제 화면을 보고 더 정확한 선택자로 바꾸면 안정성이 올라간다 (docs/ENDPOINTS.md 참고).

(() => {
  const RESET_WORDS = /(재설정|초기화|리셋|갱신|reset|refresh)/i;
  const REMAINING_WORDS = /(남음|남은|잔여|remaining|left)/i;
  const LOGIN_WORDS = /(로그인|sign in|log in)/i;
  const MAX_WAIT_MS = 20000;
  const SETTLE_MS = 800;

  function textOf(el) {
    return (el?.innerText || el?.textContent || "").trim();
  }

  function isVisibleElement(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
    if (el.closest('[aria-hidden="true"], [hidden], script, style, noscript, svg')) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }

  function isLoginRedirect() {
    if (location.hostname !== "gemini.google.com" || !location.pathname.startsWith("/usage")) {
      return true;
    }
    const signInLink = document.querySelector('a[href*="accounts.google.com"]');
    if (signInLink && isVisibleElement(signInLink) && LOGIN_WORDS.test(textOf(signInLink))) {
      return !document.querySelector('[role="progressbar"]');
    }
    return false;
  }

  // 진행 막대 주변의 "카드" 정도 크기의 상위 요소를 찾는다.
  function findContainer(el) {
    let cur = el;
    for (let i = 0; i < 6 && cur?.parentElement; i++) {
      cur = cur.parentElement;
      const t = textOf(cur);
      if (t.length >= 10 && t.length <= 300 && t.includes("\n")) return cur;
    }
    return el?.parentElement ?? el;
  }

  function lines(el) {
    return textOf(el)
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  function labelFrom(el, container) {
    const labelledBy = el.getAttribute?.("aria-labelledby");
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      const refText = textOf(ref);
      if (refText && refText.length <= 60) return refText;
    }
    const aria = el.getAttribute?.("aria-label");
    if (aria && aria.length <= 60) return aria;
    const candidate = lines(container).find(
      (l) => !l.includes("%") && !RESET_WORDS.test(l) && l.length >= 2 && l.length <= 40
    );
    return candidate ?? "";
  }

  function resetFrom(container) {
    return lines(container).find((l) => RESET_WORDS.test(l) && l.length <= 80) ?? null;
  }

  function dedupeItems(items) {
    const seen = new Set();
    return items.filter((item) => {
      const key = `${item.label}|${item.percent}|${item.resetText ?? ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function extract() {
    if (!document.body) return [];
    const items = [];

    document.querySelectorAll('[role="progressbar"]').forEach((el) => {
      if (!isVisibleElement(el)) return;
      const now = parseFloat(el.getAttribute("aria-valuenow"));
      if (!Number.isFinite(now)) return;
      const max = parseFloat(el.getAttribute("aria-valuemax") || "100");
      const percent = max > 0 ? (now / max) * 100 : now;
      const container = findContainer(el);
      items.push({
        label: labelFrom(el, container),
        percent,
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    });
    if (items.length > 0) return dedupeItems(items);

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const m = node.textContent.match(/(\d{1,3}(?:[.,]\d+)?)\s*%/);
      if (!m) continue;
      const parent = node.parentElement;
      if (!isVisibleElement(parent)) continue;
      const container = findContainer(parent);
      items.push({
        label: labelFrom(parent, container),
        percent: parseFloat(m[1].replace(",", ".")),
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    }
    return dedupeItems(items);
  }

  function send(payload) {
    try {
      chrome.runtime.sendMessage({ type: "gemini-usage", url: location.href, ...payload });
    } catch {
      // 확장이 다시 로드된 경우 등
    }
  }

  let finished = false;
  let lastJson = "";
  let settleTimer = null;
  let observer = null;

  function finish(payload) {
    if (finished) return;
    finished = true;
    clearTimeout(settleTimer);
    clearTimeout(timeoutTimer);
    clearInterval(pollTimer);
    observer?.disconnect();
    send(payload);
  }

  function check() {
    if (finished) return;
    if (isLoginRedirect()) {
      finish({ items: [], error: "Gemini 로그인이 필요해요 (크롬에서 로그인했는지 확인)" });
      return;
    }
    const items = extract();
    if (items.length === 0) {
      lastJson = "";
      clearTimeout(settleTimer);
      return;
    }
    const json = JSON.stringify(items);
    if (json !== lastJson) {
      lastJson = json;
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => finish({ items }), SETTLE_MS);
    }
  }

  const timeoutTimer = setTimeout(() => {
    finish({ items: [], error: "Gemini 사용량 화면에서 숫자를 찾지 못했어요" });
  }, MAX_WAIT_MS);

  const pollTimer = setInterval(check, 1000);
  if (document.body) {
    observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  check();
})();
