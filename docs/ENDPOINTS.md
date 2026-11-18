# 내부 API 확인하고 고치는 방법

세 서비스 모두 공식 사용량 API가 없어서, 웹사이트가 내부적으로 쓰는 요청을 따라 합니다.
서비스가 바뀌어 위젯에 "형식이 바뀌었을 수 있어요" 같은 오류가 뜨면 아래 순서로 확인하세요.

## 공통: 개발자 도구로 요청 찾기

1. 크롬에서 사용량 페이지를 엽니다.
2. `F12` → **Network** 탭 → 필터에서 **Fetch/XHR** 선택
3. `F5`로 새로고침
4. 요청 목록을 하나씩 눌러 **Response** 또는 **Preview** 탭에서 퍼센트 숫자가 들어 있는 요청을 찾습니다.
5. 그 요청의 주소(Headers → Request URL)와 응답 모양을 기록합니다.

## Claude

- 페이지: `https://claude.ai/settings/usage`
- 코드가 쓰는 요청
  1. `GET https://claude.ai/api/organizations` → 조직 목록 (`uuid`, `capabilities`)
  2. `GET https://claude.ai/api/organizations/{uuid}/usage`
- 기대하는 응답
  ```json
  { "five_hour": { "utilization": 42, "resets_at": "..." },
    "seven_day": { "utilization": 71, "resets_at": "..." } }
  ```
- 고칠 곳: 주소는 `extension/providers/claude.js`, 응답 해석은 `extension/lib/normalize.js`의 `normalizeClaude`
- 조직이 여러 개라 엉뚱한 조직이 잡히면 `claudeOrgId` 설정을 쓰도록 되어 있습니다.

## ChatGPT

- 페이지: `https://chatgpt.com/codex/settings/usage` (12월부터는 설정 → 사용량에 통합 표시 예정)
- 코드가 쓰는 요청
  1. `GET https://chatgpt.com/api/auth/session` → `accessToken`
  2. `GET https://chatgpt.com/backend-api/wham/usage` (헤더 `Authorization: Bearer {토큰}`)
- 기대하는 응답
  ```json
  { "rate_limit": {
      "primary_window":   { "used_percent": 12, "limit_window_seconds": 18000,  "reset_at": 1763560800 },
      "secondary_window": { "used_percent": 93, "limit_window_seconds": 604800, "reset_after_seconds": 86400 } } }
  ```
- 형식이 조금 바뀌어도 `used_percent`가 들어 있는 객체를 자동으로 찾도록 되어 있습니다.
- 12월 통합 후에는 **설정 → 사용량** 페이지에서 위 방법으로 새 요청을 찾아 `extension/providers/chatgpt.js`의 주소를 바꾸고, 필요하면 `normalizeChatGPT`를 고치세요.

## Gemini

- 페이지: `https://gemini.google.com/usage`
- Google 웹앱은 `batchexecute`라는 특수한 형식으로 데이터를 주고받아 JSON처럼 읽기 어렵습니다.
  그래서 지금은 화면에 그려진 값을 읽습니다 (`extension/content/gemini-usage.js`).
  1. `role="progressbar"` 요소의 `aria-valuenow` 값
  2. 없으면 화면 글자 중 `40%` 같은 형식
- 정확도를 높이는 방법
  1. 사용량 페이지에서 퍼센트가 표시된 부분을 오른쪽 클릭 → **검사**
  2. 해당 요소의 태그, 클래스, `aria-label`을 확인
  3. `gemini-usage.js`의 `extract()`에서 그 선택자를 먼저 쓰도록 바꾸기
- 화면이 "남은 양"을 보여 주면, 라벨에 "남음/remaining"이 있을 때 자동으로 사용량으로 바꿉니다.

## 확장을 고친 뒤

`chrome://extensions` → 이 확장의 새로고침 버튼 → 확장 아이콘 → **지금 새로고침**.
오류 내용은 확장 카드의 **서비스 워커** 링크를 눌러 콘솔에서 볼 수 있습니다.
