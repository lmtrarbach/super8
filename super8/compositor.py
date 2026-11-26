"""Renders the sequence: video layers -> RGB frame, audio tracks -> stereo mix."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QImage, QPainter, QTransform

from .media import AudioCache, ReaderPool
from .graphics import render_graphic, title_cache_key
from .transform import clip_box
from .model import Clip, Project, transition_windows
from .transitions import audio_gains, compose

MAX_LAYER = 8192


class Frame:
    """An RGB frame owned by a QImage, with a NumPy view onto the same pixels.

    QPainter composites into it (fast, SIMD, transforms) and NumPy code (transitions, fast copies)
    writes the very same memory. A QImage wrapping a NumPy buffer would copy on first paint, so the
    QImage has to own the memory."""

    def __init__(self, W: int, H: int):
        self.image = QImage(W, H, QImage.Format.Format_RGB888)
        self.image.fill(0)
        ptr = self.image.bits()
        ptr.setsize(self.image.sizeInBytes())
        self.array = np.ndarray((H, W, 3), np.uint8, buffer=ptr, strides=(self.image.bytesPerLine(), 3, 1))


@dataclass
class Placed:
    """A clip's picture ready to draw: `arr` is 1:1 with canvas pixels; the `src` region of it is drawn
    with its top-left at (x, y), then mirrored and rotated around (cx, cy)."""
    arr: np.ndarray  # uint8 RGB or straight-alpha RGBA
    x: int
    y: int
    src: tuple[int, int, int, int]  # sx, sy, sw, sh (the crop)
    alpha: float
    cx: float = 0.0
    cy: float = 0.0
    rotation: float = 0.0
    flip_h: bool = False
    flip_v: bool = False

    @property
    def transformed(self) -> bool:
        return bool(self.rotation % 360) or self.flip_h or self.flip_v

    def transform(self) -> QTransform:
        t = QTransform()
        t.translate(self.cx, self.cy)
        t.rotate(self.rotation)
        t.scale(-1 if self.flip_h else 1, -1 if self.flip_v else 1)  # mirror first, then rotate
        t.translate(-self.cx, -self.cy)
        return t


def paint(target: QImage, pl: Placed) -> None:
    """Draw a placed picture onto a QImage: crop (source rect), flips, rotation and opacity in one go."""
    arr = np.ascontiguousarray(pl.arr)  # already contiguous straight from the decoder
    h, w, ch = arr.shape
    fmt = QImage.Format.Format_RGBA8888 if ch == 4 else QImage.Format.Format_RGB888
    img = QImage(arr.data, w, h, ch * w, fmt)
    sx, sy, sw, sh = pl.src
    p = QPainter(target)
    if pl.transformed:
        if pl.rotation % 90:  # flips and quarter turns map pixels exactly: no filtering needed
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setTransform(pl.transform())
    p.setOpacity(max(0.0, min(1.0, pl.alpha)))
    p.drawImage(QRectF(pl.x, pl.y, sw, sh), img, QRectF(sx, sy, sw, sh))
    p.end()


def blit(frame: Frame, pl: Placed) -> None:
    """Composite a placed picture onto the frame."""
    if not pl.transformed and pl.arr.shape[2] == 3 and pl.alpha >= 0.999:  # opaque, axis-aligned: copy
        canvas = frame.array
        H, W = canvas.shape[:2]
        sx, sy, sw, sh = pl.src
        cx0, cy0 = max(0, pl.x), max(0, pl.y)
        cx1, cy1 = min(W, pl.x + sw), min(H, pl.y + sh)
        if cx1 > cx0 and cy1 > cy0:
            canvas[cy0:cy1, cx0:cx1] = pl.arr[sy + cy0 - pl.y:sy + cy1 - pl.y, sx + cx0 - pl.x:sx + cx1 - pl.x]
        return
    paint(frame.image, pl)


class Compositor:
    """Not thread-safe: create one per consumer thread (preview, export)."""

    def __init__(self, audio: AudioCache):
        self.audio = audio
        self.readers = ReaderPool()
        self.titles: OrderedDict = OrderedDict()

    def close(self):
        self.readers.close()

    # ------------------------------------------------------------ video
    def render(self, project: Project, t: float, W: int, H: int) -> np.ndarray:
        frame = Frame(W, H)
        for track in project.video_tracks:  # V1 is the bottom layer
            if track.muted:
                continue
            tx = transition_at(track, t)
            if tx is not None:
                kind, A, B, p = tx
                la = self._layer(project, A, t, W, H) if A is not None else None
                lb = self._layer(project, B, t, W, H) if B is not None else None
                over(frame.array, compose(kind, la, lb, p, W, H))
                continue
            clip = track.clip_at(t)
            if clip is not None:
                placed = self._place(project, clip, t, W, H)
                if placed is not None:
                    blit(frame, placed)
        # always a real copy: the view aliases the QImage's memory, which is freed with `frame`
        # (np.ascontiguousarray would return the view itself whenever rows have no padding)
        return frame.array.copy()

    def _place(self, project, clip: Clip, t, W, H):
        """The clip's picture at time t, ready to draw (a Placed), or None when nothing shows."""
        local = min(max(t - clip.start, 0.0), clip.duration)  # transitions may sample past the edges
        alpha = clip.opacity * clip.envelope(local)
        if alpha <= 0.002:
            return None
        if clip.kind == "title":
            key = title_cache_key(clip, W, H, project.width, project.height, local)
            if key in self.titles:
                self.titles.move_to_end(key)
            else:
                self.titles[key] = render_graphic(clip, W, H, project.width, project.height, local)
                while len(self.titles) > 64:
                    self.titles.popitem(last=False)
            res = self.titles[key]
            if res is None:
                return None
            arr, x0, y0 = res
            h, w = arr.shape[:2]
            return Placed(arr, x0, y0, (0, 0, w, h), alpha, x0 + w / 2, y0 + h / 2, clip.rotation)
        media = project.media.get(clip.media_id)
        if media is None:
            return None
        reader = self.readers.get(media)
        if reader is None:
            return None
        frame = reader.frame_at(clip.source_time(t))
        if frame is None:
            return None
        box = clip_box(clip, frame.width, frame.height, W, H, project.width, project.height)
        bx0, by0, bx1, by1 = box.bbox()
        if bx0 >= W or by0 >= H or bx1 <= 0 or by1 <= 0 or box.fw < 1 or box.fh < 1:
            return None  # off screen
        fw = min(MAX_LAYER, max(2, int(round(box.fw))))
        fh = min(MAX_LAYER, max(2, int(round(box.fh))))
        fmt = "rgba" if reader.has_alpha else "rgb24"
        arr = frame.reformat(width=fw, height=fh, format=fmt, interpolation="BILINEAR").to_ndarray()
        l, r, t, b = box.crop  # crop in source orientation: a source rect, no pixel copy
        sx, sy = int(round(l * fw)), int(round(t * fh))
        sw, sh = fw - sx - int(round(r * fw)), fh - sy - int(round(b * fh))
        if sw < 1 or sh < 1:
            return None
        x0 = int(round(box.cx - box.fw / 2)) + sx  # unflipped position; flips mirror around (cx, cy)
        y0 = int(round(box.cy - box.fh / 2)) + sy
        return Placed(arr, x0, y0, (sx, sy, sw, sh), alpha, box.cx, box.cy, box.rotation, box.flip_h, box.flip_v)

    def _layer(self, project, clip: Clip, t, W, H) -> np.ndarray | None:
        """The clip as a full-canvas premultiplied float RGBA layer (used by transitions)."""
        placed = self._place(project, clip, t, W, H)
        if placed is None:
            return None
        img = QImage(W, H, QImage.Format.Format_RGBA8888_Premultiplied)
        img.fill(0)
        paint(img, placed)
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        rgba = np.frombuffer(ptr, np.uint8).reshape(H, img.bytesPerLine())[:, :W * 4].reshape(H, W, 4)
        return rgba.astype(np.float32) / 255.0  # already premultiplied

    # ------------------------------------------------------------ audio
    def mix(self, project: Project, t0: float, n: int) -> np.ndarray:
        return mix_audio(project, self.audio, t0, n)


def over(canvas: np.ndarray, layer: np.ndarray) -> None:
    """Composite a premultiplied float layer over a uint8 RGB canvas in place."""
    canvas[...] = np.clip(layer[..., :3] * 255.0 + canvas * (1.0 - layer[..., 3:4]) + 0.5, 0, 255).astype(np.uint8)


def transition_at(track, t: float):
    """(kind, outgoing, incoming, progress) if t falls inside a transition on this track."""
    for t0, t1, kind, A, B in transition_windows(track):
        if t0 <= t < t1:
            return kind, A, B, (t - t0) / (t1 - t0)
        if t0 > t + 30:
            break
    return None


def mix_audio(project: Project, cache: AudioCache, t0: float, n: int, blocking: bool = False) -> np.ndarray:
    """Mix all audible audio clips for [t0, t0 + n/sr). `blocking` waits for noise-reduced variants
    (export) instead of falling back to the unprocessed audio (preview)."""
    sr = project.sample_rate
    out = np.zeros((n, 2), np.float32)
    t1 = t0 + n / sr
    for track in project.audio_tracks:
        if track.muted:
            continue
        # transitions extend clips past their edges and add crossfade gain curves
        span = {c.id: [c.start, c.end, []] for c in track.clips}
        for w0, w1, kind, A, B in transition_windows(track):
            if B is not None:
                span[B.id][0] = min(span[B.id][0], w0)
                span[B.id][2].append((w0, w1, kind, True))
            if A is not None:
                span[A.id][1] = max(span[A.id][1], w1)
                span[A.id][2].append((w0, w1, kind, False))
        for c in track.clips:
            s0, s1, curves = span[c.id]
            if s1 <= t0 or s0 >= t1 or c.volume <= 0:
                continue
            data = cache.get(c.media_id, c.denoise, blocking)
            if data is None:
                continue
            a = int(round((max(s0, t0) - t0) * sr))
            b = int(round((min(s1, t1) - t0) * sr))
            if b <= a:
                continue
            tt = t0 + np.arange(a, b, dtype=np.float64) / sr  # timeline time of each output sample
            pos = np.round((c.in_point + (tt - c.start) * c.speed) * sr).astype(np.int64)
            valid = (pos >= 0) & (pos < len(data))
            if not valid.any():
                continue
            local = np.clip(tt - c.start, 0, c.duration).astype(np.float32)
            gain = np.full(b - a, c.volume, np.float32)
            if c.fade_in > 0:
                gain *= np.clip(local / c.fade_in, 0, 1)
            if c.fade_out > 0:
                gain *= np.clip((c.duration - local) / c.fade_out, 0, 1)
            for w0, w1, kind, incoming in curves:
                g_out, g_in = audio_gains(kind, (tt - w0) / (w1 - w0))
                gain *= (g_in if incoming else g_out).astype(np.float32)
            seg = np.zeros((b - a, 2), np.float32)
            seg[valid] = data[pos[valid]]
            out[a:b] += seg * gain[:, None]
    np.clip(out, -1.0, 1.0, out=out)
    return out
