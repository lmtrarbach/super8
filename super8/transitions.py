"""Transition catalogue and pixel compositing for video transitions.

Layers are float32 premultiplied RGBA arrays (H, W, 4) in 0..1, or None for "nothing" (transparent).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

VIDEO_TRANSITIONS = {
    "dissolve": "Cross Dissolve",
    "dip_black": "Dip to Black",
    "dip_white": "Dip to White",
    "wipe_right": "Wipe Right",
    "wipe_left": "Wipe Left",
    "wipe_down": "Wipe Down",
    "wipe_up": "Wipe Up",
    "push_left": "Push Left",
    "push_right": "Push Right",
    "iris": "Iris Round",
    "graphic_sweep": "Graphic Sweep",
    "graphic_bars": "Graphic Bars",
}
GRAPHIC_COLORS = [(0.11, 0.14, 0.20), (1.0, 0.77, 0.2), (0.76, 0.09, 0.36), (0.24, 0.44, 0.71)]
AUDIO_TRANSITIONS = {
    "crossfade": "Constant Power",
    "crossfade_lin": "Constant Gain",
}
ALL_TRANSITIONS = {**VIDEO_TRANSITIONS, **AUDIO_TRANSITIONS}
DEFAULT_VIDEO = "dissolve"
DEFAULT_AUDIO = "crossfade"


def for_track(kind: str, track_kind: str) -> str:
    """Map a transition onto a track of the other type (video dropped on audio -> crossfade...)."""
    if track_kind == "audio":
        return kind if kind in AUDIO_TRANSITIONS else DEFAULT_AUDIO
    return kind if kind in VIDEO_TRANSITIONS else DEFAULT_VIDEO


def short_label(kind: str) -> str:
    return ALL_TRANSITIONS.get(kind, kind)


def _smooth(p: float) -> float:
    return p * p * (3 - 2 * p)


@lru_cache(maxsize=8)
def _grids(W: int, H: int):
    xs = np.arange(W, dtype=np.float32)[None, :, None]
    ys = np.arange(H, dtype=np.float32)[:, None, None]
    dist = np.sqrt((xs - W / 2) ** 2 + (ys - H / 2) ** 2)
    return xs, ys, dist


def _mix(A, B, m):
    """A*(1-m) + B*m where m is a scalar or a (H,W,1) mask."""
    return A * (1 - m) + B * m


def compose(kind: str, A: np.ndarray | None, B: np.ndarray | None, p: float, W: int, H: int) -> np.ndarray:
    zero = None
    if A is None or B is None:
        zero = np.zeros((H, W, 4), np.float32)
    A = zero if A is None else A
    B = zero if B is None else B
    p = float(min(1.0, max(0.0, p)))
    if kind in ("dip_black", "dip_white"):
        solid = np.zeros((H, W, 4), np.float32)
        solid[..., 3] = 1.0
        if kind == "dip_white":
            solid[..., :3] = 1.0
        return _mix(A, solid, p * 2) if p < 0.5 else _mix(solid, B, (p - 0.5) * 2)
    if kind.startswith("wipe_"):
        xs, ys, _ = _grids(W, H)
        q = _smooth(p)
        feather = max(2.0, 0.015 * max(W, H))
        if kind == "wipe_right":
            m = (q * (W + feather) - xs) / feather
        elif kind == "wipe_left":
            m = (xs - (W - q * (W + feather))) / feather
        elif kind == "wipe_down":
            m = (q * (H + feather) - ys) / feather
        else:
            m = (ys - (H - q * (H + feather))) / feather
        return _mix(A, B, np.clip(m, 0, 1))
    if kind in ("push_left", "push_right"):
        off = int(round(_smooth(p) * W))
        out = np.zeros_like(A)
        if kind == "push_left":
            out[:, :W - off] = A[:, off:]
            out[:, W - off:] = B[:, :off]
        else:
            out[:, off:] = A[:, :W - off]
            out[:, :off] = B[:, W - off:]
        return out
    if kind == "graphic_sweep":
        # a slanted dark panel with an accent edge sweeps across: covers A, then uncovers B
        xs, ys, _ = _grids(W, H)
        u = xs + (ys / H - 0.5) * 0.25 * W  # slant
        span = W * 1.35
        lead = _smooth(min(1.0, p * 2)) * span - 0.175 * W
        trail = _smooth(max(0.0, p * 2 - 1)) * span - 0.175 * W
        stripe = 0.04 * W
        panel = np.zeros((H, W, 4), np.float32)
        panel[..., 3] = 1
        panel[..., :3] = GRAPHIC_COLORS[0]
        accent = ((u > lead - stripe) & (u <= lead)) | ((u >= trail) & (u < trail + stripe) & (p > 0.5))
        panel[accent[..., 0], :3] = GRAPHIC_COLORS[1]
        out = np.where(u < trail, B, np.where(u < lead, panel, A))
        return out.astype(np.float32)
    if kind == "graphic_bars":
        # coloured horizontal bars slide in staggered, then slide out the other side
        xs = np.arange(W, dtype=np.float32)[None, :]
        n = 5
        out = np.empty_like(A)
        bar_h = H / n
        for i in range(n):
            y0, y1 = int(round(i * bar_h)), int(round((i + 1) * bar_h))
            d = i * 0.06
            pi = min(1.0, max(0.0, (p - d) / (1 - (n - 1) * 0.06)))
            lead = _smooth(min(1.0, pi * 2)) * W
            trail = _smooth(max(0.0, pi * 2 - 1)) * W
            if i % 2:  # alternate direction
                x = W - 1 - xs
            else:
                x = xs
            col = np.array([*GRAPHIC_COLORS[(i % 3) + 1], 1.0], np.float32)
            rows = slice(y0, y1)
            m_b = (x < trail)[..., None]
            m_p = ((x >= trail) & (x < lead))[..., None]
            out[rows] = np.where(m_b, B[rows], np.where(m_p, col, A[rows]))
        return out
    if kind == "iris":
        _, _, dist = _grids(W, H)
        feather = max(2.0, 0.02 * max(W, H))
        r = _smooth(p) * (float(np.hypot(W, H)) / 2 + feather)
        return _mix(A, B, np.clip((r - dist) / feather, 0, 1))
    return _mix(A, B, p)  # dissolve (and fallback)


def audio_gains(kind: str, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(outgoing, incoming) gain curves for progress p in 0..1."""
    p = np.clip(p, 0.0, 1.0)
    if kind == "crossfade_lin":
        return 1 - p, p
    return np.cos(p * np.pi / 2), np.sin(p * np.pi / 2)
