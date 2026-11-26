"""Clip transform geometry (no Qt): where a picture lands on the canvas.

Order, as in Premiere: the source is fitted to the frame (fit mode), scaled (uniform x per-axis),
cropped (in source orientation; the rest of the picture stays where it was), flipped, then rotated
around the centre of the full picture and positioned. All sizes here are canvas pixels; positions
in the clip are sequence pixels and get scaled to the canvas.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

FIT_MODES = {
    "fit": "Fit (letterbox)",
    "fill": "Fill (crop to frame)",
    "stretch": "Stretch to frame",
    "none": "Original size",
}
MAX_CROP = 0.95  # left+right (and top+bottom) can't remove more than this

ASPECTS = [  # label, w, h
    ("16:9 Widescreen", 16, 9),
    ("9:16 Vertical (Shorts, Reels, TikTok)", 9, 16),
    ("1:1 Square", 1, 1),
    ("4:5 Portrait (Instagram feed)", 4, 5),
    ("4:3 Classic", 4, 3),
    ("21:9 Cinema", 21, 9),
]
RESOLUTIONS = [480, 720, 1080, 1440, 2160]  # length of the short side


def size_for(aw: int, ah: int, short: int) -> tuple[int, int]:
    """Even width/height for an aspect ratio with the given short side (e.g. 9:16 @1080 -> 1080x1920)."""
    if aw >= ah:
        return int(round(short * aw / ah / 2)) * 2, short // 2 * 2
    return short // 2 * 2, int(round(short * ah / aw / 2)) * 2


def aspect_of(w: int, h: int) -> str | None:
    """Label of the preset matching w:h, or None for a custom ratio."""
    for label, aw, ah in ASPECTS:
        if abs(w / h - aw / ah) < 0.01:
            return label
    return None


def ratio_text(w: int, h: int) -> str:
    label = aspect_of(w, h)
    if label:
        return label.split()[0]
    g = math.gcd(w, h)
    return f"{w // g}:{h // g}" if w // g < 100 else f"{w / h:.2f}:1"


def clamp_crop(a: float, b: float) -> tuple[float, float]:
    a, b = max(0.0, a), max(0.0, b)
    if a + b > MAX_CROP:
        k = MAX_CROP / (a + b)
        a, b = a * k, b * k
    return a, b


@dataclass
class Box:
    """A placed picture in canvas pixels."""
    cx: float  # centre of the full (uncropped) picture
    cy: float
    fw: float  # full picture size after fit and scale
    fh: float
    crop: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # l, r, t, b (source orientation)
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False

    def visible(self) -> tuple[float, float, float, float]:
        """Visible (cropped) rect before rotation: x, y, w, h. Flips mirror which side was cut."""
        l, r, t, b = self.crop
        if self.flip_h:
            l, r = r, l
        if self.flip_v:
            t, b = b, t
        return (self.cx - self.fw / 2 + l * self.fw, self.cy - self.fh / 2 + t * self.fh,
                self.fw * (1 - l - r), self.fh * (1 - t - b))

    def rotate_point(self, x: float, y: float) -> tuple[float, float]:
        a = math.radians(self.rotation)
        dx, dy = x - self.cx, y - self.cy
        return self.cx + dx * math.cos(a) - dy * math.sin(a), self.cy + dx * math.sin(a) + dy * math.cos(a)

    def corners(self) -> list[tuple[float, float]]:
        """Visible rect corners after rotation: top-left, top-right, bottom-right, bottom-left."""
        x, y, w, h = self.visible()
        return [self.rotate_point(px, py) for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]

    def bbox(self) -> tuple[float, float, float, float]:
        pts = self.corners()
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs), min(ys), max(xs), max(ys)

    def to_local(self, x: float, y: float) -> tuple[float, float]:
        """Point in the picture's own axes (unrotated), relative to its centre."""
        a = math.radians(-self.rotation)
        dx, dy = x - self.cx, y - self.cy
        return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)


def fit_factors(fit: str, src_w: float, src_h: float, W: float, H: float, seq_w: float) -> tuple[float, float]:
    if fit == "stretch":
        return W / src_w, H / src_h
    if fit == "fill":
        f = max(W / src_w, H / src_h)
    elif fit == "none":
        f = W / seq_w  # one source pixel per sequence pixel
    else:
        f = min(W / src_w, H / src_h)
    return f, f


def clip_box(clip, src_w: float, src_h: float, W: float, H: float, seq_w: float, seq_h: float) -> Box:
    """Where a media clip's picture lands on a W x H canvas."""
    fx, fy = fit_factors(clip.fit, src_w, src_h, W, H, seq_w)
    fw = src_w * fx * clip.scale * clip.scale_x
    fh = src_h * fy * clip.scale * clip.scale_y
    l, r = clamp_crop(clip.crop_l, clip.crop_r)
    t, b = clamp_crop(clip.crop_t, clip.crop_b)
    return Box(W / 2 + clip.pos_x * W / seq_w, H / 2 + clip.pos_y * H / seq_h, fw, fh, (l, r, t, b),
               clip.rotation, clip.flip_h, clip.flip_v)


def reset_transform(clip) -> None:
    clip.fit, clip.scale, clip.scale_x, clip.scale_y = "fit", 1.0, 1.0, 1.0
    clip.pos_x = clip.pos_y = clip.rotation = 0.0
    clip.flip_h = clip.flip_v = False
    clip.crop_l = clip.crop_r = clip.crop_t = clip.crop_b = 0.0
