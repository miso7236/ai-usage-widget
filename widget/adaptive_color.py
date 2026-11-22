"""컴팩트 + 투명 배경(PNG) 모드의 주변색 기반 자동 색 보정 (순수 로직).

Tkinter나 Win32 API에 의존하지 않으므로 리눅스 CI에서도 완전히 단위 테스트할 수 있다.
- WCAG 2.1 상대 휘도·명암비 계산 및 HLS 명도 보정(`ensure_contrast`)
- 위젯 바깥 띠(top/bottom/left/right)에서 칸(슬롯)별 BGRA 픽셀 추출 및 중앙값 필터
- 슬롯별 히스테리시스 + 창 전체 크로마키(`#010101` ↔ `#fefefe`) 결정
- ~30fps 부드러운 RGB 지수 보간(`ColorAnimator`) 및 상태 통합(`AdaptiveColorController`)
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import Sequence

RGB = tuple[int, int, int]
Rect = tuple[int, int, int, int]  # (left, top, right, bottom)

CHROMA_KEY_DARK = "#010101"
CHROMA_KEY_LIGHT = "#fefefe"

FOREGROUND_ROLES: tuple[str, ...] = ("fg", "muted", "ok", "warn", "danger", "error", "accent")

BASE_PALETTE_ON_DARK: dict[str, str] = {
    "fg": "#ffffff",
    "muted": "#dadce0",
    "bar_bg": "#3c4043",
    "ok": "#81c995",
    "warn": "#fdd663",
    "danger": "#f28b82",
    "error": "#f28b82",
    "accent": "#8ab4f8",
}

BASE_PALETTE_ON_LIGHT: dict[str, str] = {
    "fg": "#1f1f1f",
    "muted": "#444746",
    "bar_bg": "#c4c7c5",
    "ok": "#137333",
    "warn": "#b06000",
    "danger": "#c5221f",
    "error": "#c5221f",
    "accent": "#0b57d0",
}


def parse_hex(color: str) -> RGB:
    """`#rrggbb` 또는 `#rgb` 문자열을 `(r, g, b)` 정수로 변환한다."""
    s = color.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"Invalid hex color: {color!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def to_hex(rgb: Sequence[float]) -> str:
    """`(r, g, b)` 값을 `#rrggbb` 소문자 16진수 문자열로 변환한다."""
    r = max(0, min(255, int(round(rgb[0]))))
    g = max(0, min(255, int(round(rgb[1]))))
    b = max(0, min(255, int(round(rgb[2]))))
    return f"#{r:02x}{g:02x}{b:02x}"


def _linearize_channel(v: int) -> float:
    c = max(0.0, min(1.0, v / 255.0))
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: RGB) -> float:
    """WCAG 2.1 기준 상대 휘도(0.0 ~ 1.0)를 계산한다."""
    r, g, b = rgb
    return (
        0.2126 * _linearize_channel(r)
        + 0.7152 * _linearize_channel(g)
        + 0.0722 * _linearize_channel(b)
    )


def contrast_ratio(a: RGB, b: RGB) -> float:
    """두 색 사이의 WCAG 2.1 명암비(1.0 ~ 21.0)를 반환한다."""
    la = relative_luminance(a)
    lb = relative_luminance(b)
    hi = max(la, lb)
    lo = min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _hls_to_rgb255(h: float, l: float, s: float) -> RGB:
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return (
        max(0, min(255, int(round(r * 255.0)))),
        max(0, min(255, int(round(g * 255.0)))),
        max(0, min(255, int(round(b * 255.0)))),
    )


def _search_lightness(
    h: float,
    s: float,
    start_l: float,
    target_l: float,
    bg_rgb: RGB,
    min_ratio: float,
) -> RGB | None:
    """색상(H)·채도(S)를 유지하며 `start_l`에서 `target_l` 쪽으로 최소 명도 이동을 이진 탐색한다."""
    extreme = _hls_to_rgb255(h, target_l, s)
    if contrast_ratio(extreme, bg_rgb) < min_ratio:
        return None
    lo = start_l
    hi = target_l
    best = extreme
    for _ in range(14):
        mid = (lo + hi) * 0.5
        cand = _hls_to_rgb255(h, mid, s)
        if contrast_ratio(cand, bg_rgb) >= min_ratio:
            best = cand
            hi = mid
        else:
            lo = mid
    return best


def ensure_contrast(
    fg_rgb: RGB,
    bg_rgb: RGB,
    min_ratio: float = 4.5,
    prefer_light: bool | None = None,
) -> RGB:
    """전경색이 배경색과 `min_ratio` 이상의 명암비를 갖도록 명도를 보정한다."""
    if contrast_ratio(fg_rgb, bg_rgb) >= min_ratio:
        return fg_rgb

    h, l, s = colorsys.rgb_to_hls(fg_rgb[0] / 255.0, fg_rgb[1] / 255.0, fg_rgb[2] / 255.0)
    if prefer_light is None:
        prefer_light = relative_luminance(bg_rgb) < 0.18

    directions = (1.0, 0.0) if prefer_light else (0.0, 1.0)
    for target_l in directions:
        found = _search_lightness(h, s, l, target_l, bg_rgb, min_ratio)
        if found is not None:
            return found

    # 이론상 sRGB의 모든 배경은 #ffffff 또는 #000000 중 하나와 >= 4.58 대비를 가진다.
    white = (255, 255, 255)
    black = (0, 0, 0)
    return white if contrast_ratio(white, bg_rgb) >= contrast_ratio(black, bg_rgb) else black


def avoid_chroma_key(rgb: RGB, chroma_key: str) -> RGB:
    """전경색이 투명 크로마키와 정확히 겹쳐 투명하게 뚫리는 현상을 방지한다."""
    key_rgb = parse_hex(chroma_key)
    if rgb != key_rgb:
        return rgb
    r, g, b = rgb
    if r >= 128:
        return (max(0, r - 2), max(0, g - 2), max(0, b - 2))
    return (min(255, r + 2), min(255, g + 2), min(255, b + 2))


def robust_bg_color(samples: Sequence[RGB]) -> RGB | None:
    """픽셀 샘플 목록에서 채널별 중앙값 RGB를 구한다 (이상치·테두리 노이즈에 강인)."""
    n = len(samples)
    if n == 0:
        return None
    mid = n // 2
    rs = sorted(p[0] for p in samples)
    gs = sorted(p[1] for p in samples)
    bs = sorted(p[2] for p in samples)
    if n % 2 == 1:
        return (rs[mid], gs[mid], bs[mid])
    return (
        (rs[mid - 1] + rs[mid]) // 2,
        (gs[mid - 1] + gs[mid]) // 2,
        (bs[mid - 1] + bs[mid]) // 2,
    )


def chroma_key_for(theme: str) -> str:
    """테마에 맞는 ClearType 가장자리 번짐 방지용 투명 기준색을 반환한다."""
    return CHROMA_KEY_LIGHT if theme == "light" else CHROMA_KEY_DARK


def slot_theme(
    bg_rgb: RGB,
    prev_theme: str = "dark",
    *,
    low: float = 0.42,
    high: float = 0.58,
) -> str:
    """단일 슬롯 배경의 상대 휘도와 히스테리시스로 `'dark'`/`'light'` 테마를 결정한다."""
    lum = relative_luminance(bg_rgb)
    if lum > high:
        return "light"
    if lum < low:
        return "dark"
    return prev_theme if prev_theme in ("dark", "light") else "dark"


def global_chroma_key(
    slot_luminances: Sequence[float],
    prev_key: str = CHROMA_KEY_DARK,
    *,
    low: float = 0.35,
    high: float = 0.65,
) -> str:
    """모든 슬롯의 평균 휘도와 넓은 히스테리시스로 창 전체 크로마키 1개를 결정한다."""
    if not slot_luminances:
        return prev_key
    avg = sum(slot_luminances) / len(slot_luminances)
    if avg > high:
        return CHROMA_KEY_LIGHT
    if avg < low:
        return CHROMA_KEY_DARK
    return prev_key


def default_palette_for_key(chroma_key: str = CHROMA_KEY_DARK) -> dict[str, str]:
    """배경 샘플이 없을 때 사용할 기본 투명 팔레트."""
    base = BASE_PALETTE_ON_LIGHT if chroma_key == CHROMA_KEY_LIGHT else BASE_PALETTE_ON_DARK
    pal = dict(base)
    pal["bg"] = chroma_key
    pal["grip_bg"] = chroma_key
    pal["border"] = chroma_key
    return pal


def build_slot_palette(
    theme: str,
    bg_rgb: RGB,
    chroma_key: str = CHROMA_KEY_DARK,
    *,
    min_contrast: float = 4.5,
) -> dict[str, str]:
    """슬롯 1개의 배경색과 테마에 맞춰 대비 보정된 팔레트를 생성한다."""
    base = BASE_PALETTE_ON_LIGHT if theme == "light" else BASE_PALETTE_ON_DARK
    prefer_light = theme == "dark"
    pal: dict[str, str] = {
        "bg": chroma_key,
        "grip_bg": chroma_key,
        "border": chroma_key,
        "bar_bg": to_hex(avoid_chroma_key(parse_hex(base["bar_bg"]), chroma_key)),
    }
    for role in FOREGROUND_ROLES:
        base_rgb = parse_hex(base[role])
        adj = ensure_contrast(base_rgb, bg_rgb, min_ratio=min_contrast, prefer_light=prefer_light)
        adj = avoid_chroma_key(adj, chroma_key)
        pal[role] = to_hex(adj)
    return pal


def ring_sample_rects(win_rect: Rect, monitor_rect: Rect, thickness: int = 5) -> dict[str, Rect | None]:
    """창 바깥 4면(top/bottom/left/right) 띠 사각형을 모니터 경계로 잘라 반환한다."""
    wl, wt, wr, wb = win_rect
    ml, mt, mr, mb = monitor_rect
    t = max(1, int(thickness))

    raw = {
        "top": (max(ml, wl), max(mt, wt - t), min(mr, wr), min(mb, wt)),
        "bottom": (max(ml, wl), max(mt, wb), min(mr, wr), min(mb, wb + t)),
        "left": (max(ml, wl - t), max(mt, wt), min(mr, wl), min(mb, wb)),
        "right": (max(ml, wr), max(mt, wt), min(mr, wr + t), min(mb, wb)),
    }
    out: dict[str, Rect | None] = {}
    for side, (l, top, r, b) in raw.items():
        out[side] = (l, top, r, b) if (r > l and b > top) else None
    return out


def outer_capture_box(win_rect: Rect, monitor_rect: Rect, thickness: int = 5) -> Rect | None:
    """창 바깥 띠 4면을 한 번의 BitBlt로 담을 수 있는 바운딩 박스를 반환한다."""
    wl, wt, wr, wb = win_rect
    ml, mt, mr, mb = monitor_rect
    t = max(1, int(thickness))
    bl = max(ml, wl - t)
    bt = max(mt, wt - t)
    br = min(mr, wr + t)
    bb = min(mb, wb + t)
    if br <= bl or bb <= bt:
        return None
    return (bl, bt, br, bb)


def extract_slot_samples_from_bgra(
    buf: bytes | bytearray | memoryview,
    box_rect: Rect,
    win_rect: Rect,
    slot_ratios: Sequence[tuple[float, float]],
    *,
    stride: int = 2,
) -> list[RGB | None]:
    """바운딩 박스 top-down 32bpp BGRA 버퍼에서 위젯 바깥 띠의 슬롯별 중앙값 RGB를 추출한다.

    위젯 안쪽 영역(`win_rect`) 픽셀은 한 점도 읽지 않으므로 자기 자신을 캡처하지 않는다.
    """
    bl, bt, br, bb = box_rect
    wl, wt, wr, wb = win_rect
    bw = br - bl
    bh = bb - bt
    win_w = wr - wl
    if bw <= 0 or bh <= 0 or win_w <= 0 or not slot_ratios:
        return [None for _ in slot_ratios]
    if len(buf) < bw * bh * 4:
        return [None for _ in slot_ratios]

    step = max(1, int(stride))
    results: list[RGB | None] = []
    last_idx = len(slot_ratios) - 1

    # top / bottom / left / right 바깥 띠의 y/x 범위 (box_rect 좌표 기준)
    top_y0 = 0
    top_y1 = max(0, min(bh, wt - bt))
    bot_y0 = max(0, min(bh, wb - bt))
    bot_y1 = bh
    mid_y0 = max(0, min(bh, wt - bt))
    mid_y1 = max(mid_y0, min(bh, wb - bt))
    left_x0 = 0
    left_x1 = max(0, min(bw, wl - bl))
    right_x0 = max(0, min(bw, wr - bl))
    right_x1 = bw

    for idx, (r0, r1) in enumerate(slot_ratios):
        f0 = max(0.0, min(1.0, float(r0)))
        f1 = max(f0, min(1.0, float(r1)))
        gx0 = wl + int(round(f0 * win_w))
        gx1 = wl + int(round(f1 * win_w))
        lx0 = max(0, min(bw, gx0 - bl))
        lx1 = max(0, min(bw, gx1 - bl))
        if lx1 <= lx0 and lx0 < bw:
            lx1 = min(bw, lx0 + 1)

        pixels: list[RGB] = []

        # 위쪽 띠
        for ly in range(top_y0, top_y1, step):
            row_off = ly * bw * 4
            for lx in range(lx0, lx1, step):
                off = row_off + lx * 4
                pixels.append((buf[off + 2], buf[off + 1], buf[off]))

        # 아래쪽 띠
        for ly in range(bot_y0, bot_y1, step):
            row_off = ly * bw * 4
            for lx in range(lx0, lx1, step):
                off = row_off + lx * 4
                pixels.append((buf[off + 2], buf[off + 1], buf[off]))

        # 양 끝 슬롯은 좌/우 띠도 포함 (화면 상단/하단에 붙어 있을 때도 샘플 확보)
        if idx == 0 and left_x1 > left_x0:
            for ly in range(mid_y0, mid_y1, step):
                row_off = ly * bw * 4
                for lx in range(left_x0, left_x1, step):
                    off = row_off + lx * 4
                    pixels.append((buf[off + 2], buf[off + 1], buf[off]))

        if idx == last_idx and right_x1 > right_x0:
            for ly in range(mid_y0, mid_y1, step):
                row_off = ly * bw * 4
                for lx in range(right_x0, right_x1, step):
                    off = row_off + lx * 4
                    pixels.append((buf[off + 2], buf[off + 1], buf[off]))

        results.append(robust_bg_color(pixels))

    return results


class ColorAnimator:
    """슬롯·역할별 RGB 색을 목표 색으로 부드럽게 지수 보간한다."""

    def __init__(self, alpha: float = 0.25, snap_eps: float = 1.0) -> None:
        self._alpha = max(0.05, min(1.0, float(alpha)))
        self._snap_eps = max(0.25, float(snap_eps))
        self._current: dict[tuple[int, str], tuple[float, float, float]] = {}
        self._target: dict[tuple[int, str], RGB] = {}

    def set_targets(self, targets: dict[tuple[int, str], RGB], *, immediate: bool = False) -> bool:
        """목표 색을 갱신하고 보간 애니메이션이 필요한지 반환한다."""
        for key in list(self._current):
            if key not in targets:
                del self._current[key]
        self._target = dict(targets)

        needs_anim = False
        for key, tgt in targets.items():
            if immediate or key not in self._current:
                self._current[key] = (float(tgt[0]), float(tgt[1]), float(tgt[2]))
            else:
                cur = self._current[key]
                if max(abs(cur[0] - tgt[0]), abs(cur[1] - tgt[1]), abs(cur[2] - tgt[2])) > self._snap_eps:
                    needs_anim = True
                else:
                    self._current[key] = (float(tgt[0]), float(tgt[1]), float(tgt[2]))
        return needs_anim

    def is_animating(self) -> bool:
        for key, tgt in self._target.items():
            cur = self._current.get(key)
            if cur is None:
                continue
            if max(abs(cur[0] - tgt[0]), abs(cur[1] - tgt[1]), abs(cur[2] - tgt[2])) > self._snap_eps:
                return True
        return False

    def step(self) -> tuple[dict[tuple[int, str], str], bool]:
        """한 프레임 전진하고 `(현재 hex 맵, 계속 애니메이션 필요 여부)`를 반환한다."""
        still = False
        out: dict[tuple[int, str], str] = {}
        a = self._alpha
        eps = self._snap_eps
        for key, tgt in self._target.items():
            cur = self._current.get(key)
            if cur is None:
                cur = (float(tgt[0]), float(tgt[1]), float(tgt[2]))
            nr = cur[0] + (tgt[0] - cur[0]) * a
            ng = cur[1] + (tgt[1] - cur[1]) * a
            nb = cur[2] + (tgt[2] - cur[2]) * a
            if max(abs(nr - tgt[0]), abs(ng - tgt[1]), abs(nb - tgt[2])) <= eps:
                cur = (float(tgt[0]), float(tgt[1]), float(tgt[2]))
            else:
                cur = (nr, ng, nb)
                still = True
            self._current[key] = cur
            out[key] = to_hex(cur)
        return out, still

    def current_hex(self, key: tuple[int, str]) -> str | None:
        cur = self._current.get(key)
        return to_hex(cur) if cur is not None else None

    def reset(self) -> None:
        self._current.clear()
        self._target.clear()


@dataclass(frozen=True)
class AdaptiveUpdate:
    chroma_changed: bool
    chroma_key: str
    needs_animation: bool
    colors_changed: bool


class AdaptiveColorController:
    """칸별 주변색 보정과 부드러운 전환을 관리하는 순수 상태 컨트롤러."""

    def __init__(self, *, anim_alpha: float = 0.25, min_contrast: float = 4.5) -> None:
        self._min_contrast = float(min_contrast)
        self.chroma_key: str = CHROMA_KEY_DARK
        self.slot_themes: dict[int, str] = {}
        self.slot_palettes: dict[int, dict[str, str]] = {}
        self.global_palette: dict[str, str] = default_palette_for_key(CHROMA_KEY_DARK)
        self.animator = ColorAnimator(alpha=anim_alpha)

    def apply_samples(
        self,
        slot_samples: Sequence[RGB | None],
        *,
        immediate: bool = False,
    ) -> AdaptiveUpdate:
        """슬롯별 배경 RGB 샘플을 반영해 목표 팔레트·크로마키·보간 상태를 갱신한다."""
        if not slot_samples:
            return AdaptiveUpdate(False, self.chroma_key, False, False)

        count = len(slot_samples)
        for stale_idx in list(self.slot_themes):
            if stale_idx >= count:
                del self.slot_themes[stale_idx]
        for stale_idx in list(self.slot_palettes):
            if stale_idx >= count:
                del self.slot_palettes[stale_idx]

        valid_rgbs: list[RGB] = []
        valid_lums: list[float] = []
        for idx, bg_rgb in enumerate(slot_samples):
            if bg_rgb is None:
                continue
            valid_rgbs.append(bg_rgb)
            valid_lums.append(relative_luminance(bg_rgb))
            prev_theme = self.slot_themes.get(idx, "dark")
            self.slot_themes[idx] = slot_theme(bg_rgb, prev_theme)

        if not valid_rgbs:
            return AdaptiveUpdate(False, self.chroma_key, self.animator.is_animating(), False)

        new_key = global_chroma_key(valid_lums, self.chroma_key)
        chroma_changed = new_key != self.chroma_key
        self.chroma_key = new_key

        prev_targets = dict(self.animator._target)
        targets: dict[tuple[int, str], RGB] = {}
        for idx, bg_rgb in enumerate(slot_samples):
            if bg_rgb is not None:
                theme = self.slot_themes.get(idx, "dark")
                pal = build_slot_palette(
                    theme,
                    bg_rgb,
                    chroma_key=self.chroma_key,
                    min_contrast=self._min_contrast,
                )
            elif idx in self.slot_palettes:
                pal = dict(self.slot_palettes[idx])
                pal["bg"] = self.chroma_key
                pal["grip_bg"] = self.chroma_key
                pal["border"] = self.chroma_key
            else:
                pal = default_palette_for_key(self.chroma_key)
            self.slot_palettes[idx] = pal
            for role in FOREGROUND_ROLES:
                targets[(idx, role)] = parse_hex(pal[role])

        # 전역 대표 팔레트(헤더/폴백용)
        rep_bg = robust_bg_color(valid_rgbs) or valid_rgbs[0]
        rep_theme = "light" if self.chroma_key == CHROMA_KEY_LIGHT else "dark"
        self.global_palette = build_slot_palette(
            rep_theme,
            rep_bg,
            chroma_key=self.chroma_key,
            min_contrast=self._min_contrast,
        )

        colors_changed = (targets != prev_targets) or chroma_changed
        needs_anim = self.animator.set_targets(targets, immediate=immediate)
        return AdaptiveUpdate(
            chroma_changed=chroma_changed,
            chroma_key=self.chroma_key,
            needs_animation=needs_anim,
            colors_changed=colors_changed,
        )

    def step_animation(self) -> tuple[dict[tuple[int, str], str], bool]:
        return self.animator.step()

    def color_for(self, slot_idx: int, role: str) -> str:
        """현재 프레임(보간 중 포함)에서 `(slot_idx, role)`에 적용할 hex 색을 반환한다."""
        cur = self.animator.current_hex((slot_idx, role))
        if cur is not None:
            return cur
        pal = self.slot_palettes.get(slot_idx) or self.global_palette
        return pal.get(role, self.global_palette.get(role, "#ffffff"))

    def reset(self) -> None:
        self.chroma_key = CHROMA_KEY_DARK
        self.slot_themes.clear()
        self.slot_palettes.clear()
        self.global_palette = default_palette_for_key(CHROMA_KEY_DARK)
        self.animator.reset()
