"""Engine tests: model edits, decoding, compositing, transitions, audio DSP, graphics (no main window)."""
import numpy as np
import pytest

from super8.audiofx import auto_threshold, denoise, envelope_db, find_silences
from super8.model import Clip, Project, carve, ripple_ranges, split_with_links


@pytest.fixture(scope="module")
def media(qapp, samples):
    from super8.media import AudioCache, decode_audio, probe
    p = Project.new()
    items = {n: probe(samples[n]) for n in ("clip_a.mp4", "clip_b.mp4", "logo.png")}
    for m in items.values():
        p.media[m.id] = m
    cache = AudioCache()
    for n in ("clip_a.mp4", "clip_b.mp4"):
        cache.samples[items[n].id] = decode_audio(items[n].path)
    return p, items, cache


def test_probe(media):
    _, items, _ = media
    a = items["clip_a.mp4"]
    assert (a.kind, a.width, a.height, a.has_audio) == ("video", 1280, 720, True)
    assert items["logo.png"].kind == "image"


def test_reader_is_frame_accurate_both_directions(media):
    from super8.media import VideoReader
    r = VideoReader(media[1]["clip_a.mp4"].path)
    for t in (5.0, 0.5, 2.0, 0.0, 3.3):
        r.frame_at(t)
        assert abs(r.cur_t - t) < 1 / 30 + 1e-6
    r.close()


def test_carve_split_roundtrip():
    p = Project.new()
    v = p.video_tracks[0]
    v.clips = [Clip("a", "video", "m", 0, 6, link="L"), Clip("b", "video", "m", 6, 4, in_point=1)]
    p.audio_tracks[0].clips = [Clip("x", "audio", "m", 0, 6, link="L")]
    carve(v, 3, 4)
    assert [(c.start, c.duration, c.in_point) for c in v.clips] == [(0, 3, 0), (4, 2, 4), (6, 4, 1)]
    split_with_links(p, p.audio_tracks[0].clips[0], 1.5)
    assert len(p.audio_tracks[0].clips) == 2 and len(v.clips) == 4
    assert Project.from_dict(p.to_dict()).to_dict() == p.to_dict()


def test_ripple_ranges_cut_and_speed():
    def build():
        p = Project.new()
        p.video_tracks[0].clips = [Clip("A", "video", "m", 0, 3, link="1"), Clip("B", "video", "m", 3, 3, link="2")]
        p.audio_tracks[0].clips = [Clip("a", "audio", "m", 0, 3, link="1"), Clip("b", "audio", "m", 3, 3, link="2")]
        return p
    p = build()
    assert ripple_ranges(p, [(1.0, 2.0)], "cut") == pytest.approx(1.0)
    v = [(c.start, round(c.duration, 3), round(c.in_point, 3)) for c in p.video_tracks[0].clips]
    a = [(c.start, round(c.duration, 3), round(c.in_point, 3)) for c in p.audio_tracks[0].clips]
    assert v == a == [(0, 1.0, 0.0), (1.0, 1.0, 2.0), (2.0, 3, 0)]
    p = build()
    saved = ripple_ranges(p, [(1.0, 2.0)], "speed", 4)
    assert saved == pytest.approx(1 - 8 / 30)
    assert p.video_tracks[0].clips[1].speed == pytest.approx(3.75)


def test_composite_and_all_transitions(media):
    from super8.compositor import Compositor
    from super8.transitions import VIDEO_TRANSITIONS
    p, items, cache = media
    comp = Compositor(cache)
    v = p.video_tracks[0]
    v.clips = [Clip("A", "video", items["clip_a.mp4"].id, 0, 3), Clip("B", "video", items["clip_b.mp4"].id, 3, 3)]
    for kind in VIDEO_TRANSITIONS:
        v.clips[1].tin = kind
        frames = [comp.render(p, t, 160, 90) for t in (2.55, 3.0, 3.45)]
        assert all(f.shape == (90, 160, 3) for f in frames)
        assert not np.array_equal(frames[0], frames[2]), kind
    v.clips[1].tin = "dip_black"
    assert comp.render(p, 3.0, 160, 90).max() < 5  # midpoint of a dip is black
    v.clips = []


def test_constant_power_crossfade_keeps_level(media):
    from super8.compositor import mix_audio
    p, items, cache = media
    a = p.audio_tracks[0]
    a.clips = [Clip("a", "audio", items["clip_a.mp4"].id, 0, 3),
               Clip("b", "audio", items["clip_b.mp4"].id, 3, 3, in_point=1, tin="crossfade")]
    mix = mix_audio(p, cache, 2.0, 48000 * 2)
    rms = [np.sqrt((mix[i:i + 4800] ** 2).mean()) for i in range(0, len(mix), 4800)]
    assert max(rms) - min(rms) < 0.02
    a.clips = []


def _talk(sr=48000, seconds=20):
    rng = np.random.default_rng(0)
    t = np.arange(sr * seconds) / sr
    speech = 0.4 * np.sin(2 * np.pi * 220 * t) * ((t % 2.5) < 1.5)
    x = speech + 0.03 * rng.standard_normal(len(t))
    return np.stack([x, x], 1).astype(np.float32), t


def test_denoise_reduces_noise_not_speech():
    x, t = _talk()
    y = denoise(x, 0.8)
    db = lambda s: 10 * np.log10((s ** 2).mean())  # noqa: E731
    off, on = (t % 2.5) > 1.7, ((t % 2.5) > 0.2) & ((t % 2.5) < 1.3)
    assert db(x[off, 0]) - db(y[off, 0]) > 15
    assert abs(db(x[on, 0]) - db(y[on, 0])) < 2


def test_silence_detection():
    x, _ = _talk()
    env = envelope_db(lambda t0, n: x[int(t0 * 48000):int(t0 * 48000) + n], 0, 20, 48000)
    sil = find_silences(env, 0, auto_threshold(env), 0.5, 0.1, 30)
    assert len(sil) == 8
    assert sil[0] == pytest.approx((1.6, 2.4), abs=0.04)


def test_graphics_presets_render_and_animate(qapp):
    from super8.graphics import PRESETS, apply_preset, preset_duration, render_graphic
    for pid in PRESETS:
        c = Clip("t", "title", None, 0, preset_duration(pid))
        apply_preset(c, pid)
        mid = render_graphic(c, 480, 270, 1920, 1080, c.duration / 2)
        assert mid is not None, pid
        if c.anim_in and not c.roll:
            early = render_graphic(c, 480, 270, 1920, 1080, 0.05)
            assert early is None or early[0].tobytes() != mid[0].tobytes(), pid


def test_presets_scale_with_sequence(qapp):
    from super8.graphics import apply_preset, block_rect, template_from_clip
    lefts = []
    for w, h in ((1920, 1080), (1280, 720), (3840, 2160)):
        c = Clip("t", "title", None, 0, 5)
        apply_preset(c, "lower_bar", w, h)
        r = block_rect(c, w, h)
        assert 0 <= r.left() and r.right() <= w and r.bottom() <= h  # stays inside the frame
        lefts.append(r.left() / w)
        assert template_from_clip(c, "x", w, h)["pos_x"] == pytest.approx(-800, abs=1)
    assert max(lefts) - min(lefts) < 0.005  # same place on screen at every resolution


def test_left_aligned_text_grows_right(qapp):
    from super8.graphics import apply_preset, block_rect
    short, long = Clip("a", "title", None, 0, 5), Clip("b", "title", None, 0, 5)
    for c, name in ((short, "Al"), (long, "Alexandra Montgomery-Smith")):
        apply_preset(c, "lower_bar")
        c.text = name
    text_left = lambda c: block_rect(c, 1920, 1080).left()  # noqa: E731
    assert text_left(short) == pytest.approx(text_left(long), abs=1)


def test_donation_addresses_and_desktop_id(qapp, monkeypatch):
    from super8.ui import about, main_window
    assert [c for c, _ in about.DONATIONS] == ["Bitcoin", "Ethereum", "Solana"]
    assert about.ICON_PATH.exists()
    monkeypatch.setenv("SNAP", "/snap/code/123")  # another snap's environment leaking in
    monkeypatch.setenv("SNAP_NAME", "code")
    assert main_window.desktop_file_id() == "super8"
