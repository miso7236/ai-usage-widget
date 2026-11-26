"""앱 안에서 업데이트하기.

흐름
1. GitHub Releases의 최신 버전을 확인한다.
2. 새 버전이면 AIUsageWidget.exe를 %LOCALAPPDATA%\\AIUsageWidget\\update 에 받고 크기·해시를 검증한다.
3. 받은 새 exe를 `--finish-update <기존 exe 경로>` 로 실행하고 기존 위젯은 종료한다.
4. 새 exe가 기존 exe 자리에 자기 자신을 복사하고, 그 자리에서 다시 실행한다.
5. 새 위젯이 켜지면 exe 안에 들어 있는 크롬 확장 파일을
   %LOCALAPPDATA%\\AIUsageWidget\\extension 에 덮어쓴다. 크롬 확장은 위젯이 알려 주는
   버전이 자기보다 높으면 스스로 다시 로드한다.

exe 실행 중에는 자기 파일을 덮어쓸 수 없어서 3~4단계처럼 "새 exe가 옛 exe를 덮어쓰는" 방식을 쓴다.
tkinter에 의존하지 않아서 테스트할 수 있다.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import local_token
from version import REPO, __version__

EXE_ASSET_NAME = "AIUsageWidget.exe"
USER_AGENT = f"ai-usage-widget/{__version__}"


class UpdateError(Exception):
    pass


# ---------------- 버전 비교 ----------------
def parse_version(text: str) -> tuple[int, ...]:
    """'v0.10.2' → (0, 10, 2). 숫자가 아닌 부분(-beta 등)은 무시한다."""
    text = (text or "").strip().lstrip("vV")
    parts = []
    for piece in text.split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


# ---------------- 경로 ----------------
def is_frozen() -> bool:
    """PyInstaller로 만든 exe로 실행 중인지"""
    return bool(getattr(sys, "frozen", False))


def current_exe() -> Path | None:
    return Path(sys.executable) if is_frozen() else None


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "AIUsageWidget" if base else Path.home() / ".ai-usage-widget"


def update_dir() -> Path:
    return app_data_dir() / "update"


def app_icon_path() -> Path | None:
    """앱 아이콘(.ico) 파일. exe 안에 함께 묶여 있고, 소스로 실행하면 widget/assets에 있다. 없으면 None."""
    if is_frozen():
        path = Path(getattr(sys, "_MEIPASS", ".")) / "assets" / "app.ico"
    else:
        path = Path(__file__).resolve().parent / "assets" / "app.ico"
    return path if path.is_file() else None


def bundled_extension_dir() -> Path:
    """exe 안에 함께 묶인 확장(소스로 실행하면 저장소의 extension 폴더)"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", ".")) / "extension"
    return Path(__file__).resolve().parent.parent / "extension"


def chrome_extension_dir() -> Path:
    """크롬에 '압축해제된 확장 프로그램'으로 로드해야 하는 폴더"""
    return app_data_dir() / "extension" if is_frozen() else bundled_extension_dir()


def read_manifest_version(folder: Path) -> str | None:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


# ---------------- 크롬 확장 파일 동기화 ----------------
def sync_extension(src: Path, dst: Path) -> bool:
    """src 확장이 dst보다 새것이거나 dst가 없으면 복사한다. 복사했으면 True."""
    src_ver = read_manifest_version(src)
    if src_ver is None:
        return False
    dst_ver = read_manifest_version(dst)
    if dst_ver is not None and not is_newer(src_ver, dst_ver):
        return False
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)  # 지워진 파일이 남지 않도록
    # 개발 PC에서 만든 exe에 개발용 토큰 파일이 섞여 들어가도 복사하지 않는다.
    shutil.copytree(
        src, dst, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(local_token.EXTENSION_TOKEN_FILE, "*.tmp"),
    )
    return True


def sync_bundled_extension(token: str | None = None) -> bool:
    """exe에 묶인 확장을 크롬 확장 폴더로 복사하고, 그 폴더에 로컬 토큰 파일을 쓴다.

    소스로 실행하면 복사는 하지 않고 저장소의 extension 폴더에 토큰 파일만 쓴다.
    복사했으면 True.
    """
    copied = False
    if is_frozen():
        try:
            copied = sync_extension(bundled_extension_dir(), chrome_extension_dir())
        except OSError:
            copied = False
    local_token.write_extension_token(chrome_extension_dir(), token or local_token.get_token())
    return copied


# ---------------- 최신 버전 확인 ----------------
@dataclass
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    page_url: str
    exe_url: str | None
    exe_size: int | None
    exe_sha256: str | None


def parse_release(data: dict) -> ReleaseInfo:
    if not isinstance(data, dict) or not data.get("tag_name"):
        raise UpdateError("릴리스 정보를 읽지 못했어요")
    exe = next(
        (a for a in data.get("assets") or [] if a.get("name") == EXE_ASSET_NAME),
        None,
    )
    sha = None
    if exe and isinstance(exe.get("digest"), str) and exe["digest"].startswith("sha256:"):
        sha = exe["digest"].split(":", 1)[1].lower()
    return ReleaseInfo(
        version=data["tag_name"].lstrip("vV"),
        tag=data["tag_name"],
        notes=(data.get("body") or "").strip(),
        page_url=data.get("html_url") or f"https://github.com/{REPO}/releases",
        exe_url=exe.get("browser_download_url") if exe else None,
        exe_size=exe.get("size") if exe else None,
        exe_sha256=sha,
    )


def _http_get(url: str, timeout: float):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest(timeout: float = 10, get=_http_get) -> ReleaseInfo:
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        with get(url, timeout) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("아직 올라온 릴리스가 없어요") from e
        raise UpdateError(f"GitHub 오류 (HTTP {e.code})") from e
    except (OSError, ValueError) as e:
        raise UpdateError(f"업데이트 정보를 가져오지 못했어요 ({e})") from e
    return parse_release(data)


# ---------------- 다운로드 ----------------
def download(
    info: ReleaseInfo,
    dest: Path,
    progress: Callable[[int], None] | None = None,
    get=_http_get,
    timeout: float = 30,
) -> Path:
    if not info.exe_url:
        raise UpdateError(f"릴리스에 {EXE_ASSET_NAME} 파일이 없어요")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    sha = hashlib.sha256()
    received = 0
    try:
        with get(info.exe_url, timeout) as res, open(part, "wb") as f:
            total = info.exe_size or int(res.headers.get("Content-Length") or 0)
            while True:
                chunk = res.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                received += len(chunk)
                if progress and total:
                    progress(min(100, received * 100 // total))
    except OSError as e:
        part.unlink(missing_ok=True)
        raise UpdateError(f"다운로드에 실패했어요 ({e})") from e

    if info.exe_size is not None and received != info.exe_size:
        part.unlink(missing_ok=True)
        raise UpdateError("받은 파일 크기가 맞지 않아요. 다시 시도해 주세요")
    if info.exe_sha256 and sha.hexdigest() != info.exe_sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("받은 파일이 손상됐어요 (해시 불일치)")
    os.replace(part, dest)
    return dest


# ---------------- 교체 ----------------
def _detached_flags() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def launch_finisher(new_exe: Path, target_exe: Path) -> None:
    """새 exe를 실행해 기존 exe를 덮어쓰게 한다. 호출한 뒤 현재 앱은 바로 종료해야 한다."""
    subprocess.Popen(
        [str(new_exe), "--finish-update", str(target_exe)],
        creationflags=_detached_flags(),
        close_fds=True,
    )


def finish_update(target: Path, self_exe: Path, timeout: float = 60, sleep=time.sleep) -> bool:
    """(새 exe 안에서 실행) 기존 exe가 종료되길 기다렸다가 덮어쓰고 다시 실행한다."""
    deadline = time.monotonic() + timeout
    ok = False
    while True:
        try:
            shutil.copy2(self_exe, target)
            ok = True
            break
        except OSError:
            if time.monotonic() >= deadline:
                break
            sleep(1)
    # 성공하면 새 버전이, 실패해도 기존 버전이 다시 켜진다.
    subprocess.Popen([str(target)], creationflags=_detached_flags(), close_fds=True)
    return ok


def cleanup_update_dir(retries: int = 10, sleep=time.sleep) -> None:
    """업데이트에 썼던 임시 exe를 지운다 (방금 끝난 프로세스가 잡고 있을 수 있어 몇 번 재시도)."""
    folder = update_dir()
    for _ in range(retries):
        if not folder.exists():
            return
        try:
            shutil.rmtree(folder)
            return
        except OSError:
            sleep(1)
