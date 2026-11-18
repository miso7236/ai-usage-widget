// "0.10.2" 같은 버전 문자열 비교. a가 더 높으면 양수, 같으면 0, 낮으면 음수.
export function compareVersions(a, b) {
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

function parse(v) {
  return String(v ?? "")
    .trim()
    .replace(/^v/i, "")
    .split(".")
    .map((p) => parseInt(p, 10) || 0);
}

// 위젯이 알려 준 확장 버전을 보고 무엇을 할지 정한다 (순수 함수, 테스트용).
// - "reload": 디스크의 확장 파일이 더 새것 → 다시 로드
// - "manual": 이미 한 번 다시 로드했는데도 그대로 → 위젯이 관리하지 않는 폴더에서 로드된 것
// - "none":   할 일 없음
export function decideSelfUpdate(remoteVersion, localVersion, lastReloadFor) {
  if (!remoteVersion || compareVersions(remoteVersion, localVersion) <= 0) return "none";
  return lastReloadFor === remoteVersion ? "manual" : "reload";
}
