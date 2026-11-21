"""Multi-track timeline with Premiere-style editing tools, snapping and drag & drop."""
from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QScrollBar, QVBoxLayout, QWidget

from ..model import Clip, Track, carve, split_clip, split_with_links, transition_windows
from ..graphics import preset_duration
from ..transitions import short_label
from . import icons

MEDIA_MIME = "application/x-super8-media"
TRANSITION_MIME = "application/x-super8-transition"
GRAPHIC_MIME = "application/x-super8-graphic"

HEADER = 96
RULER = 28
VIDEO_H = 58
AUDIO_H = 50
GAP = 8
EDGE = 7  # px grab zone for trimming
SNAP_PX = 9

# id: (label, shortcut, icon, hint shown in the status bar)
TOOLS = {
    "select": ("Selection Tool", "V", "select",
               "Click to select · drag to move · drag edges to trim · Shift+click adds · Alt ignores links"),
    "track_select": ("Track Select Forward Tool", "A", "track_select",
                     "Click to select every clip from here onward, then drag them · Shift: this track only"),
    "ripple": ("Ripple Edit Tool", "B", "ripple",
               "Drag a clip edge to trim it; later clips on the track move to close or open the gap"),
    "rolling": ("Rolling Edit Tool", "N", "rolling",
                "Drag a cut between two clips: one gets longer, the other shorter, total length unchanged"),
    "rate": ("Rate Stretch Tool", "R", "rate",
             "Drag a clip edge to change its speed instead of trimming it"),
    "razor": ("Razor Tool", "C", "razor", "Click to cut a clip · Shift+click cuts every track"),
    "slip": ("Slip Tool", "Y", "slip",
             "Drag a clip to change which part of the source plays, without moving it"),
    "slide": ("Slide Tool", "U", "slide",
              "Drag a clip left/right; the neighbouring clips are trimmed to make room"),
    "hand": ("Hand Tool", "H", "hand", "Drag to pan the timeline"),
    "zoom": ("Zoom Tool", "Z", "zoom", "Click to zoom in · Alt+click to zoom out"),
    "type": ("Type Tool", "T", "type",
             "Click a video track or the Program Monitor to add a title there"),
}

CLIP_COLORS = {
    "video": QColor("#3d6fb6"),
    "audio": QColor("#3c9a6a"),
    "image": QColor("#8a5cc2"),
    "title": QColor("#c2577f"),
}


def fmt_tc(t: float, fps: float) -> str:
    fps_i = max(1, int(round(fps)))
    f = int(round(max(0.0, t) * fps))
    s, fr = divmod(f, fps_i)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{fr:02d}"


class TimelineCanvas(QWidget):
    seek = pyqtSignal(float)
    selection_changed = pyqtSignal()
    edited = pyqtSignal(dict)  # project state *before* the edit, for undo
    drop_media = pyqtSignal(str, float, object)  # media id, time, track
    drop_graphic = pyqtSignal(str, float, object)  # template id, time, track
    drop_transition = pyqtSignal(str, str, str, bool)  # kind, clip id, "in"|"out", this track only
    context_menu = pyqtSignal(object, object, float)  # clip|None, global QPoint, time
    view_changed = pyqtSignal()

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pps = 60.0  # pixels per second
        self.scroll_t = 0.0
        self.scroll_y = 0
        self.playhead = 0.0
        self.selection: set[str] = set()
        self.tool = "select"
        self.snap = True
        self.linked_selection = True
        self.overlay = None  # (x, y, text) feedback while dragging
        self.markers: list[tuple[float, float]] = []  # e.g. detected silences, drawn in red
        self.tghost = None  # (track, t0, t1) while dragging a transition
        self.gghost = None  # (template id, track, t, duration) while dragging a graphic
        self.drag = None
        self.ghost = None  # (media_id, t, track) while dragging from the bin
        self.snap_line = None
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setMinimumHeight(RULER + 2 * (VIDEO_H + AUDIO_H))

    @property
    def project(self):
        return self.main.project

    # ------------------------------------------------------------ geometry
    def t2x(self, t: float) -> float:
        return HEADER + (t - self.scroll_t) * self.pps

    def x2t(self, x: float) -> float:
        return (x - HEADER) / self.pps + self.scroll_t

    def rows(self) -> list[tuple[Track, int, int]]:
        out, y = [], RULER - self.scroll_y
        for tr in reversed(self.project.video_tracks):
            out.append((tr, y, VIDEO_H))
            y += VIDEO_H
        y += GAP
        for tr in self.project.audio_tracks:
            out.append((tr, y, AUDIO_H))
            y += AUDIO_H
        return out

    def content_height(self) -> int:
        p = self.project
        return RULER + len(p.video_tracks) * VIDEO_H + GAP + len(p.audio_tracks) * AUDIO_H

    def track_at(self, y: float) -> Track | None:
        for tr, ty, h in self.rows():
            if ty <= y < ty + h:
                return tr
        return None

    def row_of(self, track: Track):
        for tr, y, h in self.rows():
            if tr is track:
                return y, h
        return 0, 0

    def clip_rect(self, track: Track, c: Clip) -> QRectF:
        y, h = self.row_of(track)
        return QRectF(self.t2x(c.start), y + 2, c.duration * self.pps, h - 4)

    def hit(self, x: float, y: float):
        tr = self.track_at(y)
        if tr is None or x < HEADER:
            return None, None, None
        for c in tr.clips:
            r = self.clip_rect(tr, c)
            if r.left() - 1 <= x <= r.right() + 1:
                edge = None
                if r.width() > 3 * EDGE:
                    if x - r.left() <= EDGE:
                        edge = "left"
                    elif r.right() - x <= EDGE:
                        edge = "right"
                return tr, c, edge
        return tr, None, None

    def toggle_rect(self, y: int, h: int) -> QRectF:
        return QRectF(HEADER - 30, y + (h - 20) / 2, 22, 20)

    # ------------------------------------------------------------ zoom/scroll
    def set_zoom(self, pps: float, anchor_x: float | None = None):
        anchor_x = self.width() / 2 if anchor_x is None else anchor_x
        anchor_t = self.x2t(anchor_x)
        self.pps = min(2000.0, max(2.0, pps))
        self.scroll_t = max(0.0, anchor_t - (anchor_x - HEADER) / self.pps)
        self.view_changed.emit()
        self.update()

    def zoom_fit(self):
        d = max(5.0, self.project.duration())
        self.pps = min(2000.0, max(2.0, (self.width() - HEADER - 30) / d))
        self.scroll_t = 0.0
        self.view_changed.emit()
        self.update()

    def ensure_visible(self, t: float):
        x = self.t2x(t)
        if x < HEADER or x > self.width() - 20:
            self.scroll_t = max(0.0, t - (self.width() - HEADER) * 0.1 / self.pps)
            self.view_changed.emit()

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        mods = e.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self.pps * (1.2 if dy > 0 else 1 / 1.2), e.position().x())
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            max_y = max(0, self.content_height() - self.height())
            self.scroll_y = int(min(max_y, max(0, self.scroll_y - dy / 2)))
            self.update()
        else:
            self.scroll_t = max(0.0, self.scroll_t - dy / 120 * 80 / self.pps)
            self.view_changed.emit()
            self.update()

    # ------------------------------------------------------------ snapping
    def snap_points(self, exclude: set[str]) -> list[float]:
        pts = [0.0, self.playhead]
        for _, c in self.project.all_clips():
            if c.id not in exclude:
                pts += [c.start, c.end]
        return pts

    def snap_delta(self, candidates: list[float], exclude: set[str]) -> float | None:
        """Smallest shift that brings one of `candidates` onto a snap point (or None)."""
        if not self.snap:
            return None
        best, best_d = None, SNAP_PX / self.pps
        for p in self.snap_points(exclude):
            for c in candidates:
                d = p - c
                if abs(d) < best_d:
                    best, best_d = d, abs(d)
                    self.snap_line = p
        return best

    # ------------------------------------------------------------ tool helpers
    def use_links(self, mods) -> bool:
        """Linked selection is on unless toggled off; Alt inverts it for one gesture."""
        return self.linked_selection != bool(mods & Qt.KeyboardModifier.AltModifier)

    def group_of(self, clip: Clip, mods) -> set[str]:
        return {c.id for c in self.project.linked(clip)} if self.use_links(mods) else {clip.id}

    def nearest_edge(self, clip: Clip, x: float) -> str:
        return "left" if abs(x - self.t2x(clip.start)) <= abs(x - self.t2x(clip.end)) else "right"

    def src_limits(self, c: Clip) -> tuple[float, float]:
        """(min in_point, max source end) in source seconds; infinite for stills and titles."""
        m = self.project.media.get(c.media_id) if c.media_id else None
        if m is None or not m.finite:
            return -math.inf, math.inf
        return 0.0, m.duration

    def neighbours(self, tr: Track, c: Clip, start: float, end: float):
        """Clips touching [start, end) on the left/right, and the free space bounds otherwise."""
        eps = 0.5 / self.project.fps
        prev = nxt = None
        prev_end, next_start = 0.0, math.inf
        for o in tr.clips:
            if o is c:
                continue
            if o.end <= start + eps:
                prev_end = max(prev_end, o.end)
                if abs(o.end - start) < eps:
                    prev = o
            if o.start >= end - eps:
                next_start = min(next_start, o.start)
                if abs(o.start - end) < eps:
                    nxt = o
        return prev, nxt, prev_end, next_start

    def _snapshot(self) -> dict:
        return {c.id: (c.start, c.in_point, c.duration, c.speed, tr) for tr, c in self.project.all_clips()}

    def _begin(self, mode: str, x: float, clip: Clip, tr: Track, **extra):
        self.drag = {"mode": mode, "x": x, "clip": clip.id, "track": tr, "orig": self._snapshot(),
                     "before": self.project.to_dict(), "changed": False, **extra}

    def _fdur(self) -> float:
        return 1 / self.project.fps

    # ------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        mods = e.modifiers()
        self.snap_line = None
        self.overlay = None
        if e.button() == Qt.MouseButton.MiddleButton or (
                e.button() == Qt.MouseButton.LeftButton and self.tool == "hand" and x >= HEADER):
            self.drag = {"mode": "pan", "x": x, "y": y, "scroll": self.scroll_t, "scroll_y": self.scroll_y}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if y < RULER and x >= HEADER:
            self.drag = {"mode": "scrub"}
            self.seek.emit(max(0.0, self.x2t(x)))
            return
        if x < HEADER:
            for tr, ty, h in self.rows():
                if self.toggle_rect(ty, h).contains(QPointF(x, y)):
                    before = self.project.to_dict()
                    tr.muted = not tr.muted
                    self.edited.emit(before)
                    return
            return
        tool = self.tool
        t = max(0.0, self.x2t(x))
        if tool == "zoom":
            out = bool(mods & Qt.KeyboardModifier.AltModifier)
            self.set_zoom(self.pps * (1 / 1.6 if out else 1.6), x)
            return
        tr, clip, edge = self.hit(x, y)
        if tool == "type":
            if tr is not None and tr.kind == "video":
                self.main.add_title(t, tr)
            return
        if tool == "track_select":
            self._track_select(t, tr, clip, mods)
            if self.selection:
                grab = clip if clip is not None and clip.id in self.selection else min(
                    (self.project.find_clip(cid)[1] for cid in self.selection), key=lambda c: c.start)
                gtr, _ = self.project.find_clip(grab.id)
                self._begin("move", x, grab, gtr, group=set(self.selection))
            return
        if clip is None:
            if not (mods & Qt.KeyboardModifier.ShiftModifier):
                self.selection.clear()
                self.selection_changed.emit()
            self.drag = {"mode": "scrub"}
            self.seek.emit(t)
            self.update()
            return
        if tool == "razor":
            self._razor(clip, t, all_tracks=bool(mods & Qt.KeyboardModifier.ShiftModifier))
            return
        # every remaining tool selects what it touches
        if tool == "select" and mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
            ids = self.group_of(clip, mods)
            self.selection = self.selection - ids if clip.id in self.selection else self.selection | ids
        elif clip.id not in self.selection or tool != "select":
            self.selection = self.group_of(clip, mods)
        self.selection_changed.emit()
        self.update()
        if tool == "select":
            if edge:
                self._begin(edge, x, clip, tr, group=self.group_of(clip, mods))
            else:
                group = set()
                for cid in self.selection:
                    _, c = self.project.find_clip(cid)
                    if c:
                        group |= self.group_of(c, mods)
                self._begin("move", x, clip, tr, group=group)
        elif tool in ("ripple", "rate"):
            side = edge or self.nearest_edge(clip, x)
            self._begin(f"{tool}_{side}", x, clip, tr, group=self.group_of(clip, mods))
            if tool == "ripple":
                self._prepare_ripple()
        elif tool == "rolling":
            side = edge or self.nearest_edge(clip, x)
            self._begin("rolling", x, clip, tr, group=self.group_of(clip, mods))
            self._prepare_rolling(clip.start if side == "left" else clip.end)
        elif tool == "slip":
            self._begin("slip", x, clip, tr, group=self.group_of(clip, mods))
        elif tool == "slide":
            self._begin("slide", x, clip, tr, group=self.group_of(clip, mods))
            self._prepare_slide()

    def _track_select(self, t: float, tr: Track | None, clip: Clip | None, mods):
        """Select every clip from the click point onward (Shift: only on the clicked track)."""
        if clip is not None:
            t = clip.start
        eps = 0.5 / self.project.fps
        single = bool(mods & Qt.KeyboardModifier.ShiftModifier) and tr is not None
        tracks = [tr] if single else self.project.tracks()
        sel = {c.id for track in tracks for c in track.clips if c.start >= t - eps}
        if not single and self.linked_selection:
            sel = self.project.expand_links(sel)
        self.selection = sel
        self.selection_changed.emit()
        self.update()

    def _razor(self, clip: Clip, t: float, all_tracks: bool):
        if self.snap and abs(t - self.playhead) * self.pps < SNAP_PX:
            t = self.playhead
        t = self.project.frame(t)
        before = self.project.to_dict()
        targets = [clip]
        if all_tracks:
            targets = [c for tr in self.project.tracks() if not tr.muted for c in tr.clips if c.start < t < c.end]
        done, seen = False, set()
        for c in targets:
            if c.id in seen:
                continue
            seen.update(x.id for x in self.project.linked(c))
            done |= split_with_links(self.project, c, t) if self.linked_selection else bool(
                split_clip(self.project, c, t))
        if done:
            self.edited.emit(before)

    def _prepare_ripple(self):
        """Clips after the edited clip(s) on the same tracks get pushed along."""
        d = self.drag
        followers = set()
        for cid in d["group"]:
            s0, i0, d0, sp, tr = d["orig"][cid]
            edge_t = s0 + d0
            followers |= {c.id for c in tr.clips if c.id not in d["group"] and c.start >= edge_t - 1e-6}
        d["followers"] = followers

    def _prepare_rolling(self, edit_t: float):
        """Find, on each track of the group, the clip ending (A) and starting (B) at the edit point."""
        d = self.drag
        eps = 0.5 / self.project.fps
        tracks = {d["orig"][cid][4].id: d["orig"][cid][4] for cid in d["group"]}
        pairs = []
        for tr in tracks.values():
            a = next((c for c in tr.clips if abs(c.end - edit_t) < eps), None)
            b = next((c for c in tr.clips if abs(c.start - edit_t) < eps), None)
            if a is None and b is None:
                continue
            lo_gap = max((c.end for c in tr.clips if c.end <= edit_t + eps and c is not a and c is not b),
                         default=0.0)
            hi_gap = min((c.start for c in tr.clips if c.start >= edit_t - eps and c is not a and c is not b),
                         default=math.inf)
            pairs.append((a.id if a else None, b.id if b else None, lo_gap, hi_gap))
        d["pairs"] = pairs
        d["edit_t"] = edit_t

    def _prepare_slide(self):
        d = self.drag
        info = []
        for cid in d["group"]:
            tr, c = self.project.find_clip(cid)
            prev, nxt, prev_end, next_start = self.neighbours(tr, c, c.start, c.end)
            info.append((cid, prev.id if prev else None, nxt.id if nxt else None, prev_end, next_start))
        d["slide"] = info

    def mouseMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        d = self.drag
        if d is None:
            self._update_cursor(x, y)
            return
        if d["mode"] == "pan":
            self.scroll_t = max(0.0, d["scroll"] - (x - d["x"]) / self.pps)
            max_y = max(0, self.content_height() - self.height())
            self.scroll_y = int(min(max_y, max(0, d["scroll_y"] - (y - d["y"]))))
            self.view_changed.emit()
            self.update()
            return
        if d["mode"] == "scrub":
            t = max(0.0, self.x2t(x))
            if self.snap:
                for p in self.snap_points(set()):
                    if abs(p - t) * self.pps < SNAP_PX / 2:
                        t = p
                        break
            self.seek.emit(t)
            return
        if abs(x - d["x"]) < 2 and not d["changed"]:
            return
        d["changed"] = True
        self.snap_line = None
        dt = (x - d["x"]) / self.pps
        handler = {
            "move": self._drag_move, "left": self._drag_trim, "right": self._drag_trim,
            "ripple_left": self._drag_ripple, "ripple_right": self._drag_ripple,
            "rate_left": self._drag_rate, "rate_right": self._drag_rate,
            "rolling": self._drag_rolling, "slip": self._drag_slip, "slide": self._drag_slide,
        }[d["mode"]]
        handler(dt, x, y)
        self.main.preview_dirty()
        self.update()

    # each drag handler recomputes from the snapshot taken at mouse press
    def _o(self, cid):
        return self.drag["orig"][cid]

    def _clip(self, cid) -> Clip:
        return self.project.find_clip(cid)[1]

    def _clamp(self, dt, lo, hi):
        return min(hi, max(lo, dt)) if lo <= hi else 0.0

    def _drag_move(self, dt, x, y):
        d = self.drag
        group = d["group"]
        orig = {cid: self._o(cid) for cid in group}
        dt = max(dt, -min(o[0] for o in orig.values()))
        cands = [o[0] + dt for o in orig.values()] + [o[0] + o[2] + dt for o in orig.values()]
        s = self.snap_delta(cands, group)
        if s is not None and min(o[0] for o in orig.values()) + dt + s >= 0:
            dt += s
        dt = self.project.frame(dt)
        gkind = d["track"].kind
        lst = self.project.tracks(gkind)
        target = self.track_at(y)
        shift = 0
        if target is not None and target.kind == gkind:
            shift = lst.index(target) - lst.index(d["track"])
            for o in orig.values():
                if o[4].kind == gkind and not (0 <= lst.index(o[4]) + shift < len(lst)):
                    shift = 0
        for cid, (s0, i0, d0, sp, tr0) in orig.items():
            cur_tr, c = self.project.find_clip(cid)
            c.start = s0 + dt
            dest = lst[lst.index(tr0) + shift] if (tr0.kind == gkind and shift) else tr0
            if dest is not cur_tr:
                cur_tr.clips.remove(c)
                dest.clips.append(c)
            dest.sort()
        self.overlay = (x, y, f"{'+' if dt >= 0 else '-'}{fmt_tc(abs(dt), self.project.fps)}")

    def _trim_bounds(self, side, group):
        lo, hi = -math.inf, math.inf
        fd = self._fdur()
        for cid in group:
            s0, i0, d0, sp, _ = self._o(cid)
            smin, smax = self.src_limits(self._clip(cid))
            if side == "left":
                hi = min(hi, d0 - fd)
                lo = max(lo, -s0, (smin - i0) / sp)
            else:
                lo = max(lo, -(d0 - fd))
                hi = min(hi, (smax - i0) / sp - d0)
        return lo, hi

    def _drag_trim(self, dt, x, y):
        d = self.drag
        side = d["mode"]
        lo, hi = self._trim_bounds(side, d["group"])
        g = self._o(d["clip"])
        edge_t = g[0] + dt if side == "left" else g[0] + g[2] + dt
        s = self.snap_delta([edge_t], d["group"])
        if s is not None:
            dt += s
        dt = self._clamp(self.project.frame(dt), lo, hi)
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            c = self._clip(cid)
            if side == "left":
                c.start, c.in_point, c.duration = s0 + dt, i0 + dt * sp, d0 - dt
            else:
                c.duration = d0 + dt
            self._fix_fades(c)
        gc = self._clip(d["clip"])
        self.overlay = (x, y, f"{'+' if dt >= 0 else '-'}{fmt_tc(abs(dt), self.project.fps)}  "
                              f"Dur {fmt_tc(gc.duration, self.project.fps)}")

    def _drag_ripple(self, dt, x, y):
        d = self.drag
        side = d["mode"].split("_")[1]
        fd = self._fdur()
        lo, hi = -math.inf, math.inf
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            smin, smax = self.src_limits(self._clip(cid))
            if side == "right":
                lo, hi = max(lo, -(d0 - fd)), min(hi, (smax - i0) / sp - d0)
            else:  # ripple the head: start stays, in point moves, later clips follow
                lo, hi = max(lo, (smin - i0) / sp), min(hi, d0 - fd)
        if side == "right":
            g = self._o(d["clip"])
            s = self.snap_delta([g[0] + g[2] + dt], d["group"] | d["followers"])
            if s is not None:
                dt += s
        dt = self._clamp(self.project.frame(dt), lo, hi)
        shift = dt if side == "right" else -dt
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            c = self._clip(cid)
            if side == "right":
                c.duration = d0 + dt
            else:
                c.in_point, c.duration = i0 + dt * sp, d0 - dt
            self._fix_fades(c)
        for cid in d["followers"]:
            self._clip(cid).start = self._o(cid)[0] + shift
        gc = self._clip(d["clip"])
        self.overlay = (x, y, f"Ripple {'+' if shift >= 0 else '-'}{fmt_tc(abs(shift), self.project.fps)}  "
                              f"Dur {fmt_tc(gc.duration, self.project.fps)}")

    def _drag_rate(self, dt, x, y):
        d = self.drag
        side = d["mode"].split("_")[1]
        lo, hi = -math.inf, math.inf
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            src_len = d0 * sp
            dmin, dmax = max(self._fdur(), src_len / 16), src_len / 0.0625  # speed 6.25% .. 1600%
            if side == "right":
                lo, hi = max(lo, dmin - d0), min(hi, dmax - d0)
            else:
                lo, hi = max(lo, d0 - dmax, -s0), min(hi, d0 - dmin)
        g = self._o(d["clip"])
        edge_t = g[0] + g[2] + dt if side == "right" else g[0] + dt
        s = self.snap_delta([edge_t], d["group"])
        if s is not None:
            dt += s
        dt = self._clamp(self.project.frame(dt), lo, hi)
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            c = self._clip(cid)
            new_d = d0 + dt if side == "right" else d0 - dt
            if side == "left":
                c.start = s0 + dt
            c.duration = new_d
            c.speed = (d0 * sp) / new_d
            self._fix_fades(c)
        self.overlay = (x, y, f"Speed {self._clip(d['clip']).speed * 100:.1f}%  "
                              f"Dur {fmt_tc(self._clip(d['clip']).duration, self.project.fps)}")

    def _drag_rolling(self, dt, x, y):
        d = self.drag
        fd = self._fdur()
        lo, hi = -math.inf, math.inf
        for a, b, lo_gap, hi_gap in d["pairs"]:
            if a:
                s0, i0, d0, sp, _ = self._o(a)
                smin, smax = self.src_limits(self._clip(a))
                lo, hi = max(lo, -(d0 - fd)), min(hi, (smax - i0) / sp - d0)
                if not b:
                    hi = min(hi, hi_gap - (s0 + d0))
            if b:
                s0, i0, d0, sp, _ = self._o(b)
                smin, smax = self.src_limits(self._clip(b))
                lo, hi = max(lo, (smin - i0) / sp), min(hi, d0 - fd)
                if not a:
                    lo = max(lo, lo_gap - s0)
        excl = {i for pair in d["pairs"] for i in pair[:2] if i}
        s = self.snap_delta([d["edit_t"] + dt], excl)
        if s is not None:
            dt += s
        dt = self._clamp(self.project.frame(dt), lo, hi)
        for a, b, _, _ in d["pairs"]:
            if a:
                self._clip(a).duration = self._o(a)[2] + dt
                self._fix_fades(self._clip(a))
            if b:
                s0, i0, d0, sp, _ = self._o(b)
                c = self._clip(b)
                c.start, c.in_point, c.duration = s0 + dt, i0 + dt * sp, d0 - dt
                self._fix_fades(c)
        self.snap_line = d["edit_t"] + dt
        self.overlay = (x, y, f"Roll {'+' if dt >= 0 else '-'}{fmt_tc(abs(dt), self.project.fps)}")

    def _drag_slip(self, dt, x, y):
        d = self.drag
        lo, hi = -math.inf, math.inf
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            smin, smax = self.src_limits(self._clip(cid))
            # in = i0 - dt*sp must stay within [smin, smax - d0*sp]
            lo, hi = max(lo, (i0 + d0 * sp - smax) / sp), min(hi, (i0 - smin) / sp)
        dt = self._clamp(self.project.frame(dt), lo, hi)
        for cid in d["group"]:
            s0, i0, d0, sp, _ = self._o(cid)
            self._clip(cid).in_point = i0 - dt * sp
        gc = self._clip(d["clip"])
        self.overlay = (x, y, f"In {fmt_tc(gc.in_point, self.project.fps)}  "
                              f"Out {fmt_tc(gc.src_end, self.project.fps)}")

    def _drag_slide(self, dt, x, y):
        d = self.drag
        fd = self._fdur()
        lo, hi = -math.inf, math.inf
        for cid, prev, nxt, prev_end, next_start in d["slide"]:
            s0, i0, d0, sp, _ = self._o(cid)
            if prev:
                ps, pi, pd, psp, _ = self._o(prev)
                smin, smax = self.src_limits(self._clip(prev))
                lo, hi = max(lo, -(pd - fd)), min(hi, (smax - pi) / psp - pd)
            else:
                lo = max(lo, prev_end - s0)
            if nxt:
                ns, ni, nd, nsp, _ = self._o(nxt)
                smin, smax = self.src_limits(self._clip(nxt))
                lo, hi = max(lo, (smin - ni) / nsp), min(hi, nd - fd)
            else:
                hi = min(hi, next_start - (s0 + d0))
        excl = {i for row in d["slide"] for i in row[:3] if i}
        g = self._o(d["clip"])
        s = self.snap_delta([g[0] + dt, g[0] + g[2] + dt], excl)
        if s is not None:
            dt += s
        dt = self._clamp(self.project.frame(dt), lo, hi)
        for cid, prev, nxt, _, _ in d["slide"]:
            self._clip(cid).start = self._o(cid)[0] + dt
            if prev:
                c = self._clip(prev)
                c.duration = self._o(prev)[2] + dt
                self._fix_fades(c)
            if nxt:
                ns, ni, nd, nsp, _ = self._o(nxt)
                c = self._clip(nxt)
                c.start, c.in_point, c.duration = ns + dt, ni + dt * nsp, nd - dt
                self._fix_fades(c)
            tr, _ = self.project.find_clip(cid)
            tr.sort()
        self.overlay = (x, y, f"Slide {'+' if dt >= 0 else '-'}{fmt_tc(abs(dt), self.project.fps)}")

    @staticmethod
    def _fix_fades(c: Clip):
        c.fade_in = min(c.fade_in, c.duration)
        c.fade_out = min(c.fade_out, c.duration)

    def mouseReleaseEvent(self, e):
        d, self.drag = self.drag, None
        self.snap_line = None
        self.overlay = None
        if d and d.get("changed") and "before" in d:
            if d["mode"] in ("move", "left", "right", "rate_left", "rate_right"):
                # overwrite whatever the edited clips now cover
                for cid in d["group"]:
                    tr, c = self.project.find_clip(cid)
                    carve(tr, c.start, c.end, exclude=set(d["group"]))
            self.edited.emit(d["before"])
        self._update_cursor(e.position().x(), e.position().y())
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.tool != "select":
            return
        tr, clip, _ = self.hit(e.position().x(), e.position().y())
        if clip is not None:
            self.seek.emit(clip.start)

    def _update_cursor(self, x, y):
        if x < HEADER or y < RULER:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if self.tool != "select":
            self.setCursor(icons.cursor(self.tool))
            return
        _, c, edge = self.hit(x, y)
        if edge:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif c is not None:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------ drag & drop from bin
    def transition_target(self, x: float, y: float):
        """Which clip edge a transition dropped at (x, y) attaches to: (clip, side, track) or None."""
        tr, clip, _ = self.hit(x, y)
        if clip is None:
            return None
        if x < self.clip_rect(tr, clip).center().x():
            return clip, "in", tr
        nxt = next((c for c in tr.clips if c is not clip and abs(c.start - clip.end) < 1e-3), None)
        return (nxt, "in", tr) if nxt is not None else (clip, "out", tr)

    @staticmethod
    def transition_span(tr: Track, clip: Clip, side: str, dur: float = 1.0) -> tuple[float, float]:
        if side == "out":
            d = min(dur, clip.duration)
            return clip.end - d, clip.end
        prev = next((c for c in tr.clips if c is not clip and abs(c.end - clip.start) < 1e-3), None)
        if prev is not None:
            d = min(dur, 2 * prev.duration, 2 * clip.duration)
            return clip.start - d / 2, clip.start + d / 2
        return clip.start, clip.start + min(dur, clip.duration)

    def dragEnterEvent(self, e):
        md = e.mimeData()
        if md.hasFormat(MEDIA_MIME) or md.hasFormat(TRANSITION_MIME) or md.hasFormat(GRAPHIC_MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if e.mimeData().hasFormat(GRAPHIC_MIME):
            pid = bytes(e.mimeData().data(GRAPHIC_MIME)).decode()
            tr = self.track_at(y)
            if tr is None or tr.kind != "video":
                self.gghost = None
                e.ignore()
            else:
                dur = preset_duration(pid)
                t = max(0.0, self.x2t(x))
                if self.snap:
                    self.snap_line = None
                    s = self.snap_delta([t, t + dur], set())
                    if s is not None:
                        t = max(0.0, t + s)
                self.gghost = (pid, tr, self.project.frame(t), dur)
                e.acceptProposedAction()
            self.update()
            return
        if e.mimeData().hasFormat(TRANSITION_MIME):
            tgt = self.transition_target(x, y)
            self.tghost = None
            if tgt is not None:
                clip, side, tr = tgt
                self.tghost = (tr, *self.transition_span(tr, clip, side))
                e.acceptProposedAction()
            else:
                e.ignore()
            self.update()
            return
        if not e.mimeData().hasFormat(MEDIA_MIME):
            return
        mid = bytes(e.mimeData().data(MEDIA_MIME)).decode()
        t = max(0.0, self.x2t(x))
        m = self.project.media.get(mid)
        if m and self.snap:
            self.snap_line = None
            s = self.snap_delta([t, t + m.duration], set())
            if s is not None:
                t = max(0.0, t + s)
        self.ghost = (mid, self.project.frame(t), self.track_at(y))
        e.acceptProposedAction()
        self.update()

    def dragLeaveEvent(self, e):
        self.ghost = None
        self.tghost = None
        self.gghost = None
        self.snap_line = None
        self.update()

    def dropEvent(self, e):
        x, y = e.position().x(), e.position().y()
        if e.mimeData().hasFormat(GRAPHIC_MIME):
            g, self.gghost = self.gghost, None
            self.snap_line = None
            if g is not None:
                pid, tr, t, _ = g
                self.drop_graphic.emit(pid, t, tr)
                e.acceptProposedAction()
            self.update()
            return
        if e.mimeData().hasFormat(TRANSITION_MIME):
            self.tghost = None
            tgt = self.transition_target(x, y)
            if tgt is not None:
                kind = bytes(e.mimeData().data(TRANSITION_MIME)).decode()
                only = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self.drop_transition.emit(kind, tgt[0].id, tgt[1], only)
                e.acceptProposedAction()
        elif self.ghost:
            mid, t, tr = self.ghost
            self.ghost = None
            self.snap_line = None
            self.drop_media.emit(mid, t, tr)
            e.acceptProposedAction()
        self.update()

    def contextMenuEvent(self, e):
        x, y = e.pos().x(), e.pos().y()
        if x < HEADER or y < RULER:
            return
        tr, clip, _ = self.hit(x, y)
        if clip is not None and clip.id not in self.selection:
            self.selection = {c.id for c in self.project.linked(clip)} if self.linked_selection else {clip.id}
            self.selection_changed.emit()
            self.update()
        self.context_menu.emit(clip, e.globalPos(), max(0.0, self.x2t(x)))

    # ------------------------------------------------------------ painting
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, QColor("#1b1d22"))
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        p.setFont(small)

        rows = self.rows()
        # lanes
        for tr, y, h in rows:
            lane = QColor("#24272e") if tr.kind == "video" else QColor("#22292a")
            p.fillRect(HEADER, y, W - HEADER, h - 1, lane)
            p.setPen(QColor("#15161a"))
            p.drawLine(0, y + h - 1, W, y + h - 1)

        # clips
        p.save()
        p.setClipRect(HEADER, RULER, W - HEADER, H - RULER)
        for tr, y, h in rows:
            for c in tr.clips:
                self._paint_clip(p, tr, c)
            for t0, t1, kind, A, B in transition_windows(tr):
                self._paint_transition(p, y, h, t0, t1, short_label(kind))
        if self.gghost:
            _, gtr, gt, gd = self.gghost
            gy, gh = self.row_of(gtr)
            r = QRectF(self.t2x(gt), gy + 2, gd * self.pps, gh - 4)
            p.fillRect(r, QColor(194, 87, 127, 110))
            p.setPen(QPen(QColor(255, 255, 255, 180), 1, Qt.PenStyle.DashLine))
            p.drawRect(r)
        if self.tghost:
            gtr, t0, t1 = self.tghost
            gy, gh = self.row_of(gtr)
            self._paint_transition(p, gy, gh, t0, t1, "", ghost=True)
        for a, b in self.markers:
            xa, xb = self.t2x(a), self.t2x(b)
            if xb >= HEADER and xa <= W:
                p.fillRect(QRectF(xa, RULER, max(1.0, xb - xa), H - RULER), QColor(230, 70, 70, 55))
        if self.ghost:
            mid, t, gtr = self.ghost
            m = self.project.media.get(mid)
            if m:
                for kind in ("video", "audio"):
                    if (kind == "video" and m.has_video) or (kind == "audio" and m.has_audio):
                        dest = self.main.drop_target(m, gtr, kind)
                        if dest is not None:
                            gy, gh = self.row_of(dest)
                            r = QRectF(self.t2x(t), gy + 2, m.duration * self.pps, gh - 4)
                            p.fillRect(r, QColor(255, 255, 255, 50))
                            p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.PenStyle.DashLine))
                            p.drawRect(r)
        if self.snap_line is not None:
            sx = self.t2x(self.snap_line)
            p.setPen(QPen(QColor("#ffd34d"), 1))
            p.drawLine(int(sx), RULER, int(sx), H)
        p.restore()

        # headers
        for tr, y, h in rows:
            p.fillRect(0, y, HEADER, h - 1, QColor("#2b2f37"))
            p.setPen(QColor("#d9dce3"))
            bold = QFont(small)
            bold.setBold(True)
            p.setFont(bold)
            p.drawText(QRectF(10, y, 40, h), Qt.AlignmentFlag.AlignVCenter, tr.name)
            p.setFont(small)
            r = self.toggle_rect(y, h)
            active = tr.muted
            p.fillRect(r, QColor("#c94f4f") if active else QColor("#3a3f49"))
            p.setPen(QColor("#ffffff") if active else QColor("#9aa0ab"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, ("H" if tr.kind == "video" else "M"))
        p.fillRect(0, RULER - self.scroll_y + len(self.project.video_tracks) * VIDEO_H, W, GAP, QColor("#121317"))

        self._paint_ruler(p, W)
        for a, b in self.markers:
            xa, xb = max(HEADER, self.t2x(a)), self.t2x(b)
            if xb > xa:
                p.fillRect(QRectF(xa, RULER - 5, xb - xa, 5), QColor("#e05050"))

        if self.overlay:
            ox, oy, text = self.overlay
            fm = p.fontMetrics()
            r = QRectF(ox + 14, oy + 16, fm.horizontalAdvance(text) + 14, fm.height() + 8)
            if r.right() > W:
                r.moveRight(ox - 8)
            p.fillRect(r, QColor(10, 11, 13, 225))
            p.setPen(QColor("#ffd34d"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

        # playhead
        px = self.t2x(self.playhead)
        if px >= HEADER:
            p.setPen(QPen(QColor("#ff4d4d"), 1))
            p.drawLine(int(px), RULER - 6, int(px), H)
            p.setBrush(QColor("#ff4d4d"))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.drawPolygon(QPolygonF([QPointF(px - 6, RULER - 12), QPointF(px + 6, RULER - 12),
                                     QPointF(px, RULER - 4)]))
        p.end()

    def _paint_ruler(self, p: QPainter, W: int):
        p.fillRect(0, 0, W, RULER, QColor("#2b2f37"))
        p.fillRect(0, 0, HEADER, RULER, QColor("#22252c"))
        p.setPen(QColor("#9aa0ab"))
        p.drawText(QRectF(8, 0, HEADER - 8, RULER), Qt.AlignmentFlag.AlignVCenter,
                   fmt_tc(self.playhead, self.project.fps))
        fps = self.project.fps
        steps = [1 / fps, 2 / fps, 5 / fps, 10 / fps, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600]
        major = next((s for s in steps if s * self.pps >= 90), steps[-1])
        minor = next((s for s in steps if s * self.pps >= 10), major)
        t0 = self.x2t(HEADER)
        t1 = self.x2t(W)
        p.save()
        p.setClipRect(HEADER, 0, W - HEADER, RULER)
        k = math.floor(t0 / minor)
        while k * minor <= t1:
            t = k * minor
            x = int(self.t2x(t))
            is_major = abs(t / major - round(t / major)) < 1e-6
            p.setPen(QColor("#6d737e"))
            p.drawLine(x, RULER - (10 if is_major else 4), x, RULER)
            if is_major:
                p.setPen(QColor("#c4c8d0"))
                label = fmt_tc(t, fps) if major < 1 else (
                    f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}" if t >= 3600
                    else f"{int(t // 60):02d}:{int(t % 60):02d}")
                p.drawText(x + 3, 12, label)
            k += 1
        p.restore()

    def _paint_transition(self, p: QPainter, y: int, h: int, t0: float, t1: float, label: str, ghost=False):
        r = QRectF(self.t2x(t0), y + 15, max(6.0, (t1 - t0) * self.pps), h - 19)
        if r.right() < HEADER or r.left() > self.width():
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(QColor(255, 255, 255, 120) if ghost else QColor(40, 30, 60, 200))
        p.setPen(QPen(QColor("#ffffff") if ghost else QColor("#c9b8ff"), 1.2,
                      Qt.PenStyle.DashLine if ghost else Qt.PenStyle.SolidLine))
        p.drawRoundedRect(r, 3, 3)
        p.setPen(QPen(QColor(201, 184, 255, 160), 1))
        p.drawLine(r.bottomLeft(), r.topRight())
        if label and r.width() > 40:
            p.setPen(QColor("#ffffff"))
            p.drawText(r.adjusted(3, 0, -3, 0), Qt.AlignmentFlag.AlignCenter, label)
        p.restore()

    def _paint_clip(self, p: QPainter, tr: Track, c: Clip):
        r = self.clip_rect(tr, c)
        if r.right() < HEADER or r.left() > self.width():
            return
        base = CLIP_COLORS.get(c.kind, QColor("#777"))
        if tr.muted:
            base = base.darker(180)
        sel = c.id in self.selection
        p.fillRect(r, base.darker(130) if not sel else base.lighter(115))
        m = self.project.media.get(c.media_id) if c.media_id else None
        # thumbnail strip / waveform
        inner = r.adjusted(1, 14, -1, -1)
        if c.kind in ("video", "image") and m is not None:
            thumb = self.main.thumbs.get(m.id)
            if thumb is not None and inner.height() > 4:
                tw = thumb.width() * inner.height() / thumb.height()
                x = inner.left()
                p.save()
                p.setClipRect(inner.intersected(QRectF(HEADER, 0, self.width(), self.height())))
                while x < inner.right() and x < self.width():
                    p.drawImage(QRectF(x, inner.top(), tw, inner.height()), thumb)
                    x += max(tw, 40)
                    if r.width() > 2000:
                        break
                p.restore()
        elif c.kind == "audio" and m is not None:
            peaks = self.main.audio_cache.peaks.get(m.id)
            if peaks is not None and len(peaks):
                p.setPen(QColor(200, 255, 220, 170))
                mid = inner.center().y()
                amp = inner.height() / 2
                x0 = max(int(inner.left()), HEADER)
                x1 = min(int(inner.right()), self.width())
                for x in range(x0, x1):
                    st = c.in_point + (x - r.left()) / self.pps * c.speed
                    en = st + c.speed / self.pps
                    i0 = int(st * 100)
                    i1 = max(i0 + 1, int(en * 100))
                    if i0 >= len(peaks) or i0 < 0:
                        continue
                    v = float(peaks[i0:i1].max()) * c.volume
                    hgt = min(amp, v * amp)
                    p.drawLine(x, int(mid - hgt), x, int(mid + hgt))
        # fades
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))
        if c.fade_in > 0:
            w = c.fade_in * self.pps
            p.drawPolygon(QPolygonF([r.topLeft(), QPointF(r.left() + w, r.top()), r.bottomLeft()]))
        if c.fade_out > 0:
            w = c.fade_out * self.pps
            p.drawPolygon(QPolygonF([QPointF(r.right() - w, r.top()), r.topRight(), r.bottomRight()]))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # label bar
        p.fillRect(QRectF(r.left(), r.top(), r.width(), 13), base)
        p.setPen(QColor("#ffffff"))
        name = c.text.splitlines()[0] if c.kind == "title" and c.text else (m.name if m else c.kind.title())
        if c.link:
            name = "⛓ " + name
        if abs(c.speed - 1) > 1e-6:
            name += f"  [{c.speed * 100:.0f}%]"
        p.drawText(r.adjusted(4, -1, -2, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, name)
        p.setPen(QPen(QColor("#ffffff") if sel else QColor(0, 0, 0, 150), 2 if sel else 1))
        p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))


class Timeline(QWidget):
    """Canvas + horizontal scrollbar."""

    def __init__(self, main):
        super().__init__()
        self.canvas = TimelineCanvas(main)
        self.hbar = QScrollBar(Qt.Orientation.Horizontal)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self.hbar)
        self._syncing = False
        self.canvas.view_changed.connect(self.sync_bar)
        self.hbar.valueChanged.connect(self._bar_moved)

    def sync_bar(self):
        c = self.canvas
        self._syncing = True
        total = (max(c.project.duration(), c.playhead) + 30) * c.pps
        page = max(1, c.width() - HEADER)
        self.hbar.setRange(0, int(max(0, total - page)))
        self.hbar.setPageStep(page)
        self.hbar.setSingleStep(40)
        self.hbar.setValue(int(c.scroll_t * c.pps))
        self._syncing = False

    def _bar_moved(self, v):
        if not self._syncing:
            self.canvas.scroll_t = v / self.canvas.pps
            self.canvas.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.sync_bar()
