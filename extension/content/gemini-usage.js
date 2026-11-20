// gemini.google.com/usage (및 /u/*/usage) 화면에서 사용량 숫자를 읽어 확장 백그라운드로 보낸다.
// 1) 진행 막대(role="progressbar", <progress>) 값, 2) "40%" 같은 텍스트 순서로 찾으며,
// Shadow DOM 내부 요소와 백그라운드 탭(document.hidden) 상태에서도 안정적으로 동작한다.

(() => {
  const USAGE_PATH_RE = /^\/(?:u\/\d+\/)?usage\b/;
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
    if (el.closest?.('[aria-hidden="true"], [hidden], script, style, noscript, svg')) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    // 백그라운드 탭이나 최소화 창에서는 Chrome이 레이아웃 크기를 0x0으로 반환할 수 있어 크기 검사를 생략한다.
    if (document.hidden) return true;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }

  // Light DOM과 열린 Shadow DOM을 모두 탐색한다.
  function queryAllDeep(root, selector, out = []) {
    if (!root?.querySelectorAll) return out;
    for (const el of root.querySelectorAll(selector)) {
      out.push(el);
    }
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) queryAllDeep(el.shadowRoot, selector, out);
    }
    return out;
  }

  function walkTextDeep(root, visitor) {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      visitor(node);
    }
    if (root.querySelectorAll) {
      for (const el of root.querySelectorAll("*")) {
        if (el.shadowRoot) walkTextDeep(el.shadowRoot, visitor);
      }
    }
  }

  function isLoginRedirect() {
    if (location.hostname !== "gemini.google.com" || !USAGE_PATH_RE.test(location.pathname)) {
      return true;
    }
    const signInLink = document.querySelector('a[href*="accounts.google.com"]');
    if (signInLink && isVisibleElement(signInLink) && LOGIN_WORDS.test(textOf(signInLink))) {
      return queryAllDeep(document, '[role="progressbar"], progress[value]').length === 0;
    }
    return false;
  }

  function parentOf(el) {
    if (!el) return null;
    if (el.parentElement) return el.parentElement;
    const root = el.getRootNode?.();
    return root && root.host ? root.host : null;
  }

  // 진행 막대 주변의 "카드" 정도 크기의 상위 요소를 찾는다.
  function findContainer(el) {
    let cur = el;
    for (let i = 0; i < 6; i++) {
      const next = parentOf(cur);
      if (!next) break;
      cur = next;
      const t = textOf(cur);
      if (t.length >= 10 && t.length <= 300 && t.includes("\n")) return cur;
    }
    return parentOf(el) ?? el;
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
      const root = el.getRootNode?.() || document;
      const ref = root.getElementById?.(labelledBy) || document.getElementById(labelledBy);
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

    for (const el of queryAllDeep(document, '[role="progressbar"], progress[value]')) {
      if (!isVisibleElement(el)) continue;
      const rawNow = el.tagName === "PROGRESS" ? el.getAttribute("value") : el.getAttribute("aria-valuenow");
      const now = parseFloat(rawNow);
      if (!Number.isFinite(now)) continue;
      const rawMax = el.tagName === "PROGRESS" ? el.getAttribute("max") : el.getAttribute("aria-valuemax");
      const max = parseFloat(rawMax || "100");
      const percent = max > 0 ? (now / max) * 100 : now;
      const container = findContainer(el);
      items.push({
        label: labelFrom(el, container),
        percent,
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    }
    if (items.length > 0) return dedupeItems(items);

    walkTextDeep(document.body, (node) => {
      const m = node.textContent.match(/(\d{1,3}(?:[.,]\d+)?)\s*%/);
      if (!m) return;
      const parent = node.parentElement;
      if (!isVisibleElement(parent)) return;
      const container = findContainer(parent);
      items.push({
        label: labelFrom(parent, container),
        percent: parseFloat(m[1].replace(",", ".")),
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    });
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
      finish({ items: [], error: `Gemini 로그인이 필요해요 (${location.pathname})` });
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
    finish({ items: [], error: `Gemini 사용량 화면에서 숫자를 찾지 못했어요 (${location.pathname})` });
  }, MAX_WAIT_MS);

  const pollTimer = setInterval(check, 1000);
  if (document.body) {
    observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }
  check();
})();
