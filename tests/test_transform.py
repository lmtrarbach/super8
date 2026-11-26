"""Transforms (fit, scale, rotate, flip, crop), monitor handles, aspect ratio and export size."""
import gc
import math

import av
import numpy as np
import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from super8.compositor import Compositor
from super8.export import export_project
from super8.media import AudioCache, probe
from super8.model import Clip, Project
from super8.transform import Box, aspect_of, clip_box, ratio_text, size_for

RED, BLUE, GREEN = (220, 40, 40), (40, 80, 230), (40, 200, 60)
L = Qt.MouseButton.LeftButton
NM = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="module")
def asym(tmp_path_factory, qapp):
    """320x180 picture: left half red, right half blue, green strip across the top."""
    img = np.zeros((180, 320, 3), np.uint8)
    img[:, :160], img[:, 160:], img[:30] = RED, BLUE, GREEN
    path = tmp_path_factory.mktemp("img") / "asym.png"
    with av.open(str(path), "w") as o:
        s = o.add_stream("png", rate=1)
        s.width, s.height, s.pix_fmt = 320, 180, "rgb24"
        for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
            o.mux(p)
        for p in s.encode(None):
            o.mux(p)
    return probe(str(path))


def render(media, W=320, H=180, seq=(1280, 720), **clip):
    p = Project.new()
    p.width, p.height = seq
    p.media[media.id] = media
    p.video_tracks[0].clips = [Clip("c", "image", media.id, 0, 5, **clip)]
    comp = Compositor(AudioCache())
    try:
        return comp.render(p, 1.0, W, H)
    finally:
        comp.close()


def near(px, color, tol=12):
    return all(abs(int(a) - b) <= tol for a, b in zip(px, color))


# ------------------------------------------------------------------ geometry
def test_aspect_presets_and_sizes():
    assert size_for(16, 9, 1080) == (1920, 1080)
    assert size_for(9, 16, 1080) == (1080, 1920)
    assert size_for(4, 5, 1080) == (1080, 1350)
    assert aspect_of(1080, 1920).startswith("9:16") and aspect_of(1000, 700) is None
    assert ratio_text(1920, 1080) == "16:9" and ratio_text(1000, 700) == "10:7"


def test_box_crop_flip_and_rotation_geometry():
    b = Box(100, 100, 200, 100, crop=(0.25, 0, 0, 0))
    assert b.visible() == (50.0, 50, 150, 100)  # the left quarter is gone, the rest stays put
    b.flip_h = True
    assert b.visible()[0] == 0.0  # flipped: the cut side is now on the right
    r = Box(0, 0, 200, 100, rotation=90)
    tl = r.corners()[0]  # top-left (-100, -50) turned 90 degrees clockwise lands at (50, -100)
    assert tl == pytest.approx((50, -100))
    assert r.to_local(50, -100) == pytest.approx((-100, -50))


# ------------------------------------------------------------------ rendering
def test_fit_fill_stretch_none_in_a_vertical_frame(asym):
    W, H = 90, 160  # 9:16 canvas, 16:9 picture
    fit = render(asym, W, H, seq=(1080, 1920))
    assert fit[5, 45].max() < 10 and fit[155, 45].max() < 10  # letterbox bars top and bottom
    fill = render(asym, W, H, seq=(1080, 1920), fit="fill")
    assert fill[5, 45].max() > 50 and fill[155, 45].max() > 50  # covers the frame
    assert near(fill[100, 2], RED) and near(fill[100, 87], BLUE)
    stretch = render(asym, W, H, seq=(1080, 1920), fit="stretch")
    assert near(stretch[150, 10], RED) and near(stretch[150, 80], BLUE) and near(stretch[3, 45], GREEN)
    none = render(asym, 320, 180, seq=(1280, 720), fit="none")  # 320 source px = a quarter of 1280
    cols = np.flatnonzero(none[100].max(axis=1) > 50)
    assert cols[-1] - cols[0] + 1 == pytest.approx(80, abs=2)


def test_flip_rotate_crop_pixels(asym):
    flip = render(asym, flip_h=True)
    assert near(flip[100, 20], BLUE) and near(flip[100, 300], RED)
    flipv = render(asym, flip_v=True)
    assert near(flipv[175, 160], GREEN) and not near(flipv[5, 100], GREEN)
    rot = render(asym, rotation=90)  # 180 wide x 320 tall at x 70..250; the green top is now x 220..250
    assert near(rot[90, 235], GREEN) and near(rot[40, 160], RED) and near(rot[140, 160], BLUE)
    crop = render(asym, crop_l=0.5)  # red half gone, blue stays where it was
    assert crop[100, 40].max() < 10 and near(crop[100, 240], BLUE)
    half = render(asym, opacity=0.5)
    assert half[100, 40] == pytest.approx(np.array(RED) / 2, abs=3)


def test_render_returns_its_own_memory(asym):
    """Regression: the frame's NumPy view aliases a QImage freed after render()."""
    a = render(asym, flip_h=True)
    snapshot = a.copy()
    render(asym, rotation=33)
    gc.collect()
    assert np.array_equal(a, snapshot)


def test_old_projects_load_with_default_transforms():
    d = Project.new().to_dict()
    d["video_tracks"][0]["clips"] = [{"id": "x", "kind": "video", "media_id": "m", "start": 0, "duration": 1}]
    c = Project.from_dict(d).video_tracks[0].clips[0]
    assert (c.fit, c.rotation, c.scale_x, c.flip_h, c.crop_l) == ("fit", 0.0, 1.0, False, 0.0)


# ------------------------------------------------------------------ monitor handles
@pytest.fixture
def sel(win):
    win.place_media(win.ids["talk.mp4"], 0, None)
    c = win.project.video_tracks[0].clips[0]
    c.scale = 0.5
    win.timeline.canvas.selection = {c.id}
    win.set_playhead(1.0)
    win.changed()
    win.pump(0.2)
    return win, c


def drag(widget, a: QPointF, b: QPointF, mods=NM):
    QTest.mousePress(widget, L, mods, a.toPoint())
    for i in range(1, 7):
        QTest.mouseMove(widget, QPoint(int(a.x() + (b.x() - a.x()) * i / 6), int(a.y() + (b.y() - a.y()) * i / 6)))
    QTest.mouseRelease(widget, L, mods, b.toPoint())


def test_corner_edge_rotate_handles(sel):
    win, c = sel
    m = win.monitor
    hs = m.handles()
    assert set(hs) >= {"corner0", "corner2", "edge_l", "edge_r", "rotate"}
    centre = m.to_screen(QPointF(win.project.width / 2, win.project.height / 2))
    corner = hs["corner2"]
    drag(m, corner, QPointF(centre.x() + (corner.x() - centre.x()) * 1.5, centre.y() + (corner.y() - centre.y()) * 1.5))
    c = win.project.find_clip(c.id)[1]
    assert c.scale == pytest.approx(0.75, rel=0.05) and c.scale_x == 1.0
    depth = len(win.history.undo_stack)
    hs = m.handles()
    edge = hs["edge_r"]
    drag(m, edge, QPointF(centre.x() + (edge.x() - centre.x()) * 0.5, edge.y()))
    c = win.project.find_clip(c.id)[1]
    assert c.scale_x == pytest.approx(0.5, rel=0.08) and c.scale == pytest.approx(0.75, rel=0.05)
    assert len(win.history.undo_stack) == depth + 1  # one undo step per drag
    hs = m.handles()
    r = hs["rotate"]
    radius = math.hypot(r.x() - centre.x(), r.y() - centre.y())
    drag(m, r, QPointF(centre.x() + radius, centre.y()))  # handle from straight up to the right: 90 degrees
    assert win.project.find_clip(c.id)[1].rotation == pytest.approx(90, abs=0.5)
    # Shift snaps to 15-degree steps (QTest moves can't carry modifiers, so drive the handlers)
    seq_centre = m.to_seq(centre)
    win.on_monitor_press(m.to_seq(m.handles()["rotate"]), NM, "rotate")
    win.on_monitor_drag(QPointF(seq_centre.x() + 100, seq_centre.y() + 37), Qt.KeyboardModifier.ShiftModifier)
    win.on_monitor_release()
    assert win.project.find_clip(c.id)[1].rotation % 15 == pytest.approx(0)
    win.undo()
    win.undo()
    assert win.project.find_clip(c.id)[1].rotation == 0


def test_move_snaps_to_centre_unless_ctrl(sel):
    win, c = sel
    m = win.monitor
    k = m.scale()
    start = m.to_screen(QPointF(win.project.width / 2, win.project.height / 2))
    drag(m, start, QPointF(start.x() + 40, start.y() + 3))  # 3 screen px off centre vertically: snaps
    c = win.project.find_clip(c.id)[1]
    assert c.pos_y == 0 and c.pos_x == pytest.approx(40 / k, abs=2)
    assert m.snap_lines == []  # cleared after the drag
    # Ctrl turns snapping off (QTest moves can't carry modifiers, so drive the handlers)
    p0 = QPointF(win.project.width / 2 + c.pos_x, win.project.height / 2)
    win.on_monitor_press(p0, NM, "inside")
    win.on_monitor_drag(QPointF(p0.x(), p0.y() + 3 / k), Qt.KeyboardModifier.ControlModifier)
    win.on_monitor_release()
    assert win.project.find_clip(c.id)[1].pos_y == pytest.approx(3 / k, abs=1)


def test_alt_wheel_rotates_and_wheel_scales(sel):
    win, c = sel
    win.on_monitor_wheel(2, Qt.KeyboardModifier.AltModifier)
    assert win.project.find_clip(c.id)[1].rotation == pytest.approx(10)
    win.on_monitor_wheel(1, NM)
    assert win.project.find_clip(c.id)[1].scale == pytest.approx(0.5 * 1.05)


# ------------------------------------------------------------------ actions, inspector, sequence, export
def test_transform_actions_and_reset(sel):
    win, c = sel
    depth = len(win.history.undo_stack)
    win.transform_selected("fill")
    win.transform_selected("rotate_cw")
    win.transform_selected("flip_h")
    c = win.project.find_clip(c.id)[1]
    assert (c.fit, c.scale, c.rotation, c.flip_h) == ("fill", 1.0, 90, True)
    assert len(win.history.undo_stack) == depth + 3
    ins = win.inspector
    win._refresh_inspector()
    ins.widgets["crop_l"].setValue(70)
    ins.widgets["crop_r"].setValue(60)  # 70 % + 60 % is too much: clamped to 95 % together
    c = win.project.find_clip(c.id)[1]
    assert c.crop_l + c.crop_r == pytest.approx(0.95)
    ins.reset_btn.click()
    c = win.project.find_clip(c.id)[1]
    assert (c.fit, c.scale, c.rotation, c.flip_h, c.crop_l) == ("fit", 1.0, 0.0, False, 0.0)
    win.undo()
    assert win.project.find_clip(c.id)[1].flip_h is True


def test_aspect_menu_and_sequence_dialog(win):
    from super8.ui.main_window import SequenceDialog
    win.place_media(win.ids["talk.mp4"], 0, None)  # 640x360 sequence
    win._fill_aspect_menu()
    vertical = next(a for a in win.aspect_menu.actions() if a.text().startswith("9:16"))
    vertical.trigger()
    assert (win.project.width, win.project.height) == (360, 640)
    assert win.aspect_btn.text().startswith("9:16")
    win.undo()
    assert (win.project.width, win.project.height) == (640, 360)
    d = SequenceDialog(win, win.project)
    d.aspect.setCurrentIndex(next(i for i in range(d.aspect.count()) if d.aspect.itemText(i).startswith("4:5")))
    d.res.setCurrentIndex(d.res.findData(1080))
    assert (d.w.value(), d.h.value()) == (1080, 1350)
    d.w.setValue(1000)
    assert d.aspect.currentText() == "Custom" and "1000 × 1350" in d.summary.text()


def test_export_at_another_resolution(win, tmp_path):
    win.place_media(win.ids["clip_a.mp4"], 0, None)  # 1280x720 sequence
    q = win.project.clone()
    out = str(tmp_path / "small.mp4")
    assert export_project(q, win.audio_cache, out, preset="ultrafast", size=(854, 480))
    with av.open(out) as f:
        s = f.streams.video[0]
        assert (s.codec_context.width, s.codec_context.height) == (854, 480)


def test_ai_tools_transform_and_sequence(win):
    from super8.ai.tools import ToolError, Toolkit
    win.place_media(win.ids["talk.mp4"], 0, None)
    kit = Toolkit(win)
    v = win.project.video_tracks[0].clips[0]
    kit.execute("set_clip_properties", {"clip_id": v.id, "properties": {"fit": "fill", "rotation": 15, "flip_h": True}})
    assert (v.fit, v.rotation, v.flip_h) == ("fill", 15, True)
    t = kit.execute("add_title", {"template": "basic", "start": 0})
    tid = __import__("json").loads(t)["added"]["id"]
    with pytest.raises(ToolError, match="cannot be set"):
        kit.execute("set_clip_properties", {"clip_id": tid, "properties": {"crop_l": 0.2}})
    with pytest.raises(ToolError, match="not one of"):
        kit.execute("set_sequence", {"resolution": 999})
    kit.execute("set_sequence", {"aspect": "9:16", "resolution": 1080})
    assert (win.project.width, win.project.height) == (1080, 1920)
    assert __import__("json").loads(kit.execute("get_project", {}))["sequence"]["aspect"] == "9:16"


def test_clip_box_matches_renderer(asym):
    """The monitor outline and the rendered pixels agree on where a rotated, cropped picture is."""
    c = Clip("c", "image", asym.id, 0, 5, scale=0.5, rotation=0, crop_l=0.25, pos_x=200)
    b = clip_box(c, 320, 180, 320, 180, 1280, 720)
    x0, y0, x1, y1 = b.bbox()
    img = render(asym, scale=0.5, crop_l=0.25, pos_x=200)
    cols = np.flatnonzero(img.max(axis=(0, 2)) > 50)
    rows = np.flatnonzero(img.max(axis=(1, 2)) > 50)
    assert (cols[0], cols[-1] + 1) == pytest.approx((x0, x1), abs=1.5)
    assert (rows[0], rows[-1] + 1) == pytest.approx((y0, y1), abs=1.5)
