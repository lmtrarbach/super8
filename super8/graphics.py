"""Essential-graphics style titles: layout, styling, in/out animations, rolls, and templates.

Everything is drawn with QPainter onto a QImage (safe in worker threads) and returned as an RGBA crop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

# ------------------------------------------------------------------ catalogues
ANIMATIONS = {
    "": "None",
    "fade": "Fade",
    "from_bottom": "Slide from Bottom",
    "from_top": "Slide from Top",
    "from_left": "Slide from Left",
    "from_right": "Slide from Right",
    "pop": "Pop",
    "zoom": "Zoom",
    "wipe": "Wipe Reveal",
    "typewriter": "Typewriter",
    "words": "Word by Word",
    "tracking": "Tracking",
}
ROLLS = {"": "Static", "up": "Credits Roll (up)", "left": "Crawl (right → left)"}
BACKGROUNDS = {"none": "None", "box": "Box", "bar": "Accent Bar", "band": "Full-width Band"}
ALIGNS = {"left": "Left", "center": "Center", "right": "Right"}

# field -> value overrides; "duration" is the default clip length
PRESETS: dict[str, dict] = {
    "basic": dict(name="Basic Title", text="Your Title", font_size=110, anim_in="fade", anim_out="fade"),
    "big_bold": dict(name="Big Bold", text="BIG NEWS", font_size=220, outline_width=6, anim_in="pop",
                     anim_out="zoom", anim_in_dur=0.45, shadow=0.8),
    "lower_bar": dict(name="Lower Third · Bar", text="Jane Doe", subtext="Director of Photography",
                      font_size=64, align="left", bg_style="bar", anim_in="from_left", anim_out="fade",
                      pos_x=-800, pos_y=360, shadow=0.7),
    "lower_box": dict(name="Lower Third · Box", text="John Smith", subtext="Founder, Super8 Studio",
                      font_size=60, align="left", bg_style="box", bg_opacity=0.75, anim_in="wipe",
                      anim_out="wipe", pos_x=-790, pos_y=360, shadow=0.0, sub_color="#ffc533"),
    "caption": dict(name="Subtitle / Caption", text="This is what is being said on screen.", font_size=52,
                    bold=False, bg_style="box", bg_opacity=0.55, pos_y=420, anim_in="fade", anim_out="fade",
                    anim_in_dur=0.15, anim_out_dur=0.15, shadow=0.0, duration=3.0),
    "chapter": dict(name="Chapter Heading", text="CHAPTER ONE", subtext="The beginning", font_size=120,
                    anim_in="tracking", anim_out="fade", anim_in_dur=1.2, outline_width=0, shadow=0.5),
    "typewriter": dict(name="Typewriter", text="Once upon a time…", font_family="Monospace", bold=False,
                       font_size=84, anim_in="typewriter", anim_in_dur=1.6, anim_out="fade"),
    "kinetic": dict(name="Kinetic Words", text="Make every word count", font_size=130, anim_in="words",
                    anim_in_dur=1.4, anim_out="from_top", shadow=0.8),
    "callout": dict(name="Callout Box", text="NEW!", subtext="Limited edition", font_size=70, align="left",
                    bg_style="box", bg_color="#c2185b", bg_opacity=0.95, pos_x=-820, pos_y=-380,
                    anim_in="from_top", anim_out="from_top", shadow=0.0),
    "quote": dict(name="Quote", text="“Simplicity is the ultimate sophistication.”", subtext="— Leonardo da Vinci",
                  font_family="Serif", italic=True, bold=False, font_size=76, anim_in="fade", anim_out="fade",
                  anim_in_dur=1.0, anim_out_dur=1.0, sub_color="#ffc533"),
    "ticker": dict(name="News Ticker", text="BREAKING  •  Super8 now has animated graphics  •  "
                   "Lower thirds, captions, credits and more  •", font_size=48, bg_style="band",
                   bg_color="#b71c1c", bg_opacity=0.95, roll="left", pos_y=470, shadow=0.0, duration=10.0),
    "credits": dict(name="Credits Roll", text="Directed by\nJane Doe\n\nEdited by\nJohn Smith\n\n"
                    "Made with\nSuper8", subtext="Thanks for watching", font_size=72, roll="up",
                    shadow=0.6, duration=10.0),
}


USER_PRESETS: dict[str, dict] = {}  # "user:<name>" -> fields; filled from settings by the UI

# what a template stores
GRAPHIC_FIELDS = ("text", "subtext", "font_size", "color", "sub_color", "font_family", "bold", "italic", "align",
                  "outline_width", "outline_color", "shadow", "bg_style", "bg_color", "bg_opacity",
                  "accent_color", "anim_in", "anim_in_dur", "anim_out", "anim_out_dur", "roll", "pos_x", "pos_y",
                  "scale", "opacity", "fade_in", "fade_out")


def get_preset(preset_id: str) -> dict:
    return USER_PRESETS.get(preset_id) or PRESETS.get(preset_id) or PRESETS["basic"]


def all_presets() -> dict[str, dict]:
    return {**PRESETS, **USER_PRESETS}


# templates are authored for a 1920x1080 frame; these fields scale with the sequence size
REF_W, REF_H = 1920, 1080
_SCALED = {"font_size": "h", "outline_width": "h", "pos_x": "w", "pos_y": "h"}


def _factor(key: str, seq_w: int, seq_h: int) -> float:
    return seq_w / REF_W if _SCALED[key] == "w" else seq_h / REF_H


def apply_preset(clip, preset_id: str, seq_w: int = REF_W, seq_h: int = REF_H) -> None:
    for k, v in get_preset(preset_id).items():
        if k in ("name", "duration") or not hasattr(clip, k):
            continue
        if k in _SCALED:
            v = v * _factor(k, seq_w, seq_h)
            if k == "font_size":
                v = max(6, int(round(v)))
        setattr(clip, k, v)


def preset_duration(preset_id: str) -> float:
    return float(get_preset(preset_id).get("duration", 5.0))


def template_from_clip(clip, name: str, seq_w: int = REF_W, seq_h: int = REF_H) -> dict:
    d = {k: getattr(clip, k) for k in GRAPHIC_FIELDS}
    for k in _SCALED:
        d[k] = round(d[k] / _factor(k, seq_w, seq_h), 2)
    d.update(name=name, duration=round(clip.duration, 3))
    return d


def preview_image(preset_id: str, w: int = 320, h: int = 180) -> QImage:
    """Thumbnail of a template over a neutral gradient (rendered as a 1920x1080 sequence)."""
    from .model import Clip
    c = Clip("preview", "title", None, 0.0, preset_duration(preset_id))
    apply_preset(c, preset_id)
    img = QImage(w, h, QImage.Format.Format_RGB888)
    p = QPainter(img)
    from PyQt6.QtGui import QLinearGradient
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#3a4a63"))
    g.setColorAt(1, QColor("#1c2230"))
    p.fillRect(0, 0, w, h, g)
    local = c.duration * (0.45 if c.roll else 0.5)
    res = render_graphic(c, w, h, 1920, 1080, local)
    if res is not None:
        arr, x0, y0 = res
        crop = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 4, QImage.Format.Format_RGBA8888)
        p.drawImage(x0, y0, crop)
    p.end()
    return img


# ------------------------------------------------------------------ animation
def _ease(p):
    return 1 - (1 - p) ** 3


def _ease_back(p):
    c1 = 1.70158
    return 1 + (c1 + 1) * (p - 1) ** 3 + c1 * (p - 1) ** 2


@dataclass
class AnimState:
    opacity: float = 1.0
    dx: float = 0.0  # fraction of canvas height
    dy: float = 0.0
    scale: float = 1.0
    reveal: float = 1.0  # wipe: visible fraction of the block width
    chars: float = 1.0  # typewriter: fraction of characters shown
    words: float = 1.0
    tracking: float = 0.0  # extra letter spacing, in ems

    def key(self):
        return tuple(round(v, 4) for v in (self.opacity, self.dx, self.dy, self.scale, self.reveal, self.chars,
                                            self.words, self.tracking))


def _apply(st: AnimState, kind: str, e: float, outgoing: bool) -> None:
    """Apply one animation at visibility e (0 hidden .. 1 fully shown)."""
    e = min(1.0, max(0.0, e))
    if e >= 1.0 or not kind:
        return
    q = _ease(e)
    if kind == "fade":
        st.opacity *= e
    elif kind in ("from_bottom", "from_top", "from_left", "from_right"):
        dist = 0.12 if kind in ("from_bottom", "from_top") else 0.25
        sign = 1 if kind in ("from_bottom", "from_right") else -1
        if kind in ("from_bottom", "from_top"):
            st.dy += sign * dist * (1 - q)
        else:
            st.dx += sign * dist * (1 - q)
        st.opacity *= min(1.0, e * 1.6)
    elif kind == "pop":
        st.scale *= max(0.05, 0.4 + 0.6 * (_ease_back(e) if not outgoing else q))
        st.opacity *= min(1.0, e * 2.5)
    elif kind == "zoom":
        st.scale *= 1 + 0.6 * (1 - q)
        st.opacity *= e
    elif kind == "wipe":
        st.reveal = min(st.reveal, q)
    elif kind == "typewriter":
        st.chars = min(st.chars, e)
    elif kind == "words":
        st.words = min(st.words, e)
    elif kind == "tracking":
        st.tracking += 0.6 * (1 - q)
        st.opacity *= e


def anim_state(clip, local: float) -> AnimState:
    st = AnimState()
    if clip.anim_in and clip.anim_in_dur > 0:
        _apply(st, clip.anim_in, local / clip.anim_in_dur, False)
    if clip.anim_out and clip.anim_out_dur > 0:
        _apply(st, clip.anim_out, (clip.duration - local) / clip.anim_out_dur, True)
    return st


# ------------------------------------------------------------------ layout
@dataclass
class _Line:
    text: str
    font: QFont
    color: str
    x: float
    baseline: float
    width: float


def _fonts(clip, k: float, st: AnimState):
    size = max(1, int(round(clip.font_size * clip.scale * k * st.scale)))
    main = QFont(clip.font_family)
    main.setPixelSize(size)
    main.setBold(clip.bold)
    main.setItalic(clip.italic)
    sub = QFont(clip.font_family)
    sub.setPixelSize(max(1, int(size * 0.5)))
    sub.setItalic(clip.italic)
    if st.tracking:
        for f in (main, sub):
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, st.tracking * f.pixelSize())
    return main, sub


def layout(clip, W: int, H: int, seq_w: int, seq_h: int, local: float, st: AnimState | None = None):
    """Compute line positions and the text block rect in canvas pixels."""
    st = st or anim_state(clip, local)
    k = H / seq_h
    main, sub = _fonts(clip, k, st)
    rows = [(ln, main, clip.color) for ln in (clip.text.split("\n") if clip.text else [])]
    if clip.subtext:
        rows += [(ln, sub, clip.sub_color) for ln in clip.subtext.split("\n")]
    metrics = []
    for text, font, color in rows:
        fm = QFontMetricsF(font)
        metrics.append((text, font, color, fm.horizontalAdvance(text) if text else 0.0, fm.ascent(), fm.descent(),
                        fm.lineSpacing()))
    bw = max((m[3] for m in metrics), default=0.0)
    bh = sum(m[6] for m in metrics)
    if clip.subtext and clip.text:
        bh += main.pixelSize() * 0.12  # small gap between title and subtitle
    cx = W / 2 + clip.pos_x * k + st.dx * H
    cy = H / 2 + clip.pos_y * k + st.dy * H
    if clip.roll and clip.duration > 0:
        prog = min(1.0, max(0.0, local / clip.duration))
        if clip.roll == "up":
            cy = H + bh / 2 - prog * (H + bh)
        else:
            cx = W + bw / 2 - prog * (W + bw)
    # the position is the anchor: left edge / centre / right edge depending on alignment (like Premiere),
    # so a longer name in a left-aligned lower third grows to the right instead of off-screen
    if clip.align == "left" and clip.roll != "left":
        cx += bw / 2
    elif clip.align == "right" and clip.roll != "left":
        cx -= bw / 2
    block = QRectF(cx - bw / 2, cy - bh / 2, bw, bh)
    lines = []
    y = block.top()
    for i, (text, font, color, w, asc, desc, spacing) in enumerate(metrics):
        if i > 0 and font is sub and metrics[i - 1][1] is main:
            y += main.pixelSize() * 0.12
        x = block.left() if clip.align == "left" else (block.right() - w if clip.align == "right" else cx - w / 2)
        lines.append(_Line(text, font, color, x, y + asc, w))
        y += spacing
    return lines, block, main.pixelSize(), st


def block_rect(clip, seq_w: int, seq_h: int, local: float | None = None) -> QRectF:
    """Text block (plus background) in sequence pixels; used for the monitor's selection box."""
    local = clip.duration / 2 if local is None else local
    st = AnimState()
    lines, block, px, _ = layout(clip, seq_w, seq_h, seq_w, seq_h, local, st)
    return _decor_rects(clip, block, px, seq_w)[0]


def _decor_rects(clip, block: QRectF, px: float, W: int):
    """(bounds incl. background, background rect or None, accent bar rect or None)."""
    pad = px * 0.35
    bg = bar = None
    bounds = QRectF(block)
    if clip.bg_style == "box":
        bg = block.adjusted(-pad, -pad * 0.6, pad, pad * 0.6)
        bounds = bg
    elif clip.bg_style == "band":
        bg = QRectF(0, block.top() - pad * 0.6, W, block.height() + pad * 1.2)
        bounds = bg
    elif clip.bg_style == "bar":
        bw = max(3.0, px * 0.12)
        bar = QRectF(block.left() - pad * 0.6 - bw, block.top() - pad * 0.2, bw, block.height() + pad * 0.4)
        bounds = block.united(bar)
    return bounds, bg, bar


# ------------------------------------------------------------------ rendering
def _visible_text(lines, st: AnimState):
    """Apply typewriter / word-by-word reveal: returns per-line strings to draw."""
    texts = [ln.text for ln in lines]
    if st.chars < 1.0:
        total = sum(len(t) for t in texts)
        n = int(math.floor(st.chars * total + 1e-9))
        out = []
        for t in texts:
            out.append(t[:max(0, n)])
            n -= len(t)
        return out
    if st.words < 1.0:
        words = [len(t.split()) for t in texts]
        n = int(math.ceil(st.words * sum(words) - 1e-9))
        out = []
        for t, wc in zip(texts, words):
            parts = t.split()
            out.append(" ".join(parts[:max(0, n)]) if n < wc else t)
            n -= wc
        return out
    return texts


def render_graphic(clip, W: int, H: int, seq_w: int, seq_h: int, local: float):
    """Return (rgba uint8 crop, x0, y0) for the title at clip-local time, or None if nothing is visible."""
    st = anim_state(clip, local)
    if st.opacity <= 0.002:
        return None
    lines, block, px, st = layout(clip, W, H, seq_w, seq_h, local, st)
    bounds, bg, bar = _decor_rects(clip, block, px, W)
    margin = clip.outline_width * H / seq_h + px * 0.12 + 4
    r = bounds.adjusted(-margin, -margin, margin, margin).intersected(QRectF(0, 0, W, H))
    if r.width() < 1 or r.height() < 1:
        return None
    x0, y0 = int(math.floor(r.left())), int(math.floor(r.top()))
    iw, ih = int(math.ceil(r.right())) - x0, int(math.ceil(r.bottom())) - y0
    img = QImage(iw, ih, QImage.Format.Format_RGBA8888)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.translate(-x0, -y0)
    p.setOpacity(st.opacity)
    if st.reveal < 1.0:
        left = bounds.left()
        p.setClipRect(QRectF(left, bounds.top() - margin, bounds.width() * st.reveal, bounds.height() + 2 * margin))
    if bg is not None:
        c = QColor(clip.bg_color)
        c.setAlphaF(max(0.0, min(1.0, clip.bg_opacity)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        radius = 0 if clip.bg_style == "band" else px * 0.12
        p.drawRoundedRect(bg, radius, radius)
        if clip.bg_style == "box":  # thin accent line under the box
            p.setBrush(QColor(clip.accent_color))
            p.drawRect(QRectF(bg.left(), bg.bottom() - max(2.0, px * 0.05), bg.width(), max(2.0, px * 0.05)))
    if bar is not None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(clip.accent_color))
        p.drawRect(bar)
    visible = _visible_text(lines, st)
    paths = []
    for ln, txt in zip(lines, visible):
        if not txt:
            continue
        path = QPainterPath()
        path.addText(QPointF(ln.x, ln.baseline), ln.font, txt)
        paths.append((path, ln.color))
    if clip.shadow > 0:
        off = max(1.0, px * 0.045)
        for path, _ in paths:
            for i, a in enumerate((0.35, 0.25, 0.15)):
                sh = QColor(0, 0, 0, int(255 * a * clip.shadow))
                p.fillPath(path.translated(off + i * off * 0.5, off + i * off * 0.5), sh)
    ow = clip.outline_width * H / seq_h
    if ow > 0:
        pen = QPen(QColor(clip.outline_color), ow * 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        for path, _ in paths:
            p.strokePath(path, pen)
    for path, color in paths:
        p.fillPath(path, QColor(color))
    # typewriter caret
    if 0 < st.chars < 1.0 and paths:
        last_i = max(i for i, t in enumerate(visible) if t) if any(visible) else 0
        ln = lines[last_i]
        cx = ln.x + QFontMetricsF(ln.font).horizontalAdvance(visible[last_i])
        p.fillRect(QRectF(cx + 2, ln.baseline - ln.font.pixelSize() * 0.8, max(2.0, px * 0.06),
                          ln.font.pixelSize() * 0.9), QColor(ln.color))
    p.end()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(ih, img.bytesPerLine() // 4, 4)[:, :iw].copy()
    return arr, x0, y0


def title_cache_key(clip, W, H, seq_w, seq_h, local):
    """Key that changes only when the rendered pixels would change (static titles are rendered once)."""
    st = anim_state(clip, local)
    roll = round(local / max(1e-6, clip.duration), 5) if clip.roll else 0
    fields = (clip.text, clip.subtext, clip.font_size, clip.color, clip.sub_color, clip.font_family, clip.bold,
              clip.italic, clip.align, clip.outline_width, clip.outline_color, clip.shadow, clip.bg_style,
              clip.bg_color, clip.bg_opacity, clip.accent_color, clip.pos_x, clip.pos_y, clip.scale, clip.roll)
    return fields, st.key(), roll, W, H, seq_w, seq_h
