"""Main-window tests driven with simulated mouse/keyboard input (offscreen)."""
import av
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QInputDialog

from super8 import graphics
from super8.export import export_project

L = Qt.MouseButton.LeftButton
NM = Qt.KeyboardModifier.NoModifier


def two_clips(w):
    """clip_a [0,4) in 0 and clip_b [4,8) in 1 on V1/A1, linked, with source handles on both sides."""
    p = w.project
    for tr in p.tracks():
        tr.clips.clear()
    w.place_media(w.ids["clip_a.mp4"], 0, p.video_tracks[0])
    w.place_media(w.ids["clip_b.mp4"], 6, p.video_tracks[0])
    for tr in (p.video_tracks[0], p.audio_tracks[0]):
        a, b = tr.clips
        a.duration = 4.0
        b.start, b.in_point, b.duration = 4.0, 1.0, 4.0
    c = w.timeline.canvas
    c.pps, c.scroll_t = 100, 0
    w.changed()
    w.pump(0.05)
    return c


def state(tr):
    return [(round(x.start, 2), round(x.duration, 2), round(x.in_point, 2), round(x.speed, 3)) for x in tr.clips]


def drag(c, x0, x1, y, mods=NM):
    QTest.mousePress(c, L, mods, QPoint(int(x0), int(y)))
    for i in range(1, 9):
        QTest.mouseMove(c, QPoint(int(x0 + (x1 - x0) * i / 8), int(y)))
    QTest.mouseRelease(c, L, mods, QPoint(int(x1), int(y)))


def v1y(c, w):
    y, h = c.row_of(w.project.video_tracks[0])
    return y + h / 2 + 5


def test_import_and_sequence_match(win):
    assert set(win.ids) == {"clip_a.mp4", "clip_b.mp4", "talk.mp4", "logo.png"}
    win.place_media(win.ids["clip_a.mp4"], 0, None)
    assert (win.project.width, win.project.height, win.project.fps) == (1280, 720, 30.0)
    v, a = win.project.video_tracks[0].clips, win.project.audio_tracks[0].clips
    assert len(v) == len(a) == 1 and v[0].link == a[0].link


@pytest.mark.parametrize("tool,gesture,expected", [
    ("ripple", lambda c: (c.t2x(4) - 3, c.t2x(3) - 3), [(0, 3, 0, 1), (3, 4, 1, 1)]),
    ("rolling", lambda c: (c.t2x(4) + 3, c.t2x(4.5) + 3), [(0, 4.5, 0, 1), (4.5, 3.5, 1.5, 1)]),
    ("rolling", lambda c: (c.t2x(4) + 3, c.t2x(2) + 3), [(0, 3, 0, 1), (3, 5, 0, 1)]),  # clamped by handle
    ("rate", lambda c: (c.t2x(8) - 3, c.t2x(12) - 3), [(0, 4, 0, 1), (4, 8, 1, 0.5)]),
    ("slip", lambda c: (c.t2x(6), c.t2x(5.5)), [(0, 4, 0, 1), (4, 4, 1.5, 1)]),
    ("slide", lambda c: (c.t2x(6), c.t2x(5)), [(0, 3, 0, 1), (3, 4, 1, 1)]),
    ("track_select", lambda c: (c.t2x(2), c.t2x(3)), [(1, 4, 0, 1), (5, 4, 1, 1)]),
])
def test_editing_tools(win, tool, gesture, expected):
    c = two_clips(win)
    win.set_tool(tool)
    drag(c, *gesture(c), v1y(c, win))
    assert state(win.project.video_tracks[0]) == expected
    if tool != "rate":  # linked audio follows the video edit
        assert state(win.project.audio_tracks[0]) == expected


def test_razor_shift_cuts_all_tracks(win):
    c = two_clips(win)
    win.set_tool("razor")
    QTest.mouseClick(c, L, Qt.KeyboardModifier.ShiftModifier, QPoint(int(c.t2x(2)), int(v1y(c, win))))
    assert len(win.project.video_tracks[0].clips) == len(win.project.audio_tracks[0].clips) == 3


def test_alt_trim_edits_one_side(win):
    c = two_clips(win)
    win.set_tool("select")
    drag(c, c.t2x(8) - 3, c.t2x(7) - 3, v1y(c, win), Qt.KeyboardModifier.AltModifier)
    assert win.project.video_tracks[0].clips[1].duration == 3.0
    assert win.project.audio_tracks[0].clips[1].duration == 4.0


def test_move_overwrites_and_undo_redo(win):
    c = two_clips(win)
    win.set_tool("select")
    drag(c, c.t2x(6), c.t2x(5), v1y(c, win))
    assert state(win.project.video_tracks[0]) == [(0, 3, 0, 1), (3, 4, 1, 1)]
    win.undo()
    assert state(win.project.video_tracks[0])[1][0] == 4
    win.redo()
    assert state(win.project.video_tracks[0])[1][0] == 3


def test_zoom_hand_type(win):
    c = two_clips(win)
    win.set_tool("zoom")
    pps = c.pps
    QTest.mouseClick(c, L, NM, QPoint(400, int(v1y(c, win))))
    assert c.pps / pps == pytest.approx(1.6)
    win.set_tool("hand")
    c.scroll_t = 2
    drag(c, 500, 400, v1y(c, win))
    assert c.scroll_t > 2
    win.set_tool("type")
    c.scroll_t = 0  # the hand tool scrolled the view; bring 1 s back on screen
    y2, h2 = c.row_of(win.project.video_tracks[1])
    QTest.mouseClick(c, L, NM, QPoint(int(c.t2x(1)), int(y2 + h2 / 2)))
    assert [(x.kind, x.start) for x in win.project.video_tracks[1].clips] == [("title", 1.0)]


def test_speed_from_inspector(win):
    c = two_clips(win)
    c.selection = {win.project.video_tracks[0].clips[1].id}
    win._refresh_inspector()
    win.inspector.widgets["speed"].setValue(200)
    assert state(win.project.video_tracks[0]) == [(0, 4, 0, 1), (4, 2, 1, 2.0)]


def test_monitor_drag_moves_clip(win):
    c = two_clips(win)
    win.set_playhead(1.0)
    c.selection = {win.project.video_tracks[0].clips[0].id}
    win._refresh_inspector()
    win.pump(0.2)
    m = win.monitor
    r = m.frame_rect()
    k = r.width() / win.project.width
    p0 = QPoint(int(r.center().x()), int(r.center().y()))
    QTest.mousePress(m, L, NM, p0)
    QTest.mouseMove(m, p0 + QPoint(50, 20))
    QTest.mouseMove(m, p0 + QPoint(100, 40))
    QTest.mouseRelease(m, L, NM, p0 + QPoint(100, 40))
    clip = win.project.video_tracks[0].clips[0]
    assert clip.pos_x == pytest.approx(100 / k, abs=3) and clip.pos_y == pytest.approx(40 / k, abs=3)
    win.undo()
    assert win.project.video_tracks[0].clips[0].pos_x == 0


def test_transitions_drop_and_ctrl_d(win):
    c = two_clips(win)
    v1, a1 = win.project.video_tracks[0], win.project.audio_tracks[0]
    y, h = c.row_of(v1)
    clip, side, _ = c.transition_target(c.t2x(3.5), y + h / 2)  # right half of A -> B's "in"
    assert clip is v1.clips[1] and side == "in"
    c.drop_transition.emit("wipe_right", clip.id, side, False)
    assert v1.clips[1].tin == "wipe_right" and a1.clips[1].tin == "crossfade"
    win.set_playhead(0.1)
    c.selection.clear()
    win.apply_transition_at_playhead()
    assert win.project.video_tracks[0].clips[0].tin == "dissolve"
    win.set_playhead(4.0)
    win.apply_transition_at_playhead("graphic_sweep")
    assert win.project.video_tracks[0].clips[1].tin == "graphic_sweep"


def test_silences_cut_keeps_sync(win):
    from super8.audiofx import envelope_db
    from super8.compositor import mix_audio
    from super8.ui.audio_tools import SilenceDialog
    p = win.project
    for tr in p.tracks():
        tr.clips.clear()
    win.place_media(win.ids["talk.mp4"], 0, None)
    p = win.project
    db = envelope_db(lambda t, n: mix_audio(p, win.audio_cache, t, n), 0, p.duration(), p.sample_rate)
    dlg = SilenceDialog(win, db, 0, p.duration(), p.fps, "test")
    assert len(dlg.ranges) >= 6
    total = sum(b - a for a, b in dlg.ranges)
    before = p.duration()
    win.timeline.canvas.markers = list(dlg.ranges)
    win.cut_marked_silences()
    p = win.project
    assert before - p.duration() == pytest.approx(total, abs=0.05)
    v = [(round(x.start, 3), round(x.end, 3)) for x in p.video_tracks[0].clips]
    a = [(round(x.start, 3), round(x.end, 3)) for x in p.audio_tracks[0].clips]
    assert v == a
    assert all(abs(n[0] - m[1]) < 1e-6 for m, n in zip(v, v[1:]))  # no gaps


def test_noise_reduction_background(win):
    win.place_media(win.ids["talk.mp4"], 0, None)
    clip = win.project.audio_tracks[0].clips[0]
    win.set_denoise([clip], 0.7)
    for _ in range(150):
        win.pump(0.1)
        if win.audio_cache.ready(win.ids["talk.mp4"], 0.7):
            break
    assert win.audio_cache.ready(win.ids["talk.mp4"], 0.7)


def test_graphics_workflow(win):
    two_clips(win)
    c = win.timeline.canvas
    assert win.graphics.list.count() == len(graphics.all_presets())
    c.drop_graphic.emit("lower_bar", 1.0, win.project.video_tracks[1])
    g = win.project.video_tracks[1].clips[0]
    assert g.kind == "title" and g.start == 1.0 and g.bg_style == "bar"
    assert g.pos_x == pytest.approx(-800 * 1280 / 1920)  # scaled to the 720p sequence
    ins = win.inspector
    c.selection = {g.id}
    win._refresh_inspector()
    assert not ins.form.isRowVisible(ins.widgets["bg_color"])
    ins.widgets["bg_style"].setCurrentIndex(ins.widgets["bg_style"].findData("box"))
    assert g.bg_style == "box" and ins.form.isRowVisible(ins.widgets["bg_color"])
    ins.widgets["subtext"].setPlainText("Lead Editor")
    ins.widgets["anim_in"].setCurrentIndex(ins.widgets["anim_in"].findData("typewriter"))
    ins.widgets["italic"].setChecked(True)
    assert (g.subtext, g.anim_in, g.italic) == ("Lead Editor", "typewriter", True)
    win.undo()
    assert not win.project.find_clip(g.id)[1].italic
    win.redo()
    win.set_playhead(3.0)
    win.pump(0.3)
    ov = win.monitor.overlay
    assert ov is not None and ov.left() >= 0 and ov.center().y() > win.project.height / 2
    # templates: save, persist, reuse
    QInputDialog.getText = staticmethod(lambda *a, **k: ("My Lower Third", True))
    win.save_template()
    assert "user:My Lower Third" in graphics.USER_PRESETS
    graphics.USER_PRESETS.clear()
    win._load_templates()
    assert graphics.USER_PRESETS["user:My Lower Third"]["subtext"] == "Lead Editor"
    win.add_title(12.0, preset="user:My Lower Third")
    new = [x for t in win.project.video_tracks for x in t.clips if x.start == 12.0]
    assert new and new[0].pos_x == pytest.approx(g.pos_x) and new[0].anim_in == "typewriter"
    win.delete_template("user:My Lower Third")
    assert "user:My Lower Third" not in graphics.USER_PRESETS


def test_save_reload_and_export(win, tmp_path):
    two_clips(win)
    win.add_title(1.0, preset="kinetic")
    win.project.video_tracks[0].clips[1].tin = "graphic_bars"
    win.project_path = str(tmp_path / "t.s8proj")
    assert win.save()
    d1 = win.project.to_dict()
    win.open_path(win.project_path)
    assert win.project.to_dict() == d1
    q = win.project.clone()
    q.width, q.height = 640, 360
    out = str(tmp_path / "out.mp4")
    assert export_project(q, win.audio_cache, out, preset="ultrafast")
    with av.open(out) as f:
        assert f.duration / 1e6 == pytest.approx(q.duration(), abs=0.1)
        assert {s.type for s in f.streams} == {"video", "audio"}
