"""Generate small test clips (colour bars + tone) so the editor can be tried without real footage."""
import os
import sys
from fractions import Fraction

import av
import numpy as np


def talk_audio(seconds, sr=48000, seed=1):
    """Speech-like bursts (modulated harmonics) separated by pauses of random length, over hiss + hum."""
    rng = np.random.default_rng(seed)
    n = seconds * sr
    t = np.arange(n) / sr
    voice = np.zeros(n, np.float32)
    pos = int(0.4 * sr)
    while pos < n:
        talk = int(rng.uniform(0.8, 3.0) * sr)
        seg = np.arange(min(talk, n - pos)) / sr
        f0 = rng.uniform(110, 190)
        syll = 0.5 + 0.5 * np.sin(2 * np.pi * rng.uniform(3, 6) * seg) ** 2  # syllable rhythm
        tone = sum(np.sin(2 * np.pi * f0 * k * seg + k) / k for k in range(1, 6))
        env = np.minimum(1, np.minimum(seg / 0.05, (seg[-1] - seg + 1e-3) / 0.05)) if len(seg) else seg
        voice[pos:pos + len(seg)] = 0.22 * tone * syll * env
        pos += talk + int(rng.choice([0.25, 0.4, 1.2, 1.8, 2.6]) * sr)  # short breaths and long rambling pauses
    noise = 0.02 * rng.standard_normal(n) + 0.012 * np.sin(2 * np.pi * 60 * t)  # hiss + mains hum
    return (voice + noise).astype(np.float32)


def make_clip(path, seconds=6, w=1280, h=720, fps=30, hue=0, tone=440.0, audio=None):
    out = av.open(path, "w")
    vs = out.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
    aus = out.add_stream("aac", rate=48000, layout="stereo")
    yy, xx = np.mgrid[0:h, 0:w]
    sr, a_written = 48000, 0
    for i in range(seconds * fps):
        t = i / fps
        r = (127 + 127 * np.sin(xx / 90 + t * 2 + hue)).astype(np.uint8)
        g = (127 + 127 * np.sin(yy / 70 + t * 3 + hue * 2)).astype(np.uint8)
        b = np.full_like(r, (hue * 60) % 255)
        img = np.dstack([r, g, b])
        cx = int((w - 120) * (0.5 + 0.5 * np.sin(t * 1.5)))
        img[h // 2 - 60:h // 2 + 60, cx:cx + 120] = 255  # moving white box
        bar = int(w * (i + 1) / (seconds * fps))
        img[h - 16:, :bar] = (255, 40, 40)  # progress bar to check frame accuracy
        f = av.VideoFrame.from_ndarray(img, format="rgb24")
        f.pts, f.time_base = i, Fraction(1, fps)
        for p in vs.encode(f):
            out.mux(p)
        while a_written < (i + 1) / fps * sr:
            n = 1024
            ts = (np.arange(a_written, a_written + n) / sr)
            if audio is not None:
                s = audio[a_written:a_written + n]
                s = np.pad(s, (0, n - len(s)))
            else:
                s = (0.3 * np.sin(2 * np.pi * tone * ts)).astype(np.float32)
            af = av.AudioFrame.from_ndarray(np.vstack([s, s]), format="fltp", layout="stereo")
            af.sample_rate, af.pts, af.time_base = sr, a_written, Fraction(1, sr)
            for p in aus.encode(af):
                out.mux(p)
            a_written += n
    for s in (vs, aus):
        for p in s.encode(None):
            out.mux(p)
    out.close()


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "samples"
    os.makedirs(d, exist_ok=True)
    make_clip(os.path.join(d, "clip_a.mp4"), hue=0, tone=440)
    make_clip(os.path.join(d, "clip_b.mp4"), hue=2, tone=660)
    make_clip(os.path.join(d, "talk.mp4"), seconds=30, w=640, h=360, hue=4, audio=talk_audio(30))
    # a logo with transparency
    img = np.zeros((256, 256, 4), np.uint8)
    yy, xx = np.mgrid[0:256, 0:256]
    m = (xx - 128) ** 2 + (yy - 128) ** 2 < 110 ** 2
    img[m] = (255, 200, 0, 230)
    fr = av.VideoFrame.from_ndarray(img, format="rgba")
    with av.open(os.path.join(d, "logo.png"), "w") as o:
        s = o.add_stream("png", rate=1)
        s.width, s.height, s.pix_fmt = 256, 256, "rgba"
        for p in s.encode(fr):
            o.mux(p)
        for p in s.encode(None):
            o.mux(p)
    print("samples written to", d)
