"""Renders the sequence: video layers -> RGB frame, audio tracks -> stereo mix."""
from __future__ import annotations

from collections import OrderedDict

import numpy as np

from .media import AudioCache, ReaderPool
from .graphics import render_graphic, title_cache_key
from .model import Clip, Project, transition_windows
from .transitions import audio_gains, compose

MAX_LAYER = 8192


def blit(canvas: np.ndarray, src: np.ndarray, x0: int, y0: int, alpha: float) -> None:
    H, W = canvas.shape[:2]
    h, w = src.shape[:2]
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(W, x0 + w), min(H, y0 + h)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    s = src[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
    dst = canvas[cy0:cy1, cx0:cx1]
    if s.shape[2] == 4:
        a = s[..., 3:4].astype(np.float32) * (alpha / 255.0)
        rgb = s[..., :3]
    elif alpha >= 0.999:
        dst[...] = s
        return
    else:
        a = np.float32(alpha)
        rgb = s
    dst[...] = (rgb * a + dst * (1.0 - a) + 0.5).astype(np.uint8)


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
        canvas = np.zeros((H, W, 3), np.uint8)
        for track in project.video_tracks:  # V1 is the bottom layer
            if track.muted:
                continue
            tx = transition_at(track, t)
            if tx is not None:
                kind, A, B, p = tx
                la = self._layer(project, A, t, W, H) if A is not None else None
                lb = self._layer(project, B, t, W, H) if B is not None else None
                over(canvas, compose(kind, la, lb, p, W, H))
                continue
            clip = track.clip_at(t)
            if clip is not None:
                placed = self._place(project, clip, t, W, H)
                if placed is not None:
                    blit(canvas, *placed)
        return canvas

    def _place(self, project, clip: Clip, t, W, H):
        """Pixels for one clip at time t: (array rgb|rgba uint8, x0, y0, alpha) or None."""
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
            return None if res is None else (res[0], res[1], res[2], alpha)
        media = project.media.get(clip.media_id)
        if media is None:
            return None
        reader = self.readers.get(media)
        if reader is None:
            return None
        frame = reader.frame_at(clip.source_time(t))
        if frame is None:
            return None
        fit = min(W / frame.width, H / frame.height) * clip.scale
        w = min(MAX_LAYER, max(2, int(round(frame.width * fit))))
        h = min(MAX_LAYER, max(2, int(round(frame.height * fit))))
        x0 = int(round(W / 2 + clip.pos_x * W / project.width - w / 2))
        y0 = int(round(H / 2 + clip.pos_y * H / project.height - h / 2))
        if x0 >= W or y0 >= H or x0 + w <= 0 or y0 + h <= 0:
            return None
        fmt = "rgba" if reader.has_alpha else "rgb24"
        arr = frame.reformat(width=w, height=h, format=fmt, interpolation="BILINEAR").to_ndarray()
        return arr, x0, y0, alpha

    def _layer(self, project, clip: Clip, t, W, H) -> np.ndarray | None:
        """The clip as a full-canvas premultiplied float RGBA layer (used by transitions)."""
        placed = self._place(project, clip, t, W, H)
        if placed is None:
            return None
        src, x0, y0, alpha = placed
        h, w = src.shape[:2]
        cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x0 + w), min(H, y0 + h)
        layer = np.zeros((H, W, 4), np.float32)
        if cx1 <= cx0 or cy1 <= cy0:
            return layer
        s = src[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0].astype(np.float32) / 255.0
        a = (s[..., 3:4] if s.shape[2] == 4 else np.ones_like(s[..., :1])) * alpha
        layer[cy0:cy1, cx0:cx1, :3] = s[..., :3] * a
        layer[cy0:cy1, cx0:cx1, 3:4] = a
        return layer

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
