# AI 사용량 위젯

Claude, ChatGPT, Gemini 구독 사용량(5시간·주간 한도)을 Windows 화면 구석에 항상 띄워 주는 프로그램입니다.

```
AI 사용량            갱신 23:10  ×
Claude
  5시간  ████░░░░░  42%  2시간 13분 후
  주간   ███████░░  71%  3일 4시간 후
ChatGPT
  5시간  █░░░░░░░░  12%  4시간 0분 후
  주간   █████████  93%  1일 0시간 후
```

## 구조

위젯은 화면에 그리는 일을 하고, 사용량 숫자는 아래 네 가지 경로 중 켜 둔 곳에서 받습니다.

```
웹 사용량 (둘 중 하나 선택)
  ├ 크롬 확장 (extension/)         ── POST 127.0.0.1:47823/update ─┐
  └ 내장 브라우저 (WebView2)        ── 위젯이 직접 실행해서 읽음   ─┤
로컬 조회 (선택)                                                    ├─→ 항상 위에 떠 있는 작은 창
  ├ Claude 로컬 OAuth (Claude Code) ── api.anthropic.com 조회      ─┤
  └ Antigravity                    ── 로컬 언어 서버 조회         ─┘
```

| 경로 | 대상 | 필요한 것 |
|---|---|---|
| 크롬 확장 (기본) | Claude, ChatGPT, Gemini | 크롬에 확장 설치, 크롬 실행 중 |
| 내장 브라우저 | Claude, ChatGPT, Gemini | 설정에서 서비스별로 한 번 로그인 |
| Claude 로컬 OAuth | Claude | 이 PC에 로그인된 Claude Code (또는 Hermes) |
| Antigravity | Antigravity의 기본 Gemini 용량 | Antigravity 실행 중 |

- **크롬 확장**은 평소 쓰는 크롬의 로그인을 그대로 써서 사용량을 가져옵니다. 로그인 정보를 따로 저장하거나 외부로 보내지 않습니다. 위젯이 꺼져 있어도 확장 아이콘의 배지에 가장 높은 사용률이 표시됩니다.
- **내장 브라우저**는 크롬을 켜지 않아도 위젯이 주기적으로 사용량을 가져옵니다. 로그인 세션은 `%LOCALAPPDATA%\AIUsageWidget\browser-profile`에만 저장됩니다.
- **Claude 로컬 OAuth**는 Claude Code의 자격증명 파일을 읽기만 합니다. 토큰을 갱신하거나 덮어쓰지 않으며, `api.anthropic.com` 외에는 보내지 않습니다.
- **Antigravity**는 같은 PC에서 실행 중인 Antigravity의 로컬 주소(127.0.0.1)만 조회합니다.

## 설치

### 1. 위젯 실행

1. [Releases](https://github.com/miso7236/ai-usage-widget/releases/latest)에서 `AIUsageWidget.exe`를 받습니다.
2. 원하는 폴더(예: `문서\AIUsageWidget`)에 두고 실행합니다.
   서명이 없는 프로그램이라 SmartScreen 경고가 뜨면 "추가 정보 → 실행"을 누르세요.

### 2. 사용량 가져오는 방법 고르기

위젯 오른쪽 클릭 → **설정…** → **수집·계정** 탭에서 고릅니다. 둘 중 하나만 하면 됩니다.

**방법 A. 내장 브라우저 (크롬 확장 없이 사용)**

1. "웹 사용량 수집 방식"에서 **내장 브라우저 (WebView2)** 를 고릅니다.
2. **Claude 로그인…**, **ChatGPT 로그인…**, **Gemini 로그인…** 중 쓰는 서비스를 눌러 한 번씩 로그인합니다.
3. 자동 수집 주기를 고릅니다 (기본 3분).

**방법 B. 크롬 확장 (기본)**

1. 설정의 **크롬 확장 폴더 열기**를 누릅니다 (폴더 경로가 클립보드에 복사됩니다).
2. 크롬 주소창에 `chrome://extensions` 입력 → 오른쪽 위 **개발자 모드** 켜기
3. **압축해제된 확장 프로그램을 로드합니다** → 1번에서 열린 폴더 선택
   (`%LOCALAPPDATA%\AIUsageWidget\extension`)
4. 크롬에서 claude.ai, chatgpt.com, gemini.google.com에 로그인되어 있는지 확인
5. 확장 아이콘을 눌러 **지금 새로고침**

이 폴더로 설치해야 위젯을 업데이트할 때 확장도 자동으로 업데이트됩니다.

### 3. Windows 시작 시 자동 실행 (선택)

설정 → **시스템·정보** 탭 → **Windows 시작 시 자동 실행**을 켭니다.

### 소스 코드로 실행 (개발용)

```
python widget\widget.py --demo      # 예시 데이터로 화면 확인
widget\run_widget.bat               # 콘솔 창 없이 실행
```

- 위젯 본체는 Python 기본 라이브러리만 씁니다. 내장 브라우저 기능만 `pip install pywebview`가 필요합니다.
- 크롬 확장은 저장소의 `extension` 폴더로 설치하고, `git pull`로 업데이트합니다.

## 업데이트

- 위젯이 켜질 때와 6시간마다 새 버전을 확인합니다. 새 버전이 있으면 제목 옆에 **⬆ v0.3.0** 같은 표시가 뜹니다.
- 표시를 누르거나 설정 → 시스템·정보 → **업데이트 확인**을 누르면, 내려받고 → 위젯이 잠깐 꺼졌다가 → 새 버전으로 다시 켜집니다.
- 크롬 확장은 위젯이 새 파일을 확장 폴더에 넣으면 몇 분 안에 스스로 다시 로드됩니다.
- 받은 파일은 GitHub Release에 기록된 크기와 SHA-256 해시로 검증합니다.

## 사용법

| 동작 | 방법 |
|---|---|
| 위젯 이동 | 드래그 (위치는 자동 저장) |
| 크기 조절 | 위젯 위에서 마우스 휠, 또는 오른쪽 아래 모서리 드래그 |
| 컴팩트 모드 전환 | 위젯 더블클릭, 또는 오른쪽 클릭 메뉴 |
| 메뉴 | 오른쪽 클릭 → 지금 새로고침, 컴팩트 모드, 클릭 통과, 트레이로 숨기기, 설정, 종료 |
| 트레이 아이콘 | 위젯 숨기기/표시, 클릭 통과 해제, 새로고침, 설정, 종료 |
| 즉시 갱신 | 오른쪽 클릭 → 지금 새로고침 (크롬 확장 아이콘에서도 가능) |
| 서비스 끄기, 조회 간격, 알림 기준 | 크롬 확장 아이콘 → 설정 |

설정 창은 세 탭으로 나뉩니다.

| 탭 | 내용 |
|---|---|
| 표시 | 컴팩트 모드, 페이스 기준값, Antigravity 표시, 투명 배경, 주변 색 자동 보정, 항상 위, 투명도, 크기, 글꼴, 위치 |
| 수집·계정 | 크롬 확장 / 내장 브라우저 선택, 서비스별 로그인, 수집 주기, Claude 계정(웹 / 로컬 OAuth) |
| 시스템·정보 | 자동 실행, 업데이트, 진단 정보 복사, 로그 폴더 열기 |

색상: 70% 미만 초록, 70% 이상 주황, 90% 이상 빨강. 15분 넘게 갱신되지 않으면 "(오래됨)"으로 흐리게 표시됩니다.

**페이스 기준값**은 `42/38%`처럼 "지금 사용률 / 경과 시간만큼 썼다면 적정한 사용률"을 함께 보여 줍니다. 앞 숫자가 더 크면 한도를 빨리 쓰고 있다는 뜻입니다.

## 문제가 생겼을 때

1. 설정 → **시스템·정보** 탭 → **진단 정보 복사**를 누릅니다.
2. [이슈](https://github.com/miso7236/ai-usage-widget/issues)를 열고 그대로 붙여 넣습니다.

진단 정보에는 위젯 버전, Windows 버전, 설정, 서비스별 조회 상태와 오류 문구, 최근 로그 40줄이 들어갑니다. 토큰, 계정 식별자(UUID·이메일), 사용자 폴더 이름은 가려집니다.

서비스 응답 형식이 바뀌어 해석하지 못하면, 받은 응답의 **구조**도 함께 남깁니다. 값은 지우고 키 이름과 종류(문자열·날짜·숫자 등)만 남기며, 어느 필드가 사용률인지 알 수 있도록 0~100 사이 숫자만 값을 남깁니다.

```
chatgpt: 조회 실패 - ChatGPT 응답에서 사용량을 찾지 못했어요 | 응답 구조: {"usage":{"codex":{"percent_used":"37","resets":"date","window":"string"}}}
```

| 저장 위치 | 내용 |
|---|---|
| `%APPDATA%\AIUsageWidget\config.json` | 위젯 설정 |
| `%APPDATA%\AIUsageWidget\widget.log` | 로그 (512KB마다 교체, 2개 보관) |
| `%LOCALAPPDATA%\AIUsageWidget\browser-profile` | 내장 브라우저 로그인 세션 |
| `%LOCALAPPDATA%\AIUsageWidget\extension` | 크롬에 로드하는 확장 폴더 |

## 서비스별 방식과 한계

| 서비스 | 가져오는 방법 | 상태 |
|---|---|---|
| Claude (웹) | `claude.ai/api/organizations/{조직}/usage` | 여러 오픈소스 앱이 쓰는 방식 |
| Claude (로컬 OAuth) | Claude Code 토큰으로 `api.anthropic.com/api/oauth/usage` 호출 | 토큰이 만료되면 Claude Code를 한 번 실행해야 함 |
| ChatGPT | `chatgpt.com/api/auth/session`에서 토큰을 받아 `backend-api/wham/usage` 호출 | Codex 사용량 기준. 12월 ChatGPT·Codex 통합 후 바뀔 수 있음 |
| Gemini | `gemini.google.com/usage` 화면에서 숫자를 읽음 | **실험적.** 화면 구조에 따라 조정 필요 |
| Antigravity | 실행 중인 Antigravity 언어 서버의 로컬 RPC | 기본 Gemini 용량만 표시 |

- 모두 **공식 API가 아닙니다.** 서비스가 바뀌면 동작하지 않을 수 있고, 그럴 때 위젯에 오류 문구가 표시되고 로그에 기록됩니다.
- 실제 응답을 확인하고 코드를 고치는 방법은 [docs/ENDPOINTS.md](docs/ENDPOINTS.md)에 있습니다.
- 내 사용량을 몇 분에 한 번 읽기만 하지만, 각 서비스 약관이 내부 API 자동 호출을 공식적으로 허용하지는 않습니다. 조회 간격을 너무 짧게 하지 마세요.

## 개발

```
python -m unittest discover -s widget/tests -v   # 위젯 테스트
npm test                                          # 확장 테스트 (Node 22)
```

서비스 응답 해석 규칙은 두 곳에 같은 내용으로 있습니다. 한쪽을 고치면 다른 쪽도 똑같이 고쳐야 합니다.

- 크롬 확장: `extension/lib/normalize.js`
- 내장 브라우저: `widget/web_normalize.py`

[tests/fixtures/normalize](tests/fixtures/normalize)의 공용 샘플을 두 구현에 똑같이 돌리므로, 한쪽만 고치면 테스트가 실패합니다. 서비스 응답이 바뀌면 새 응답(토큰·식별자는 지운 것)으로 샘플을 하나 추가하고 두 구현을 고칩니다. 자세한 형식은 [샘플 안내](tests/fixtures/normalize/README.md)에 있습니다.

| 파일 | 역할 |
|---|---|
| `extension/lib/normalize.js` | 서비스별 응답을 공통 형식으로 바꾸기 (여기를 가장 자주 고치게 됨) |
| `extension/providers/*.js` | 서비스별 요청 |
| `extension/content/gemini-usage.js` | Gemini 화면에서 숫자 읽기 |
| `extension/background.js` | 주기적 조회, 저장, 위젯 전송, 배지 |
| `extension/lib/notifications.js`, `history.js` | 임계치·리셋 알림, 사용량 기록과 소진 예측 |
| `widget/widget.py` | 화면 (tkinter), 수집 경로 조율 |
| `widget/state.py` | 데이터 모델, 남은 시간·페이스 계산, 설정 저장 |
| `widget/server.py` | 확장 데이터를 받는 로컬 서버 |
| `widget/browser_worker.py` | 내장 브라우저(WebView2) 로그인·수집 |
| `widget/web_normalize.py` | 내장 브라우저용 응답 해석 (`normalize.js`와 같은 규칙) |
| `widget/claude_oauth.py` | 로컬 OAuth 자격증명으로 Claude 사용량 조회 |
| `widget/antigravity.py` | Antigravity 로컬 언어 서버 조회 |
| `widget/settings_window.py`, `context_menu.py` | 설정 창, 오른쪽 클릭 메뉴 |
| `widget/tray.py`, `autostart.py` | 트레이 아이콘·클릭 통과·작업 표시줄 숨김, 자동 실행 |
| `widget/adaptive_color.py`, `screen_sampler.py` | 투명 배경에서 주변 색에 맞춘 글자색 보정 |
| `widget/diagnostics.py` | 파일 로그, 진단 정보 |
| `widget/updater.py` | 업데이트 확인, 다운로드·검증, exe 교체, 확장 폴더 동기화 |
| `widget/version.py` | 앱 버전 (manifest.json, package.json과 같아야 함) |

확장이 위젯에 보내는 형식:

```json
{
  "providers": [
    {
      "provider": "claude",
      "name": "Claude",
      "ok": true,
      "windows": [
        { "label": "5시간", "usedPercent": 42, "resetsAt": "2026-11-18T16:13:00Z", "resetText": null }
      ],
      "error": null,
      "fetchedAt": "2026-11-18T14:00:00Z"
    }
  ]
}
```

## GitHub

- `main` 브랜치에 push하면 GitHub Actions가 테스트를 돌리고 exe를 빌드합니다.
- 앱 버전이 바뀐 커밋이 `main`에 올라가면 **Release**가 자동으로 만들어지고, 사용자 위젯이 이 Release로 업데이트됩니다.

### 새 버전 배포하기

1. 세 곳의 버전을 똑같이 올립니다: `widget/version.py`, `extension/manifest.json`, `package.json`
   (테스트가 세 값이 같은지 확인합니다)
2. 커밋해서 `main`에 push합니다.
   ```
   git commit -am "v0.3.0"
   git push
   ```
3. Actions가 끝나면 Releases에 `v0.3.0`이 올라갑니다. 버전을 안 바꾼 push는 Release를 만들지 않습니다.

## 라이선스

[MIT](LICENSE)
