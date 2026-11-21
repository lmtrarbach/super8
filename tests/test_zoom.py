"""Timeline zoom: zoom tool ranges, navigator, slider, track heights, filmstrip and waveform detail."""
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from super8.ui.timeline import HEADER, MAX_PPS, MIN_TRACK_H

L = Qt.MouseButton.LeftButton
NM = Qt.KeyboardModifier.NoModifier


@pytest.fixture
def seq(win):
    """talk.mp4 (30 s, linked A/V) on V1/A1, view fitted."""
    win.place_media(win.ids["talk.mp4"], 0, None)
    c = win.timeline.canvas
    c.zoom_fit()
    win.pump(0.05)
    return win, c


def visible_range(c):
    return c.scroll_t, c.scroll_t + c.visible_seconds()


def drag(widget, x0, x1, y):
    QTest.mousePress(widget, L, NM, QPoint(int(x0), int(y)))
    for i in range(1, 7):
        QTest.mouseMove(widget, QPoint(int(x0 + (x1 - x0) * i / 6), int(y)))
    QTest.mouseRelease(widget, L, NM, QPoint(int(x1), int(y)))


def test_zoom_tool_drag_fills_view_with_range(seq):
    w, c = seq
    w.set_tool("zoom")
    drag(c, c.t2x(10), c.t2x(14), 60)
    a, b = visible_range(c)
    assert a == pytest.approx(10, abs=0.05) and b == pytest.approx(14, abs=0.05)
    w.set_tool("zoom")
    QTest.mouseClick(c, L, Qt.KeyboardModifier.AltModifier, QPoint(int(c.t2x(12)), 60))  # Alt-click zooms out
    assert c.visible_seconds() == pytest.approx(4 * 1.6, rel=0.01)


def test_keyboard_zoom_keeps_playhead_on_screen_position(seq):
    w, c = seq
    w.set_playhead(20.0)
    x_before = c.t2x(20.0)
    c.set_zoom(c.pps * 8)
    assert c.t2x(20.0) == pytest.approx(x_before, abs=0.5)


def test_zoom_to_selection_and_limits(seq):
    w, c = seq
    clip = w.project.video_tracks[0].clips[0]
    c.selection = {clip.id}
    assert c.zoom_selection()
    a, b = visible_range(c)
    assert a <= clip.start + 0.01 and b >= clip.end - 0.01 and b - a < clip.duration * 1.1
    c.zoom_to_range(5.0, 5.0001)  # narrower than the max zoom allows: clamp and centre
    assert c.pps == MAX_PPS
    a, b = visible_range(c)
    assert a < 5.0 < b


def test_navigator_pan_resize_jump_fit(seq):
    w, c = seq
    nav = w.timeline.navigator
    c.zoom_to_range(0, 6, margin=0)
    y = nav.height() // 2
    # drag the right edge to the 12 s mark: left edge stays, view now covers ~0..12 s
    r = nav.view_rect()
    drag(nav, r.right(), nav.t2x(12), y)
    a, b = visible_range(c)
    assert a == pytest.approx(0, abs=0.05) and b == pytest.approx(12, abs=0.3)
    # drag the box itself: pans without zooming
    pps = c.pps
    r = nav.view_rect()
    drag(nav, r.center().x(), r.center().x() + (nav.t2x(5) - nav.t2x(0)), y)
    assert c.pps == pps and c.scroll_t == pytest.approx(5, abs=0.3)
    # click outside the box: the view jumps there
    QTest.mouseClick(nav, L, NM, QPoint(int(nav.t2x(27)), y))
    a, b = visible_range(c)
    assert a < 27 < b
    QTest.mouseDClick(nav, L, NM, QPoint(int(nav.t2x(27)), y))
    a, b = visible_range(c)
    assert a == 0 and b >= w.project.duration()


def test_slider_maps_log_scale(seq):
    w, c = seq
    tl = w.timeline
    for pps in (0.1, 5.0, 300.0, MAX_PPS):
        assert tl._slider_to_pps(tl._pps_to_slider(pps)) == pytest.approx(pps, rel=0.02)
    before = c.pps
    tl.slider.setValue(tl.slider.value() + 200)
    assert c.pps > before * 3
    assert tl.span_label.text() == __import__("super8.ui.timeline", fromlist=["fmt_tc"]).fmt_tc(
        c.visible_seconds(), w.project.fps)


def test_track_height_resize(seq):
    w, c = seq
    a1 = w.project.audio_tracks[0]
    y, h = c.row_of(a1)
    assert c.header_edge_at(HEADER / 2, y + h) is a1
    QTest.mousePress(c, L, NM, QPoint(int(HEADER / 2), int(y + h)))
    QTest.mouseMove(c, QPoint(int(HEADER / 2), int(y + h + 70)))
    QTest.mouseRelease(c, L, NM, QPoint(int(HEADER / 2), int(y + h + 70)))
    assert c.track_h(a1) == h + 70
    clip = a1.clips[0]
    assert c.clip_rect(a1, clip).height() == h + 70 - 4  # clips follow the row
    assert c.track_at(y + h + 60) is a1
    c.scale_tracks(0.01)
    assert all(c.track_h(t) == MIN_TRACK_H for t in w.project.tracks())
    c.reset_track_heights()
    assert c.track_h(a1) == h
    assert c.content_height() == c.rows()[-1][1] + c.rows()[-1][2] + c.scroll_y


def test_filmstrip_and_deep_zoom_painting(seq):
    w, c = seq
    mid = w.ids["talk.mp4"]
    for _ in range(100):
        if mid in w.filmstrips:
            break
        w.pump(0.1)
    times, images = w.filmstrips[mid]
    assert len(images) >= 100 and times[0] == 0 and times[-1] > 29
    clip = w.project.video_tracks[0].clips[0]
    # tiles at different positions pick different frames
    pick = lambda t: int(__import__("numpy").searchsorted(times, t, side="right")) - 1  # noqa: E731
    assert pick(clip.in_point + 2) != pick(clip.in_point + 20)
    c.set_track_height(w.project.audio_tracks[0], 200)
    for pps in (2.0, 60.0, 600.0, MAX_PPS):  # overview peaks -> sample-accurate waveform
        c.zoom_to_range(14, 14 + c.visible_seconds() if pps == 2.0 else 14 + 900 / pps, margin=0)
        img = c.grab().toImage()
        assert not img.isNull()


def test_tall_tracks_get_a_vertical_scrollbar(seq):
    w, c = seq
    tl = w.timeline
    c.reset_track_heights()
    w.pump(0.05)
    overflow = c.content_height() - c.height()
    assert tl.vbar.isVisible() == (overflow > 0) and tl.vbar.maximum() == max(0, overflow)
    c.scale_tracks(4)
    w.pump(0.05)
    assert tl.vbar.isVisible() and tl.vbar.maximum() == c.content_height() - c.height()
    tl.vbar.setValue(tl.vbar.maximum())
    assert c.scroll_y == tl.vbar.maximum()
    last, y, h = c.rows()[-1]
    assert y + h <= c.height() + 1  # the last track is reachable
