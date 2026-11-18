// gemini.google.com/usage 화면에서 사용량 숫자를 읽어 확장 백그라운드로 보낸다.
// 화면 구조는 Google이 언제든 바꿀 수 있어서, 특정 클래스 이름에 의존하지 않고
// 1) 진행 막대(role="progressbar")의 aria 값, 2) "40%" 같은 글자 순서로 찾는다.
// 실제 화면을 보고 더 정확한 선택자로 바꾸면 안정성이 올라간다 (docs/ENDPOINTS.md 참고).

(() => {
  const RESET_WORDS = /(재설정|초기화|리셋|갱신|reset|refresh)/i;
  const REMAINING_WORDS = /(남음|남은|잔여|remaining|left)/i;
  const MAX_WAIT_MS = 20000;

  function textOf(el) {
    return (el?.innerText || el?.textContent || "").trim();
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

  function extract() {
    const items = [];

    document.querySelectorAll('[role="progressbar"]').forEach((el) => {
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
    if (items.length > 0) return items;

    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const m = node.textContent.match(/(\d{1,3}(?:[.,]\d+)?)\s*%/);
      if (!m) continue;
      const parent = node.parentElement;
      if (!parent || parent.closest("script,style,noscript")) continue;
      const container = findContainer(parent);
      items.push({
        label: labelFrom(parent, container),
        percent: parseFloat(m[1].replace(",", ".")),
        resetText: resetFrom(container),
        isRemaining: REMAINING_WORDS.test(textOf(container)),
      });
    }
    return items;
  }

  function send(payload) {
    try {
      chrome.runtime.sendMessage({ type: "gemini-usage", url: location.href, ...payload });
    } catch {
      // 확장이 다시 로드된 경우 등
    }
  }

  const started = Date.now();
  let lastJson = "";
  const timer = setInterval(() => {
    const items = extract();
    const json = JSON.stringify(items);
    // 값이 두 번 연속 같으면 화면 로딩이 끝났다고 보고 보낸다.
    if (items.length > 0 && json === lastJson) {
      clearInterval(timer);
      send({ items });
      return;
    }
    lastJson = json;
    if (Date.now() - started > MAX_WAIT_MS) {
      clearInterval(timer);
      send({ items: [], error: "Gemini 사용량 화면에서 숫자를 찾지 못했어요" });
    }
  }, 1000);
})();
