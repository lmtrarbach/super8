"""Render the sequence to an H.264/AAC MP4 (or MKV/MOV) file."""
from __future__ import annotations

import math
import threading
from fractions import Fraction

import av

from .compositor import Compositor, mix_audio
from .media import AudioCache
from .model import Project

AAC_FRAME = 1024


def export_project(project: Project, cache: AudioCache, path: str, crf: int = 20, preset: str = "medium",
                   progress=None, cancel: threading.Event | None = None) -> bool:
    """Blocking export. `progress(done_frames, total_frames)`. Returns False if cancelled."""
    W, H = project.width // 2 * 2, project.height // 2 * 2
    sr = project.sample_rate
    rate = Fraction(project.fps).limit_denominator(1001)
    total = project.duration()
    if total <= 0:
        raise ValueError("The timeline is empty.")
    nframes = max(1, math.ceil(total * float(rate) - 1e-6))
    has_audio = any(c for t in project.audio_tracks if not t.muted for c in t.clips)

    comp = Compositor(cache)
    out = av.open(path, "w")
    try:
        vs = out.add_stream("libx264", rate=rate)
        vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
        vs.options = {"crf": str(crf), "preset": preset, "movflags": "+faststart"}
        vtb = 1 / rate
        vs.codec_context.time_base = vtb
        aus = None
        if has_audio:
            aus = out.add_stream("aac", rate=sr, layout="stereo")
            aus.bit_rate = 192_000
        written = 0  # audio samples written
        for i in range(nframes):
            if cancel is not None and cancel.is_set():
                return False
            t = i / float(rate)
            img = comp.render(project, t, W, H)
            vf = av.VideoFrame.from_ndarray(img, format="rgb24")
            vf.pts, vf.time_base = i, vtb
            for pkt in vs.encode(vf):
                out.mux(pkt)
            if aus is not None:
                target = int(round((i + 1) / float(rate) * sr))
                last = i == nframes - 1
                while written + AAC_FRAME <= target or (last and written < target):
                    buf = mix_audio(project, cache, written / sr, AAC_FRAME, blocking=True)
                    af = av.AudioFrame.from_ndarray(buf.T.copy(), format="fltp", layout="stereo")
                    af.sample_rate, af.pts, af.time_base = sr, written, Fraction(1, sr)
                    for pkt in aus.encode(af):
                        out.mux(pkt)
                    written += AAC_FRAME
            if progress:
                progress(i + 1, nframes)
        for pkt in vs.encode(None):
            out.mux(pkt)
        if aus is not None:
            for pkt in aus.encode(None):
                out.mux(pkt)
        return True
    finally:
        out.close()
        comp.close()
