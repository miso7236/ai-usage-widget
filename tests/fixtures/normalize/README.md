# 응답 해석 공용 샘플

서비스 응답을 위젯 표준 형식으로 바꾸는 규칙은 두 곳에 있습니다.

- 크롬 확장: `extension/lib/normalize.js`
- 내장 브라우저: `widget/web_normalize.py`

이 폴더의 샘플은 두 구현에 똑같이 들어갑니다.

- JS: `tests/js/fixtures.test.js` (`npm test`)
- Python: `widget/tests/test_shared_fixtures.py` (`python -m unittest discover -s widget/tests`)

한쪽만 고치면 둘 중 하나가 실패합니다.

## 서비스 응답이 바뀌었을 때

1. 실제 응답을 [docs/ENDPOINTS.md](../../../docs/ENDPOINTS.md) 방법으로 확인합니다.
2. 토큰·이메일·조직 ID를 지운 응답으로 샘플 파일을 하나 추가합니다.
3. 두 구현을 고쳐서 두 테스트를 모두 통과시킵니다.

## 파일 형식

```json
{
  "description": "이 샘플이 확인하는 것",
  "provider": "claude | chatgpt | gemini",
  "now": "2026-11-18T14:00:00Z",
  "utcOffsetMinutes": 540,
  "input": { "서비스 응답 원본": "…" },
  "expected": [
    { "key": "five_hour", "label": "5시간", "usedPercent": 42, "resetsAt": "2026-11-18T16:13:00Z", "resetText": null }
  ]
}
```

- `now`: 해석 기준 시각. 상대 시간("2시간 후")과 `reset_after_seconds` 계산에 씁니다.
- `utcOffsetMinutes`: Gemini의 "오후 3:00" 같은 문구를 해석할 PC 시간대 (540 = 한국). 정시 단위만 됩니다. 없으면 UTC.
- `expected`: 표시될 구간 목록 (순서 포함). `resetsAt`은 같은 시각이면 표기 방식이 달라도 통과합니다.
- 오류를 기대하는 샘플은 `expected` 대신 `"error": "오류 문구 일부"`를 씁니다.
