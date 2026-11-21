"""Audio analysis & processing: loudness envelope, silence detection, spectral noise reduction."""
from __future__ import annotations

import numpy as np

ENV_WIN = 0.01  # seconds per envelope bin


# ===================================================================== silence detection
def envelope_db(mix_fn, t0: float, t1: float, sr: int, win: float = ENV_WIN, chunk_s: float = 20.0) -> np.ndarray:
    """RMS level (dBFS) of the sequence mix in `win`-second bins between t0 and t1.

    `mix_fn(t, n)` returns an (n, 2) float array of the mix starting at time t.
    """
    hop = max(1, int(round(win * sr)))
    total = int(round((t1 - t0) * sr)) // hop
    out = np.empty(total, np.float32)
    per_chunk = max(1, int(chunk_s * sr) // hop)
    i = 0
    while i < total:
        k = min(per_chunk, total - i)
        buf = mix_fn(t0 + i * hop / sr, k * hop)
        power = (buf.astype(np.float32) ** 2).mean(axis=1).reshape(k, hop).mean(axis=1)
        out[i:i + k] = 10 * np.log10(power + 1e-12)
        i += k
    return out


def auto_threshold(db: np.ndarray) -> float:
    """Pick a threshold between the noise floor and the speech level."""
    if len(db) == 0:
        return -40.0
    floor = float(np.percentile(db, 8))
    loud = float(np.percentile(db, 95))
    if loud - floor < 6:  # nearly constant level: nothing meaningful to cut
        return floor - 1
    return float(min(loud - 6, floor + max(6.0, 0.35 * (loud - floor))))


def find_silences(db: np.ndarray, t0: float, thr: float, min_silence: float, padding: float,
                  fps: float, win: float = ENV_WIN) -> list[tuple[float, float]]:
    """Silent ranges (timeline seconds) at least `min_silence` long, shrunk by `padding` on each side
    so breaths and word tails survive. Leading/trailing silence of the range is not padded."""
    if len(db) == 0:
        return []
    silent = np.concatenate([[False], db < thr, [False]])
    edges = np.flatnonzero(np.diff(silent.astype(np.int8)))
    starts, ends = edges[0::2], edges[1::2]
    out = []
    n = len(db)
    for s, e in zip(starts, ends):
        a, b = t0 + s * win, t0 + e * win
        if b - a < min_silence:
            continue
        if s > 0:
            a += padding
        if e < n:
            b -= padding
        a, b = round(a * fps) / fps, round(b * fps) / fps
        if b - a >= 1 / fps:
            out.append((a, b))
    return out


# ===================================================================== noise reduction
def _frames_view(x: np.ndarray, n_fft: int, hop: int, start: int, count: int) -> np.ndarray:
    idx = start * hop + np.arange(count)[:, None] * hop + np.arange(n_fft)[None, :]
    return x[idx]


def denoise(samples: np.ndarray, strength: float, sr: int = 48000, n_fft: int = 2048, hop: int = 512,
            batch: int = 1024, progress=None) -> np.ndarray:
    """Spectral-gating noise reduction for a stereo (n, 2) float32 signal.

    The noise profile is learned from the quietest ~10% of frames, so it works best when the recording
    has some pauses (as speech nearly always does). `strength` 0..1 controls over-subtraction and the
    maximum attenuation (6 dB at 0 up to 30 dB at 1)."""
    strength = float(min(1.0, max(0.0, strength)))
    n = len(samples)
    if n < n_fft * 2 or strength <= 0:
        return samples.copy()
    pad = n_fft
    x = np.pad(samples.astype(np.float32), ((pad, pad + hop), (0, 0)))
    mono = x.mean(axis=1)
    win = np.hanning(n_fft + 1)[:-1].astype(np.float32)
    n_frames = 1 + (len(x) - n_fft) // hop

    # 1) frame energies -> pick the quietest frames as the noise sample
    blocks = mono[: (len(mono) // hop) * hop].reshape(-1, hop)
    block_e = (blocks ** 2).mean(axis=1)
    per = n_fft // hop
    csum = np.concatenate([[0.0], np.cumsum(block_e)])
    energy = (csum[per:per + n_frames] - csum[:n_frames]) / per
    energy = energy[:n_frames]
    k = max(10, int(len(energy) * 0.10))
    quiet = np.argsort(energy)[:k]
    noise_mag = np.zeros(n_fft // 2 + 1, np.float32)
    for i in range(0, len(quiet), batch):
        idx = quiet[i:i + batch]
        fr = mono[idx[:, None] * hop + np.arange(n_fft)[None, :]] * win
        noise_mag += np.abs(np.fft.rfft(fr, axis=1)).sum(axis=0)
    noise_mag /= len(quiet)

    alpha = 1.0 + 1.5 * strength  # over-subtraction
    floor = 10 ** (-(6 + 24 * strength) / 20)
    fk = np.array([0.15, 0.2, 0.3, 0.2, 0.15], np.float32)  # frequency smoothing kernel

    out = np.zeros_like(x)
    wsum = np.zeros(len(x), np.float32)
    for f0 in range(0, n_frames, batch):
        cnt = min(batch, n_frames - f0)
        # one extra frame of context each side for temporal smoothing
        c0, c1 = max(0, f0 - 1), min(n_frames, f0 + cnt + 1)
        X = np.stack([np.fft.rfft(_frames_view(x[:, ch], n_fft, hop, c0, c1 - c0) * win, axis=1)
                      for ch in range(2)])
        mag = np.abs(X).mean(axis=0)
        gain = 1.0 - alpha * noise_mag[None, :] / (mag + 1e-9)
        gain = np.clip(gain, floor, 1.0)
        # smooth over frequency then time to avoid "musical noise"
        g = np.pad(gain, ((0, 0), (2, 2)), mode="edge")
        gain = sum(fk[j] * g[:, j:j + gain.shape[1]] for j in range(5))
        gp = np.pad(gain, ((1, 1), (0, 0)), mode="edge")
        gain = 0.25 * gp[:-2] + 0.5 * gp[1:-1] + 0.25 * gp[2:]
        lo = f0 - c0
        Y = X[:, lo:lo + cnt] * gain[None, lo:lo + cnt]
        y = np.fft.irfft(Y, n=n_fft, axis=2).astype(np.float32) * win
        for ch in range(2):
            _ola(out[:, ch], y[ch], f0, hop)
        _ola(wsum, np.broadcast_to(win * win, (cnt, n_fft)), f0, hop)
        if progress:
            progress(min(1.0, (f0 + cnt) / n_frames))
    nz = wsum > 1e-3
    out[nz] /= wsum[nz, None]
    return np.ascontiguousarray(out[pad:pad + n], dtype=np.float32)


def _ola(dst: np.ndarray, frames: np.ndarray, f0: int, hop: int) -> None:
    """Overlap-add frames (count, n_fft) into dst starting at frame f0."""
    count, n_fft = frames.shape
    per = n_fft // hop
    for j in range(per):  # frames j, j+per, ... don't overlap each other -> vectorised add
        sub = frames[j::per]
        if len(sub) == 0:
            continue
        start = (f0 + j) * hop
        seg = dst[start:start + len(sub) * n_fft]
        seg[: len(sub) * n_fft] += sub.reshape(-1)[: len(seg)]
