"""위젯 화면의 배치 계산 (그리기 명령 목록 만들기).

화면에 무엇을 어디에 그릴지만 정한다. 실제로 그리는 일은 layered.py가 한다.
글자 너비를 재는 도구(Metrics)를 밖에서 받으므로 Windows 없이도 테스트할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

HitRect = tuple[int, int, int, int]  # (x0, y0, x1, y1), 창 왼쪽 위 기준


# ---------------- 그리기 명령 ----------------
@dataclass(frozen=True)
class Text:
    x: float
    y: float
    text: str
    size: float
    color: int
    bold: bool = False
    halo: bool = False  # 어두운 외곽선 (투명 배경에서 어떤 바탕 위에서도 읽히게)


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    w: float
    h: float
    pct: float
    pace: float | None
    track: int
    fill: int
    marker: int


@dataclass(frozen=True)
class Dot:
    cx: float
    cy: float
    d: float
    color: int


@dataclass(frozen=True)
class Ring:
    cx: float
    cy: float
    d: float  # 선 가운데를 지나는 원의 지름
    t: float  # 선 굵기
    pct: float | None  # None이면 빈 고리만 그린다
    pace: float | None
    track: int
    fill: int
    marker: int
    halo: bool = False
    gain: float = 0.0  # 채워진 부분의 끝에서 이만큼(%)을 gain_color로 덧칠한다
    gain_color: int = 0


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float
    radius: float
    fill: int
    stroke: int | None = None


@dataclass
class Scene:
    width: int
    height: int
    ops: list = field(default_factory=list)
    hits: dict[str, HitRect] = field(default_factory=dict)


# ---------------- 입력 ----------------
class Metrics(Protocol):
    def measure(self, text: str, size: float, bold: bool = False) -> float: ...

    def line_height(self, size: float) -> float: ...


@dataclass(frozen=True)
class Style:
    main: float  # 본문 글자 크기 (픽셀)
    small: float  # 작은 글자 크기 (픽셀)
    bar_w: float
    bar_h: float
    pad_x: float
    pad_y: float
    compact_pad_x: float
    compact_pad_y: float
    wrap_w: float
    unit: float  # 디자인 치수 1에 해당하는 실제 픽셀 (배율 × 화면 DPI)
    transparent: bool
    colors: Mapping[str, int]  # 역할 이름 -> ARGB
    pct_bold: bool = False


@dataclass(frozen=True)
class WindowRow:
    label: str
    used: float
    pace: float | None
    pace_role: str
    reset: str
    eta: str


@dataclass(frozen=True)
class ProviderBlock:
    name: str
    dot_role: str
    stale: bool
    dim: bool
    error: str
    windows: tuple[WindowRow, ...]


@dataclass(frozen=True)
class Gauge:
    used: float
    pace: float | None
    role: str  # 사용률 색 역할 (ok / warn / danger / muted)
    pace_role: str = "muted"  # 기준 대비 속도 색 역할
    gain: float = 0.0  # 추적을 시작한 뒤 늘어난 양 (%p)


@dataclass(frozen=True)
class CompactItem:
    short: str
    long: Gauge | None  # 주간 등 긴 주기
    five: Gauge | None  # 5시간 등 짧은 주기
    mark: str = "-"  # 보여 줄 값이 없을 때 대신 쓰는 글자
    mark_role: str = "muted"
    tracking: bool = False  # 사용량 변동 추적 중 (링 둘레에 물결을 그린다)


BRAND_DOTS = (0xFF4285F4, 0xFFEA4335, 0xFFFBBC04, 0xFF34A853)
CLICK_BG = 0x01000000  # 눈에는 안 보이지만 Windows가 클릭을 전달하는 최소 불투명도


def argb(hex_color: str, alpha: int = 255) -> int:
    """'#rrggbb'를 ARGB 정수로 바꾼다."""
    return ((alpha & 0xFF) << 24) | int(hex_color.lstrip("#"), 16)


def lighten(color: int, amount: float = 0.65) -> int:
    """색을 흰색 쪽으로 섞어 밝게 한다 (불투명도는 그대로)."""
    out = color & 0xFF000000
    for shift in (16, 8, 0):
        channel = (color >> shift) & 0xFF
        out |= int(channel + (255 - channel) * amount) << shift
    return out


def wrap_text(text: str, max_w: float, measure: Callable[[str], float]) -> list[str]:
    """너비 max_w를 넘지 않도록 띄어쓰기 단위로 줄을 나눈다. 한 단어가 너무 길면 글자 단위로 자른다."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for word in paragraph.split(" "):
            candidate = f"{current} {word}" if current else word
            if measure(candidate) <= max_w:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word
            while current and measure(current) > max_w and len(current) > 1:
                cut = len(current) - 1
                while cut > 1 and measure(current[:cut]) > max_w:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current)
    return lines


class _Builder:
    def __init__(self, style: Style, metrics: Metrics) -> None:
        self.st = style
        self.mt = metrics
        self.ops: list = []
        self.hits: dict[str, HitRect] = {}

    def color(self, role: str) -> int:
        return self.st.colors.get(role, self.st.colors["fg"])

    def width(self, text: str, size: float, bold: bool = False) -> float:
        return self.mt.measure(text, size, bold) if text else 0.0

    def text(self, x: float, y: float, text: str, size: float, role: str, *, bold: bool = False, right: bool = False) -> float:
        """글자를 놓고 너비를 돌려준다. right=True면 x가 오른쪽 끝이다."""
        w = self.width(text, size, bold)
        if text:
            self.ops.append(
                Text(x - w if right else x, y, text, size, self.color(role), bold=bold, halo=self.st.transparent)
            )
        return w

    def hit(self, name: str, x0: float, y0: float, x1: float, y1: float) -> None:
        pad = 2 * self.st.unit
        self.hits[name] = (int(x0 - pad), int(y0 - pad), int(x1 + pad + 0.999), int(y1 + pad + 0.999))

    def ring(self, cx: float, cy: float, d: float, t: float, gauge: "Gauge | None") -> None:
        fill = self.color(gauge.role if gauge is not None else "muted")
        marker = self.color("muted" if gauge is not None and gauge.role == "muted" else "marker")
        gain = min(gauge.used, gauge.gain) if gauge is not None and gauge.gain > 0 else 0.0
        self.ops.append(
            Ring(
                cx, cy, d, t,
                gauge.used if gauge is not None else None,
                gauge.pace if gauge is not None else None,
                self.color("bar_bg"), fill, marker, halo=self.st.transparent,
                gain=gain, gain_color=lighten(fill) if gain else 0,
            )
        )

    def finish(self, width: float, height: float, radius: float | None = None) -> Scene:
        w, h = max(1, int(width + 0.999)), max(1, int(height + 0.999))
        st = self.st
        if st.transparent:
            background = Box(0, 0, w, h, 0, CLICK_BG)
        else:
            r = 5 * st.unit if radius is None else radius
            background = Box(0.5, 0.5, w - 1, h - 1, r, st.colors["bg"], st.colors["border"])
        return Scene(w, h, [background, *self.ops], self.hits)


# ---------------- 상세 모드 ----------------
def layout_detail(
    blocks: list[ProviderBlock] | tuple[ProviderBlock, ...],
    style: Style,
    metrics: Metrics,
    *,
    update_text: str = "",
    clock_text: str = "",
) -> Scene:
    st, u = style, style.unit
    b = _Builder(st, metrics)
    lh_m, lh_s = metrics.line_height(st.main), metrics.line_height(st.small)
    small_dy = (lh_m - lh_s) * 0.75  # 작은 글자를 본문 글자와 밑줄(기준선)이 맞게 내린다
    px = st.pad_x + (2 if st.transparent else 3) * u
    py = st.pad_y + (1 if st.transparent else 2) * u

    # --- 본문 열 너비 ---
    rows = [w for blk in blocks for w in blk.windows]
    label_w = max((b.width(w.label, st.main) for w in rows), default=0.0)
    pct_w = max((b.width(f"{round(w.used)}%", st.main, st.pct_bold) for w in rows), default=0.0)
    pace_w = max((b.width(f"기준{round(w.pace)}%", st.small) for w in rows if w.pace is not None), default=0.0)
    reset_w = max((b.width(w.reset, st.small) for w in rows), default=0.0)
    col_bar = label_w + 7 * u
    col_pct_right = col_bar + st.bar_w + 5 * u + pct_w
    col_pace = col_pct_right + 3 * u
    col_reset = (col_pace + pace_w if pace_w else col_pct_right) + 4 * u
    body_w = col_reset + reset_w if reset_w else (col_pace + pace_w if pace_w else col_pct_right)

    dot_d = max(4 * u, st.bar_h)
    for blk in blocks:
        name = blk.name + (" (오래됨)" if blk.stale else "")
        body_w = max(body_w, dot_d + 4 * u + b.width(name, st.main, True))
        for w in blk.windows:
            if w.eta and not blk.dim:
                body_w = max(body_w, b.width(f"↳ {w.eta}", st.small))
    error_lines: dict[int, list[str]] = {}
    for idx, blk in enumerate(blocks):
        if blk.error:
            lines = wrap_text(blk.error, max(st.wrap_w, body_w), lambda s: b.width(s, st.small))
            error_lines[idx] = lines
            body_w = max(body_w, max(b.width(line, st.small) for line in lines))
    if not blocks:
        body_w = b.width("데이터 대기 중…", st.small)

    # --- 머리글 너비 ---
    brand_d = max(3 * u, st.bar_h - u)
    brand_gap = max(2 * u, brand_d / 2)
    brand_w = brand_d * 4 + brand_gap * 3
    left_w = b.width("▾", st.small) + 5 * u + brand_w + 4 * u + b.width("AI", st.main, True)
    if update_text:
        left_w += 4 * u + b.width(update_text, st.small)
    buttons = (("close", "×"), ("settings", "⚙"), ("refresh", "↻"))
    right_w = sum(b.width(glyph, st.main, True) + 5 * u for _name, glyph in buttons)
    if clock_text:
        right_w += b.width(clock_text, st.small) + 5 * u
    inner_w = max(body_w, left_w + 8 * u + right_w)
    width = inner_w + px * 2

    # --- 머리글 ---
    y = py
    x = px
    w = b.text(x, y + small_dy, "▾", st.small, "muted")
    b.hit("compact", x, y, x + w, y + lh_m)
    x += w + 5 * u
    for i, color in enumerate(BRAND_DOTS):
        b.ops.append(Dot(x + brand_d / 2 + i * (brand_d + brand_gap), y + lh_m / 2, brand_d, color))
    x += brand_w + 4 * u
    x += b.text(x, y, "AI", st.main, "fg", bold=True)
    if update_text:
        x += 4 * u
        w = b.text(x, y + small_dy, update_text, st.small, "accent")
        b.hit("update", x, y, x + w, y + lh_m)

    x = width - px
    for name, glyph in buttons:
        w = b.text(x - u, y, glyph, st.main, "muted", bold=True, right=True)
        b.hit(name, x - w - 2 * u, y, x, y + lh_m)
        x -= w + 5 * u
    if clock_text:
        b.text(x - 2 * u, y + small_dy, clock_text, st.small, "muted", right=True)

    y += lh_m
    if st.transparent:
        y += max(2 * u, st.pad_y)
    else:
        y += 3 * u
        b.ops.append(Box(px, y, inner_w, u, 0, st.colors["border"]))
        y += 4 * u

    # --- 본문 ---
    if not blocks:
        b.text(px, y, "데이터 대기 중…", st.small, "muted")
        y += lh_s
    for idx, blk in enumerate(blocks):
        if idx:
            y += 3 * u
        b.ops.append(Dot(px + dot_d / 2, y + lh_m / 2, dot_d, b.color(blk.dot_role)))
        name = blk.name + (" (오래됨)" if blk.stale else "")
        b.text(px + dot_d + 4 * u, y, name, st.main, "muted" if blk.stale else "fg", bold=True)
        y += lh_m + u
        fg = "muted" if blk.dim else "fg"
        for w in blk.windows:
            b.text(px, y, w.label, st.main, fg)
            fill_role = "muted" if blk.dim else _level(w.used)
            b.ops.append(
                Bar(
                    px + col_bar, y + (lh_m - st.bar_h) / 2 + u * 0.5, st.bar_w, st.bar_h, w.used, w.pace,
                    b.color("bar_bg"), b.color(fill_role), b.color("muted" if blk.dim else "marker"),
                )
            )
            b.text(px + col_pct_right, y, f"{round(w.used)}%", st.main, fg, bold=st.pct_bold, right=True)
            if w.pace is not None:
                b.text(px + col_pace, y + small_dy, f"기준{round(w.pace)}%", st.small, "muted" if blk.dim else w.pace_role)
            if w.reset:
                b.text(px + col_reset, y + small_dy, w.reset, st.small, "muted")
            y += lh_m
            if w.eta and not blk.dim:
                b.text(px, y, f"↳ {w.eta}", st.small, "warn")
                y += lh_s
        for line in error_lines.get(idx, []):
            b.text(px, y, line, st.small, "error")
            y += lh_s

    return b.finish(width, y + py)


def _level(used_percent: float) -> str:
    if used_percent >= 90:
        return "danger"
    if used_percent >= 70:
        return "warn"
    return "ok"


# ---------------- 컴팩트 모드 ----------------
def layout_compact(
    items: list[CompactItem] | tuple[CompactItem, ...],
    style: Style,
    metrics: Metrics,
    *,
    has_update: bool = False,
    wave: float | None = None,
    ripples: bool = True,
) -> Scene:
    """서비스마다 이중 링(바깥 = 긴 주기, 안쪽 = 5시간)과 숫자를 알약 모양 안에 한 줄로 놓는다.

    wave는 추적 중인 서비스의 물결이 퍼진 정도(0~1)다. None이면 움직이지 않는 옅은 고리 하나만 그린다.
    """
    st, u = style, style.unit
    b = _Builder(st, metrics)
    lh_m, lh_s = metrics.line_height(st.main), metrics.line_height(st.small)
    small_dy = (lh_m - lh_s) * 0.75
    ring_d = float(round(lh_m * 1.5))
    t = max(2.0, ring_d * 0.115)
    gap = max(1.0, t * 0.5)
    px = st.compact_pad_x + (2 if st.transparent else 5) * u
    py = st.compact_pad_y + (1 if st.transparent else 2) * u
    row = py + (ring_d - lh_m) / 2
    height = py * 2 + ring_d

    x = px
    w = b.text(x, row + small_dy, "▸", st.small, "muted")
    b.hit("compact", x, py, x + w + 2 * u, py + ring_d)
    x += w + 4 * u

    if not items:
        x += b.text(x, row + small_dy, "대기 중…", st.small, "muted")
        return b.finish(x + px, height, radius=height / 2)

    two_rings = any(it.five is not None for it in items)
    label_gap = st.small * 0.35
    for idx, it in enumerate(items):
        if idx:
            x += 7 * u
        cx, cy = x + ring_d / 2, py + ring_d / 2
        if it.tracking and ripples:
            shown_gauge = it.long if it.long is not None else it.five
            role = shown_gauge.role if shown_gauge is not None and shown_gauge.role != "muted" else "accent"
            rgb = b.color(role) & 0x00FFFFFF
            reach = max(1.0, min(py, 4 * u) - 1.0)  # 물결이 링 밖으로 퍼지는 거리 (알약 안쪽 여백까지만)
            for ph in ((wave, (wave + 0.5) % 1.0) if wave is not None else (0.55,)):
                color = (int(235 * (1.0 - ph) ** 0.8) << 24) | rgb
                start = ring_d - t  # 바깥 링의 선 가운데에서 시작해 여백 끝까지 퍼진다
                d = start + (ring_d + 2 * reach - start) * ph
                b.ops.append(Ring(cx, cy, d, max(1.2, t * 0.6), None, None, color, color, color))
        b.ring(cx, cy, ring_d - t, t, it.long)
        if two_rings:
            b.ring(cx, cy, ring_d - t - 2 * (t + gap), t, it.five)
        x += ring_d + 4 * u
        x += b.text(x, row + small_dy, it.short, st.small, "muted") + label_gap
        shown = it.long if it.long is not None else it.five
        if shown is None:
            x += b.text(x, row, it.mark, st.main, it.mark_role, bold=True)
            continue
        value_role = "fg" if shown.role == "ok" else shown.role
        # 적정 기준값은 숫자로 쓰지 않고 링 위의 눈금으로만 보여 준다.
        x += b.text(x, row, str(round(shown.used)), st.main, value_role, bold=True)
        # 추적을 시작한 뒤 늘어난 양. 잘 움직이는 5시간 값을 먼저 쓰고, 없으면 긴 주기 값을 쓴다.
        gained = next((g.gain for g in (it.five, it.long) if g is not None and g.gain >= 1), 0.0)
        if it.tracking and gained:
            x += u + b.text(x + u, row + small_dy, f"+{round(gained)}", st.small, "fg")

    if has_update:
        x += 4 * u
        w = b.text(x, row + small_dy, "⬆", st.small, "accent")
        b.hit("update", x, py, x + w, py + ring_d)
        x += w

    return b.finish(x + px, height, radius=height / 2)
