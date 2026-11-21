"""Media probing, frame-accurate decoding and audio caching on top of PyAV (FFmpeg)."""
from __future__ import annotations

import os
import queue
import threading
import traceback
from collections import OrderedDict

import av
import numpy as np

from .model import MediaItem, new_id

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
DEFAULT_IMAGE_DURATION = 5.0
SAMPLE_RATE = 48000
PEAKS_PER_SEC = 100


def _pix_has_alpha(pix_fmt: str | None) -> bool:
    p = pix_fmt or ""
    return p.startswith(("yuva", "gbrap", "ya")) or p in {"rgba", "bgra", "argb", "abgr", "pal8"} or p.startswith(("rgba", "bgra"))


def _first_visual_stream(container):
    for s in container.streams.video:
        try:
            if s.disposition & av.stream.Disposition.attached_pic:
                continue  # cover art in an mp3, not real video
        except Exception:
            pass
        return s
    return None


def probe(path: str) -> MediaItem:
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    with av.open(path) as c:
        v = _first_visual_stream(c)
        a = c.streams.audio[0] if c.streams.audio else None
        if v is None and a is None:
            raise ValueError("no audio or video stream")
        if ext in IMAGE_EXT and v is not None:
            return MediaItem(new_id(), path, name, "image", DEFAULT_IMAGE_DURATION,
                             v.codec_context.width, v.codec_context.height, 0.0, True, False)
        dur = 0.0
        if c.duration:
            dur = c.duration / av.time_base
        else:
            for s in (v, a):
                if s is not None and s.duration:
                    dur = max(dur, float(s.duration * s.time_base))
        if dur <= 0:
            raise ValueError("could not determine duration")
        fps = float(v.average_rate) if v is not None and v.average_rate else 0.0
        return MediaItem(new_id(), path, name, "video" if v is not None else "audio", dur,
                         v.codec_context.width if v else 0, v.codec_context.height if v else 0,
                         fps, v is not None, a is not None)


# ============================================================ video readers
class VideoReader:
    """Sequential-friendly random access: decodes forward when possible, seeks otherwise."""

    has_alpha = False

    def __init__(self, path: str):
        self.container = av.open(path)
        self.stream = _first_visual_stream(self.container)
        self.stream.thread_type = "AUTO"
        tb = self.stream.time_base
        self.offset = float(self.stream.start_time * tb) if self.stream.start_time is not None else 0.0
        self.has_alpha = _pix_has_alpha(self.stream.codec_context.pix_fmt)
        self.gen = None
        self.cur = None
        self.cur_t = -1.0
        self.nxt = None
        self.eof = False

    def _time(self, f) -> float:
        if f.pts is None:
            return self.cur_t + 1e-3
        return float(f.pts * f.time_base) - self.offset

    def _seek(self, t: float) -> None:
        ts = int((t + self.offset) / self.stream.time_base)
        try:
            self.container.seek(max(ts, 0), stream=self.stream, backward=True, any_frame=False)
        except av.error.FFmpegError:
            self.container.seek(0, stream=self.stream)
        self.gen = self.container.decode(self.stream)
        self.cur, self.nxt, self.eof = None, None, False

    def frame_at(self, t: float):
        t = max(0.0, t)
        forward_ok = self.gen is not None and self.cur is not None and self.cur_t <= t + 1e-6 and t - self.cur_t < 2.0
        if not forward_ok:
            if self.cur is not None and self.eof and t >= self.cur_t:
                return self.cur  # past the end: hold last frame
            self._seek(t)
        while not self.eof:
            if self.nxt is None:
                try:
                    self.nxt = next(self.gen)
                except (StopIteration, av.error.EOFError):
                    self.eof = True
                    break
                except av.error.FFmpegError:
                    continue  # corrupt packet; keep going
            nt = self._time(self.nxt)
            if self.cur is None or nt <= t + 1e-6:
                self.cur, self.cur_t, self.nxt = self.nxt, nt, None
            else:
                break
        return self.cur

    def close(self):
        try:
            self.container.close()
        except Exception:
            pass


class ImageReader:
    def __init__(self, path: str):
        with av.open(path) as c:
            s = c.streams.video[0]
            self.frame = next(c.decode(s))
            self.has_alpha = _pix_has_alpha(self.frame.format.name)

    def frame_at(self, t: float):
        return self.frame

    def close(self):
        pass


class ReaderPool:
    """LRU pool of open decoders keyed by path (one pool per consumer thread)."""

    def __init__(self, size: int = 10):
        self.size = size
        self.readers: OrderedDict[str, object] = OrderedDict()
        self.failed: set[str] = set()

    def get(self, media: MediaItem):
        key = media.path
        if key in self.readers:
            self.readers.move_to_end(key)
            return self.readers[key]
        if key in self.failed:
            return None
        try:
            r = ImageReader(key) if media.kind == "image" else VideoReader(key)
        except Exception:
            traceback.print_exc()
            self.failed.add(key)
            return None
        self.readers[key] = r
        while len(self.readers) > self.size:
            _, old = self.readers.popitem(last=False)
            old.close()
        return r

    def close(self):
        for r in self.readers.values():
            r.close()
        self.readers.clear()


# ============================================================ audio / thumbs
def decode_audio(path: str, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode the whole first audio stream to float32 stereo (n, 2)."""
    chunks = []
    with av.open(path) as c:
        s = c.streams.audio[0]
        s.thread_type = "AUTO"
        res = av.AudioResampler(format="flt", layout="stereo", rate=sr)
        for frame in c.decode(s):
            for f in res.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1, 2))
        for f in res.resample(None):
            chunks.append(f.to_ndarray().reshape(-1, 2))
    if not chunks:
        return np.zeros((0, 2), np.float32)
    return np.ascontiguousarray(np.concatenate(chunks), dtype=np.float32)


def compute_peaks(samples: np.ndarray, sr: int = SAMPLE_RATE) -> np.ndarray:
    step = sr // PEAKS_PER_SEC
    n = len(samples) // step
    if n == 0:
        return np.zeros(0, np.float32)
    return np.abs(samples[: n * step]).reshape(n, step * 2).max(axis=1)


def thumbnail_rgb(media: MediaItem, width: int = 160) -> np.ndarray | None:
    if not media.has_video:
        return None
    if media.kind == "image":
        f = ImageReader(media.path).frame
    else:
        r = VideoReader(media.path)
        try:
            f = r.frame_at(min(1.0, media.duration * 0.1))
        finally:
            r.close()
    if f is None:
        return None
    h = max(2, int(round(width * f.height / max(1, f.width))))
    return f.reformat(width=width, height=h, format="rgb24").to_ndarray()


FILMSTRIP_HEIGHT = 54
FILMSTRIP_MAX_FRAMES = 360


def filmstrip(media: MediaItem, height: int = FILMSTRIP_HEIGHT, max_frames: int = FILMSTRIP_MAX_FRAMES,
              alive=lambda: True):
    """Small frames spread over a video for the timeline's filmstrip: (times array, list of rgb arrays).

    Short clips are sampled evenly (frame-accurate); long ones decode keyframes only, which is
    many times faster and still gives a frame every couple of seconds."""
    if media.kind != "video" or not media.has_video or media.duration <= 0:
        return None
    step = max(media.duration / max_frames, 1 / max(1.0, media.fps or 30.0))
    times, frames = [], []
    with av.open(media.path) as c:
        s = _first_visual_stream(c)
        w = max(2, int(round(height * s.codec_context.width / max(1, s.codec_context.height))) // 2 * 2)
        offset = float(s.start_time * s.time_base) if s.start_time is not None else 0.0
        long_clip = media.duration > 180
        if long_clip:
            s.codec_context.skip_frame = "NONKEY"
        s.thread_type = "AUTO"
        nxt = 0.0
        for f in c.decode(s):
            if not alive():
                return None
            if f.pts is None:
                continue
            t = float(f.pts * f.time_base) - offset
            if t + 1e-6 < nxt:
                continue
            times.append(max(0.0, t))
            frames.append(f.reformat(width=w, height=height, format="rgb24").to_ndarray())
            nxt = t + step
    if not frames:
        return None
    return np.array(times), frames


class AudioCache:
    """Decoded audio per media id, plus noise-reduced variants computed on demand in the background.

    Reads are lock-free (dict lookups are atomic under the GIL)."""

    MAX_VARIANTS_PER_MEDIA = 2

    def __init__(self):
        self.samples: dict[str, np.ndarray] = {}
        self.peaks: dict[str, np.ndarray] = {}
        self.variants: dict[tuple[str, float], np.ndarray] = {}
        self.pending: set[tuple[str, float]] = set()
        self.lock = threading.Lock()
        self.on_variant = None  # callback(media_id, strength) from a worker thread
        self.on_variant_progress = None  # callback(media_id, fraction)

    @staticmethod
    def _key(media_id: str, strength: float) -> tuple[str, float]:
        return media_id, round(float(strength), 2)

    def get(self, media_id: str | None, denoise: float = 0.0, blocking: bool = False) -> np.ndarray | None:
        base = self.samples.get(media_id)
        if base is None or denoise <= 0:
            return base
        key = self._key(media_id, denoise)
        v = self.variants.get(key)
        if v is not None:
            return v
        if blocking:
            return self._compute(key)
        self.request(media_id, denoise)
        return base  # preview falls back to the raw audio until processing is done

    def ready(self, media_id: str, denoise: float) -> bool:
        return denoise <= 0 or self._key(media_id, denoise) in self.variants

    def request(self, media_id: str, denoise: float) -> None:
        if denoise <= 0 or media_id not in self.samples:
            return
        key = self._key(media_id, denoise)
        with self.lock:
            if key in self.variants or key in self.pending:
                return
            self.pending.add(key)
        threading.Thread(target=self._compute, args=(key,), daemon=True).start()

    def _compute(self, key) -> np.ndarray:
        from .audiofx import denoise as run_denoise
        mid, strength = key
        prog = (lambda f: self.on_variant_progress(mid, f)) if self.on_variant_progress else None
        try:
            v = run_denoise(self.samples[mid], strength, progress=prog)
        except Exception:
            traceback.print_exc()
            v = self.samples[mid]
        with self.lock:
            old = [k for k in self.variants if k[0] == mid]
            for k in old[: max(0, len(old) - self.MAX_VARIANTS_PER_MEDIA + 1)]:
                del self.variants[k]
            self.variants[key] = v
            self.pending.discard(key)
        if self.on_variant:
            self.on_variant(mid, strength)
        return v


class MediaLoader:
    """Background worker that produces thumbnails and decoded audio for imported media.

    `on_thumb(media_id, rgb_array)` and `on_audio(media_id)` are called from the worker
    thread; the UI wires them to Qt signals so they are delivered on the GUI thread.
    """

    def __init__(self, cache: AudioCache, on_thumb, on_audio, on_error, on_filmstrip=None):
        self.cache = cache
        self.on_thumb, self.on_audio, self.on_error = on_thumb, on_audio, on_error
        self.on_filmstrip = on_filmstrip
        self.q: queue.Queue = queue.Queue()
        self.alive = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def enqueue(self, media: MediaItem):
        self.q.put((media, "basic"))

    def stop(self, timeout: float = 5.0):
        """Stop delivering results (the receiver may be going away) and end the thread."""
        self.alive = False
        self.q.put(None)
        self.thread.join(timeout)

    def _run(self):
        while True:
            item = self.q.get()
            if item is None or not self.alive:
                return
            m, phase = item
            try:
                if phase == "strip":
                    strip = filmstrip(m, alive=lambda: self.alive)
                    if strip is not None and self.alive and self.on_filmstrip:
                        self.on_filmstrip(m.id, strip)
                    continue
                if m.has_video:
                    thumb = thumbnail_rgb(m)
                    if thumb is not None and self.alive:
                        self.on_thumb(m.id, thumb)
                if m.has_audio and m.id not in self.cache.samples and self.alive:
                    data = decode_audio(m.path)
                    self.cache.peaks[m.id] = compute_peaks(data)
                    self.cache.samples[m.id] = data
                    if self.alive:
                        self.on_audio(m.id)
                if m.kind == "video" and self.on_filmstrip:
                    # second pass, after every import's thumbnail and audio are done
                    self.q.put((m, "strip"))
            except Exception as e:
                traceback.print_exc()
                if self.alive:
                    self.on_error(m.id, str(e))
