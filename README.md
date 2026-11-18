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

```
크롬 확장 (extension/)                    Windows 위젯 (widget/)
  ├ claude.ai 내부 API 조회     ─┐
  ├ chatgpt.com 내부 API 조회   ─┼─ POST 127.0.0.1:47823/update ─→  항상 위에 떠 있는 작은 창
  └ gemini.google.com/usage 화면 읽기 ─┘
```

- **확장**은 평소 쓰는 크롬의 로그인을 그대로 써서 사용량을 가져옵니다. 로그인 정보를 따로 저장하거나 외부로 보내지 않습니다.
- **위젯**은 확장이 보낸 숫자를 화면에 그리기만 합니다. Python 기본 라이브러리만 사용해서 추가 설치가 필요 없습니다.
- 위젯이 꺼져 있어도 확장 아이콘의 배지에 가장 높은 사용률이 표시됩니다.

## 설치

### 1. 위젯 실행

1. [Python](https://www.python.org/downloads/)을 설치합니다. (설치할 때 "Add python.exe to PATH" 체크)
2. 예시 데이터로 화면을 먼저 확인합니다.
   ```
   python widget\widget.py --demo
   ```
3. 실제로 쓸 때는 `widget\run_widget.bat`을 더블클릭합니다. (콘솔 창 없이 실행)

또는 GitHub Actions가 만든 `AIUsageWidget.exe`를 받아서 바로 실행해도 됩니다. (아래 "GitHub" 참고)

### 2. 크롬 확장 설치

1. 크롬 주소창에 `chrome://extensions` 입력
2. 오른쪽 위 **개발자 모드** 켜기
3. **압축해제된 확장 프로그램을 로드합니다** → 이 저장소의 `extension` 폴더 선택
4. 크롬에서 claude.ai, chatgpt.com, gemini.google.com에 로그인되어 있는지 확인
5. 확장 아이콘을 눌러 **지금 새로고침**

### 3. Windows 시작 시 자동 실행 (선택)

1. `Win + R` → `shell:startup` 입력
2. 열린 폴더에 `run_widget.bat`의 바로 가기를 만들어 넣습니다.

## 사용법

| 동작 | 방법 |
|---|---|
| 위젯 이동 | 드래그 (위치는 자동 저장) |
| 메뉴 | 오른쪽 클릭 → 항상 위, 투명도, 위치 초기화, 종료 |
| 즉시 갱신 | 확장 아이콘 → 지금 새로고침 |
| Gemini 갱신 | 확장 아이콘 → Gemini 갱신 (또는 gemini.google.com/usage를 열면 자동 반영) |
| 서비스 끄기, 조회 간격 | 확장 아이콘 → 설정 |

색상: 70% 미만 초록, 70% 이상 주황, 90% 이상 빨강. 15분 넘게 갱신되지 않으면 "(오래됨)"으로 흐리게 표시됩니다.

## 서비스별 방식과 한계

| 서비스 | 가져오는 방법 | 상태 |
|---|---|---|
| Claude | `claude.ai/api/organizations/{조직}/usage` | 여러 오픈소스 앱이 쓰는 방식 |
| ChatGPT | `chatgpt.com/api/auth/session`에서 토큰을 받아 `backend-api/wham/usage` 호출 | Codex 사용량 기준. 12월 ChatGPT·Codex 통합 후 바뀔 수 있음 |
| Gemini | `gemini.google.com/usage` 화면을 최소화 창으로 열어 숫자를 읽음 | **실험적.** 화면 구조에 따라 조정 필요 |

- 모두 **공식 API가 아닙니다.** 서비스가 바뀌면 동작하지 않을 수 있고, 그럴 때 위젯에 오류 문구가 표시됩니다.
- 실제 응답을 확인하고 코드를 고치는 방법은 [docs/ENDPOINTS.md](docs/ENDPOINTS.md)에 있습니다.
- 내 사용량을 몇 분에 한 번 읽기만 하지만, 각 서비스 약관이 내부 API 자동 호출을 공식적으로 허용하지는 않습니다. 조회 간격을 너무 짧게 하지 마세요.

## 개발

```
python -m unittest discover -s widget/tests -v   # 위젯 테스트
npm test                                          # 확장 테스트 (Node 22)
```

| 파일 | 역할 |
|---|---|
| `extension/lib/normalize.js` | 서비스별 응답을 공통 형식으로 바꾸기 (여기를 가장 자주 고치게 됨) |
| `extension/providers/*.js` | 서비스별 요청 |
| `extension/content/gemini-usage.js` | Gemini 화면에서 숫자 읽기 |
| `extension/background.js` | 주기적 조회, 저장, 위젯 전송, 배지 |
| `widget/state.py` | 데이터 모델, 남은 시간 계산, 설정 저장 |
| `widget/server.py` | 확장 데이터를 받는 로컬 서버 |
| `widget/widget.py` | 화면 (tkinter) |

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

`main` 브랜치에 push하면 GitHub Actions가 테스트를 돌리고, Windows용 `AIUsageWidget.exe`와 확장 zip을 만듭니다. 저장소의 **Actions** 탭 → 최근 실행 → **Artifacts**에서 받을 수 있습니다.
