"""Super8 main window."""
from __future__ import annotations

import json
import os
import threading
import traceback

import numpy as np
from PyQt6.QtCore import QElapsedTimer, QObject, QPointF, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QColor, QImage, QKeySequence, QPalette
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QLabel, QInputDialog, QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressDialog,
                             QPushButton, QGridLayout, QSizePolicy, QSpinBox, QScrollArea, QSplitter, QTabWidget, QToolButton,
                             QVBoxLayout, QWidget)

from ..compositor import mix_audio
from ..export import export_project
from ..media import AudioCache, MediaLoader, probe
from ..model import Clip, Project, carve, new_id, remove_clips, ripple_ranges, split_with_links
from ..audiofx import envelope_db
from ..transitions import AUDIO_TRANSITIONS, DEFAULT_VIDEO, VIDEO_TRANSITIONS, for_track, short_label
from .audio_tools import NoiseDialog, SilenceDialog
from ..graphics import USER_PRESETS, all_presets, apply_preset, block_rect, preset_duration, template_from_clip
from .panels import EffectsPanel, GraphicsPanel, Inspector, MediaBin, Monitor, RenderWorker
from . import icons
from .. import __version__
from .about import AboutDialog, app_icon
from .timeline import TOOLS, Timeline, fmt_tc

APP = "Super8"
PROJECT_FILTER = "Super8 project (*.s8proj)"
MEDIA_FILTER = ("Media (*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mts *.mpg *.mpeg *.flv *.wmv *.ogv "
                "*.mp3 *.wav *.flac *.ogg *.opus *.m4a *.aac *.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff);;"
                "All files (*)")
FPS_CHOICES = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]


class Bridge(QObject):
    """Carries results from background threads onto the GUI thread."""
    thumb = pyqtSignal(str, object)
    audio = pyqtSignal(str)
    error = pyqtSignal(str, str)
    export_progress = pyqtSignal(int, int)
    export_done = pyqtSignal(bool, str)
    variant_ready = pyqtSignal(str, float)
    filmstrip = pyqtSignal(str, object)
    variant_progress = pyqtSignal(str, float)


class History:
    def __init__(self, limit: int = 300):
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self.limit = limit

    def push(self, state: dict):
        self.undo_stack.append(state)
        del self.undo_stack[:-self.limit]
        self.redo_stack.clear()

    def undo(self, current: dict):
        if not self.undo_stack:
            return None
        self.redo_stack.append(current)
        return self.undo_stack.pop()

    def redo(self, current: dict):
        if not self.redo_stack:
            return None
        self.undo_stack.append(current)
        return self.redo_stack.pop()


class AudioPlayer:
    """Pushes the live mix into a QAudioSink while playing."""

    def __init__(self, cache: AudioCache):
        self.cache = cache
        self.sink = None
        self.io = None
        self.timer = QTimer()
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._feed)
        self.available = True

    def start(self, project: Project, t: float):
        self.stop()
        if not self.available:
            return
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
            dev = QMediaDevices.defaultAudioOutput()
            if dev.isNull():
                return
            fmt = QAudioFormat()
            fmt.setSampleRate(project.sample_rate)
            fmt.setChannelCount(2)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
            self.sink = QAudioSink(dev, fmt)
            self.sink.setBufferSize(int(project.sample_rate * 8 * 0.1))
            self.io = self.sink.start()
            if self.io is None:
                self.sink = None
                return
        except Exception:
            traceback.print_exc()
            self.available = False
            self.sink = None
            return
        self.project, self.pos = project, t
        self._feed()
        self.timer.start()

    def _feed(self):
        if self.sink is None:
            return
        n = self.sink.bytesFree() // 8
        if n < 128:
            return
        data = mix_audio(self.project, self.cache, self.pos, n)
        self.io.write(data.tobytes())
        self.pos += n / self.project.sample_rate

    def stop(self):
        self.timer.stop()
        if self.sink is not None:
            self.sink.stop()
            self.sink = None
            self.io = None


# ===================================================================== dialogs
class SequenceDialog(QDialog):
    def __init__(self, parent, project: Project):
        super().__init__(parent)
        self.setWindowTitle("Sequence Settings")
        f = QFormLayout(self)
        self.w = QSpinBox()
        self.w.setRange(16, 7680)
        self.w.setSingleStep(2)
        self.w.setValue(project.width)
        self.h = QSpinBox()
        self.h.setRange(16, 4320)
        self.h.setSingleStep(2)
        self.h.setValue(project.height)
        self.preset = QComboBox()
        self.presets = [("Custom", None), ("4K UHD 3840×2160", (3840, 2160)), ("1080p 1920×1080", (1920, 1080)),
                        ("720p 1280×720", (1280, 720)), ("Vertical 1080×1920", (1080, 1920)),
                        ("Square 1080×1080", (1080, 1080))]
        for name, _ in self.presets:
            self.preset.addItem(name)
        self.preset.currentIndexChanged.connect(self._apply_preset)
        self.fps = QComboBox()
        for v in FPS_CHOICES:
            self.fps.addItem(f"{v:g}", v)
        self.fps.setCurrentIndex(min(range(len(FPS_CHOICES)), key=lambda i: abs(FPS_CHOICES[i] - project.fps)))
        f.addRow("Preset", self.preset)
        f.addRow("Width", self.w)
        f.addRow("Height", self.h)
        f.addRow("Frame rate", self.fps)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _apply_preset(self, i):
        v = self.presets[i][1]
        if v:
            self.w.setValue(v[0])
            self.h.setValue(v[1])


class ExportDialog(QDialog):
    QUALITY = [("High (CRF 18)", 18), ("Good (CRF 21)", 21), ("Medium (CRF 24)", 24), ("Small file (CRF 28)", 28)]
    SPEED = ["veryfast", "fast", "medium", "slow"]

    def __init__(self, parent, project: Project, default_path: str):
        super().__init__(parent)
        self.setWindowTitle("Export Media")
        f = QFormLayout(self)
        row = QHBoxLayout()
        self.path = QLineEdit(default_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        f.addRow("Output", row)
        f.addRow("Format", QLabel(f"H.264 + AAC · {project.width}×{project.height} · {project.fps:g} fps · "
                                  f"{fmt_tc(project.duration(), project.fps)}"))
        self.quality = QComboBox()
        for name, crf in self.QUALITY:
            self.quality.addItem(name, crf)
        self.quality.setCurrentIndex(1)
        self.speed = QComboBox()
        self.speed.addItems(self.SPEED)
        self.speed.setCurrentText("medium")
        f.addRow("Quality", self.quality)
        f.addRow("Encoder speed", self.speed)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.resize(560, 10)

    def _browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export to", self.path.text(), "MP4 (*.mp4);;MKV (*.mkv);;MOV (*.mov)")
        if p:
            self.path.setText(p)


# ===================================================================== main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("super8", "super8")
        self.project = Project.new()
        self.project_path: str | None = None
        self.modified = False
        self.history = History()
        self.audio_cache = AudioCache()
        self.thumbs: dict[str, QImage] = {}
        self.filmstrips: dict[str, tuple] = {}  # media id -> (times, [QImage]) for the timeline
        self.playhead = 0.0
        self.playing = False
        self.rev = 0
        self._clone, self._clone_rev = None, -1
        self._prop_key = None

        self.bridge = Bridge(self)  # parented: dies with the window, so queued results are dropped
        self.bridge.thumb.connect(self._on_thumb)
        self.bridge.audio.connect(self._on_audio_loaded)
        self.bridge.error.connect(self._on_media_error)
        self.bridge.variant_ready.connect(self._on_variant_ready)
        self.bridge.variant_progress.connect(self._on_variant_progress)
        self.audio_cache.on_variant = self.bridge.variant_ready.emit
        self.audio_cache.on_variant_progress = self.bridge.variant_progress.emit
        self.bridge.filmstrip.connect(self._on_filmstrip)
        self.loader = MediaLoader(self.audio_cache, self.bridge.thumb.emit, self.bridge.audio.emit,
                                  self.bridge.error.emit, self.bridge.filmstrip.emit)
        self.worker = RenderWorker(self.audio_cache)
        self.worker.frame_ready.connect(self._on_frame)
        self.worker.start()
        self.player = AudioPlayer(self.audio_cache)
        self.clock = QElapsedTimer()
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(15)
        self.play_timer.timeout.connect(self._tick)

        self._build_ui()
        self._build_actions()
        self.resize(1500, 920)
        self._refresh_title()
        self.changed(rebuild_bin=True)

    # ------------------------------------------------------------- UI
    def _build_ui(self):
        self.bin = MediaBin()
        self.bin.import_requested.connect(self.import_dialog)
        self.bin.add_requested.connect(lambda mid: self.place_media(mid, self.playhead, None))
        self.bin.remove_requested.connect(self.remove_media)
        self.bin.list.files_dropped.connect(self.import_files)

        self.monitor = Monitor()
        self.monitor.resized.connect(self.request_frame)
        self.monitor.pressed.connect(self.on_monitor_press)
        self.monitor.dragged.connect(self.on_monitor_drag)
        self.monitor.released.connect(self.on_monitor_release)
        self.monitor.wheeled.connect(self.on_monitor_wheel)
        mon_box = QWidget()
        mv = QVBoxLayout(mon_box)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(2)
        mv.addWidget(self.monitor, 1)
        transport = QHBoxLayout()
        transport.setContentsMargins(6, 0, 6, 4)
        self.tc_label = QLabel("00:00:00:00")
        self.tc_label.setStyleSheet("font-family: monospace; font-size: 15px; color: #ffcf5c")
        self.dur_label = QLabel("")
        self.dur_label.setStyleSheet("font-family: monospace; color: #8a909b")
        buttons = []
        for text, tip, fn in (("⏮", "Go to start (Home)", lambda: self.set_playhead(0)),
                              ("⏪", "Previous edit (Up)", lambda: self.jump_edit(-1)),
                              ("◀|", "Previous frame (Left)", lambda: self.step(-1)),
                              ("▶", "Play / Pause (Space)", self.toggle_play),
                              ("|▶", "Next frame (Right)", lambda: self.step(1)),
                              ("⏩", "Next edit (Down)", lambda: self.jump_edit(1)),
                              ("⏭", "Go to end (End)", lambda: self.set_playhead(self.project.duration()))):
            b = QToolButton(text=text)
            b.setToolTip(tip)
            b.setMinimumWidth(34)
            b.clicked.connect(fn)
            buttons.append(b)
        self.play_btn = buttons[3]
        transport.addWidget(self.tc_label)
        transport.addStretch(1)
        for b in buttons:
            transport.addWidget(b)
        transport.addStretch(1)
        transport.addWidget(self.dur_label)
        mv.addLayout(transport)

        self.inspector = Inspector()
        self.inspector.changed.connect(self.on_property)

        self.timeline = Timeline(self)
        c = self.timeline.canvas
        c.seek.connect(self.on_seek)
        c.edited.connect(self.commit)
        c.selection_changed.connect(self._refresh_inspector)
        c.drop_media.connect(self.place_media)
        c.drop_transition.connect(lambda kind, cid, side, only: self.apply_transition(cid, side, kind, only))
        c.context_menu.connect(self.on_context_menu)

        self.effects = EffectsPanel()
        self.effects.apply_transition.connect(self.apply_transition_at_playhead)
        self.effects.detect_silences.connect(self.detect_silences)
        self.effects.noise_reduction.connect(self.noise_reduction_dialog)
        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(self.bin, "Project")
        self.left_tabs.addTab(self.effects, "Effects")
        self.graphics = GraphicsPanel()
        self.graphics.add_requested.connect(lambda pid: self.add_title(preset=pid))
        self.graphics.delete_template.connect(self.delete_template)
        self._load_templates()
        self.graphics.rebuild()
        self.left_tabs.addTab(self.graphics, "Graphics")
        c.drop_graphic.connect(lambda pid, t, tr: self.add_title(t, tr, preset=pid))
        self.inspector.save_template.connect(self.save_template)
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setWidget(self.inspector)
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(self.left_tabs)
        top.addWidget(mon_box)
        top.addWidget(self.inspector_scroll)
        top.setSizes([330, 860, 290])
        top.setStretchFactor(1, 1)

        # Premiere-style tools panel (two columns) to the left of the timeline
        self.tools_panel = QWidget()
        self.tools_panel.setObjectName("toolsPanel")
        grid = QGridLayout(self.tools_panel)
        grid.setContentsMargins(4, 6, 4, 6)
        grid.setSpacing(2)
        self.tool_group = QActionGroup(self)
        self.tool_actions: dict[str, QAction] = {}
        for i, (tid, (label, key, icon_name, hint)) in enumerate(TOOLS.items()):
            a = QAction(icons.icon(icon_name), label, self, checkable=True)
            a.setShortcut(QKeySequence(key))
            a.setToolTip(f"<b>{label}</b> ({key})<br>{hint}")
            a.triggered.connect(lambda _=False, tid=tid: self.set_tool(tid))
            self.tool_group.addAction(a)
            self.tool_actions[tid] = a
            btn = QToolButton()
            btn.setDefaultAction(a)
            btn.setIconSize(QSize(20, 20))
            btn.setAutoRaise(True)
            grid.addWidget(btn, i // 2, i % 2)
        grid.setRowStretch(len(TOOLS) // 2 + 1, 1)
        self.tool_actions["select"].setChecked(True)
        tl_box = QWidget()
        tl_lay = QHBoxLayout(tl_box)
        tl_lay.setContentsMargins(0, 0, 0, 0)
        tl_lay.setSpacing(0)
        tl_lay.addWidget(self.tools_panel)
        tl_lay.addWidget(self.timeline, 1)

        main = QSplitter(Qt.Orientation.Vertical)
        main.addWidget(top)
        main.addWidget(tl_box)
        main.setSizes([560, 380])
        self.setCentralWidget(main)

        self.tool_hint = QLabel()
        self.tool_hint.setStyleSheet("color:#9aa0ab; padding: 0 6px")
        self.statusBar().addPermanentWidget(self.tool_hint)
        self.seq_label = QLabel()
        self.seq_label.setStyleSheet("color:#8a909b; padding-left:8px")

    def _act(self, menu, text, fn, key=None, tip=None, icon_name=None, checkable=False, checked=False):
        a = QAction(text, self)
        if icon_name:
            a.setIcon(icons.icon(icon_name))
        keys = []
        if key:
            keys = key if isinstance(key, (list, tuple)) else [key]
            a.setShortcuts([QKeySequence(k) for k in keys])
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
            a.toggled.connect(fn)
        else:
            a.triggered.connect(lambda _=False: fn())
        tip_text = f"<b>{text.rstrip('…')}</b>" + (f" ({keys[0]})" if keys else "") + (f"<br>{tip}" if tip else "")
        a.setToolTip(tip_text)
        if tip:
            a.setStatusTip(tip)
        if menu is not None:
            menu.addAction(a)
        return a

    def _build_actions(self):
        A = self.actions_by_name = {}
        mb = self.menuBar()
        m = mb.addMenu("&File")
        A["new"] = self._act(m, "New Project", self.new_project, "Ctrl+N", icon_name="new")
        A["open"] = self._act(m, "Open Project…", self.open_dialog, "Ctrl+O", icon_name="open")
        A["save"] = self._act(m, "Save Project", self.save, "Ctrl+S", icon_name="save")
        self._act(m, "Save Project As…", self.save_as, "Ctrl+Shift+S")
        m.addSeparator()
        A["import"] = self._act(m, "Import Media…", self.import_dialog, "Ctrl+I", icon_name="import",
                                tip="Add video, audio or image files to the project")
        A["export"] = self._act(m, "Export Media…", self.export_dialog, "Ctrl+M", icon_name="export",
                                tip="Render the sequence to an MP4/MKV/MOV file")
        m.addSeparator()
        self._act(m, "Quit", self.close, "Ctrl+Q")

        m = mb.addMenu("&Edit")
        A["undo"] = self._act(m, "Undo", self.undo, "Ctrl+Z", icon_name="undo")
        A["redo"] = self._act(m, "Redo", self.redo, ["Ctrl+Shift+Z", "Ctrl+Y"], icon_name="redo")
        m.addSeparator()
        A["delete"] = self._act(m, "Delete", lambda: self.delete_selected(False), ["Delete", "Backspace"],
                                icon_name="delete", tip="Remove the selected clips, leaving a gap")
        A["ripple_delete"] = self._act(m, "Ripple Delete", lambda: self.delete_selected(True),
                                       ["Shift+Delete", "Shift+Backspace"], icon_name="ripple_delete",
                                       tip="Remove the selected clips and close the gap")
        A["split"] = self._act(m, "Split at Playhead", self.split_at_playhead, "Ctrl+K", icon_name="split",
                               tip="Cut the clips under the playhead (only selected ones if any are selected)")
        m.addSeparator()
        self._act(m, "Select All", self.select_all, "Ctrl+A")
        self._act(m, "Deselect All", self.deselect, "Ctrl+Shift+A")
        A["link"] = self._act(m, "Link / Unlink Selected", self.toggle_link, "Ctrl+L")

        m = mb.addMenu("&Tools")
        for tid, a in self.tool_actions.items():
            m.addAction(a)

        m = mb.addMenu("&Sequence")
        self._act(m, "Sequence Settings…", self.sequence_dialog)
        A["track_add"] = self._act(m, "Add Video Track", lambda: self._add_track("video"), icon_name="track_add")
        self._act(m, "Add Audio Track", lambda: self._add_track("audio"))
        A["title"] = self._act(m, "Add Title at Playhead", lambda: self.add_title(), "Ctrl+T", icon_name="title",
                               tip="Insert a text title at the playhead on a free video track")
        m.addSeparator()
        A["snap"] = self._act(m, "Snapping", self._set_snap, "S", icon_name="magnet", checkable=True, checked=True,
                              tip="Clips, edges and the playhead snap to each other")
        A["linked"] = self._act(m, "Linked Selection", self._set_linked, "Shift+L", icon_name="link",
                                checkable=True, checked=True,
                                tip="Clicking a clip also selects its linked audio/video (Alt inverts)")
        m.addSeparator()
        A["zoom_in"] = self._act(m, "Zoom In", lambda: self.timeline.canvas.set_zoom(self.timeline.canvas.pps * 1.5),
                                 ["=", "+"], icon_name="zoom_in")
        A["zoom_out"] = self._act(m, "Zoom Out", lambda: self.timeline.canvas.set_zoom(self.timeline.canvas.pps / 1.5),
                                  "-", icon_name="zoom_out")
        A["zoom_fit"] = self._act(m, "Zoom to Fit", self.timeline.canvas.zoom_fit, "\\", icon_name="zoom_fit")
        self._act(m, "Zoom to Selection", self.zoom_selection, "Shift+Z",
                  tip="Fill the timeline with the selected clips")
        m.addSeparator()
        self._act(m, "Taller Tracks", lambda: self.timeline.canvas.scale_tracks(1.25), "Alt+=",
                  tip="Bigger waveforms and thumbnails (also Alt+wheel, or drag a track's lower edge)")
        self._act(m, "Shorter Tracks", lambda: self.timeline.canvas.scale_tracks(1 / 1.25), "Alt+-")
        self._act(m, "Reset Track Heights", self.timeline.canvas.reset_track_heights)

        m = mb.addMenu("&Graphics")
        self._act(m, "New Title", lambda: self.add_title(), icon_name="title")
        sub = m.addMenu(icons.icon("title"), "New from Template")
        sub.aboutToShow.connect(lambda sub=sub: self._fill_template_menu(sub))
        self._act(m, "Save Selected Title as Template…", self.save_template)
        m.addSeparator()
        self._act(m, "Graphic Sweep Transition at Playhead", lambda: self.apply_transition_at_playhead("graphic_sweep"))
        self._act(m, "Graphic Bars Transition at Playhead", lambda: self.apply_transition_at_playhead("graphic_bars"))
        self._act(m, "Show Graphics Panel", lambda: self.left_tabs.setCurrentWidget(self.graphics))

        m = mb.addMenu("&Clip")
        A["transition"] = self._act(m, "Apply Default Transition", lambda: self.apply_transition_at_playhead(),
                                    "Ctrl+D", icon_name="transition",
                                    tip="Cross dissolve (video) / constant power (audio) at the edit nearest "
                                        "the playhead")
        self._act(m, "Remove Transitions from Selection", self.remove_transitions)
        m.addSeparator()
        A["silence"] = self._act(m, "Detect Silences…", self.detect_silences, "Ctrl+Shift+D", icon_name="silence",
                                 tip="Find pauses and rambling dead air; cut, speed up or mark them")
        A["cut_marks"] = self._act(m, "Cut Marked Silences", self.cut_marked_silences, icon_name="marker")
        self._act(m, "Clear Silence Marks", self.clear_marks)
        m.addSeparator()
        A["denoise"] = self._act(m, "Noise Reduction…", self.noise_reduction_dialog, "Ctrl+Shift+N",
                                 icon_name="denoise", tip="Remove hiss, hum and room noise from audio clips")

        # main toolbar
        tb = self.addToolBar("Main")
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        for group in (("new", "open", "save"), ("import", "export"), ("undo", "redo"),
                      ("split", "delete", "ripple_delete", "title", "track_add"),
                      ("transition", "silence", "denoise"), ("snap", "linked"),
                      ("zoom_out", "zoom_in", "zoom_fit")):
            for name in group:
                tb.addAction(A[name])
            tb.addSeparator()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addWidget(self.seq_label)

        m = mb.addMenu("&Playback")
        self._act(m, "Play / Pause", self.toggle_play, "Space")
        self._act(m, "Previous Frame", lambda: self.step(-1), "Left")
        self._act(m, "Next Frame", lambda: self.step(1), "Right")
        self._act(m, "Back 1 Second", lambda: self.step(-round(self.project.fps)), "Shift+Left")
        self._act(m, "Forward 1 Second", lambda: self.step(round(self.project.fps)), "Shift+Right")
        self._act(m, "Previous Edit Point", lambda: self.jump_edit(-1), "Up")
        self._act(m, "Next Edit Point", lambda: self.jump_edit(1), "Down")
        self._act(m, "Go to Start", lambda: self.set_playhead(0), "Home")
        self._act(m, "Go to End", lambda: self.set_playhead(self.project.duration()), "End")

        m = mb.addMenu("&Help")
        self._act(m, "Keyboard Shortcuts", self.show_help, "F1")
        m.addSeparator()
        self._act(m, "Support Super8…", lambda: AboutDialog(self, focus_support=True).exec())
        self._act(m, "About Super8", lambda: AboutDialog(self).exec())
        self.set_tool("select")

    # ------------------------------------------------------------- state plumbing
    def changed(self, rebuild_bin: bool = False):
        """Call after any change to self.project (already pushed to undo by the caller)."""
        self.rev += 1
        c = self.timeline.canvas
        c.selection = {cid for cid in c.selection if self.project.find_clip(cid)[1] is not None}
        if rebuild_bin:
            self.bin.rebuild(self.project, self.thumbs)
        self.monitor.aspect = self.project.width / self.project.height
        self.monitor.seq_size = (self.project.width, self.project.height)
        self.seq_label.setText(f"Sequence: {self.project.width}×{self.project.height} @ {self.project.fps:g} fps")
        self.dur_label.setText(fmt_tc(self.project.duration(), self.project.fps))
        self._refresh_inspector()
        self.timeline.sync_bar()
        c.update()
        self.monitor.update()
        self.request_frame()

    def commit(self, before: dict):
        self.history.push(before)
        self._prop_key = None
        self._set_modified()
        self.changed()

    def preview_dirty(self):
        self.rev += 1
        self.request_frame()

    def _set_modified(self, v: bool = True):
        self.modified = v
        self._refresh_title()

    def _refresh_title(self):
        name = os.path.basename(self.project_path) if self.project_path else "Untitled"
        self.setWindowTitle(f"{'• ' if self.modified else ''}{name} — {APP}")

    def render_project(self) -> Project:
        if self._clone_rev != self.rev:
            self._clone = self.project.clone()
            self._clone_rev = self.rev
        return self._clone

    def request_frame(self):
        w, h = self.monitor.render_size()
        self.worker.request(self.render_project(), self.playhead, w, h)
        if not self.playing:
            self.update_monitor_overlay()

    def _refresh_inspector(self):
        self.update_monitor_overlay()
        c = self.timeline.canvas
        clip = None
        if c.selection:
            clips = [self.project.find_clip(cid)[1] for cid in c.selection]
            clips = [x for x in clips if x is not None]
            clips.sort(key=lambda x: (x.kind == "audio", x.start))
            clip = clips[0] if clips else None
        if clip is not self.inspector.clip or clip is None:
            self.inspector.set_clip(self.project, clip)
        else:
            self.inspector.set_clip(self.project, clip)

    def _on_thumb(self, mid: str, arr: np.ndarray):
        h, w = arr.shape[:2]
        arr = np.ascontiguousarray(arr)
        img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        self.thumbs[mid] = img
        self.bin.set_thumb(mid, img)
        self.timeline.canvas.update()

    # ------------------------------------------------------------- transport
    def set_playhead(self, t: float, follow: bool = True):
        if self.playing:
            self.stop_play()
        self._move_playhead(t, follow)

    def _move_playhead(self, t: float, follow: bool = True):
        self.playhead = max(0.0, t)
        c = self.timeline.canvas
        c.playhead = self.playhead
        if follow:
            c.ensure_visible(self.playhead)
        self.tc_label.setText(fmt_tc(self.playhead, self.project.fps))
        c.update()
        self.timeline.navigator.update()
        self.request_frame()

    def zoom_selection(self):
        if not self.timeline.canvas.zoom_selection():
            self.statusBar().showMessage("Select one or more clips to zoom to", 3000)

    def on_seek(self, t: float):
        self.set_playhead(t, follow=False)

    def step(self, frames: int):
        fps = self.project.fps
        self.set_playhead(round(self.playhead * fps + frames) / fps)

    def jump_edit(self, direction: int):
        pts = self.project.edit_points()
        eps = 0.5 / self.project.fps
        if direction > 0:
            nxt = [p for p in pts if p > self.playhead + eps]
        else:
            nxt = [p for p in pts if p < self.playhead - eps]
        if nxt:
            self.set_playhead(nxt[0] if direction > 0 else nxt[-1])

    def toggle_play(self):
        if self.playing:
            self.stop_play()
            return
        end = self.project.duration()
        if end <= 0:
            return
        if self.playhead >= end - 1e-3:
            self._move_playhead(0.0)
        self.playing = True
        self.play_origin = self.playhead
        self.clock.start()
        self.play_timer.start()
        self.player.start(self.render_project(), self.playhead)
        self.play_btn.setText("⏸")
        self.monitor.set_overlay(None)

    def stop_play(self):
        self.playing = False
        self.play_timer.stop()
        self.player.stop()
        self.play_btn.setText("▶")
        # land exactly on a frame
        self._move_playhead(self.project.frame(self.playhead), follow=False)

    def _tick(self):
        t = self.play_origin + self.clock.elapsed() / 1000.0
        end = self.project.duration()
        if t >= end:
            self._move_playhead(end)
            self.stop_play()
            return
        self._move_playhead(t)
        self.timeline.sync_bar()

    # ------------------------------------------------------------- tools
    def set_tool(self, tool: str):
        c = self.timeline.canvas
        c.tool = tool
        self.tool_actions[tool].setChecked(True)
        label, key, icon_name, hint = TOOLS[tool]
        self.tool_hint.setText(f"<b>{label}</b> — {hint}")
        c.setCursor(icons.cursor(tool) if tool != "select" else Qt.CursorShape.ArrowCursor)
        self.monitor.setCursor(icons.cursor("type") if tool == "type" else Qt.CursorShape.ArrowCursor)

    def _set_snap(self, on: bool):
        self.timeline.canvas.snap = on

    def _set_linked(self, on: bool):
        self.timeline.canvas.linked_selection = on

    # ------------------------------------------------------------- media
    def import_dialog(self):
        start = self.settings.value("last_dir", os.path.expanduser("~"))
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Media", start, MEDIA_FILTER)
        if paths:
            self.settings.setValue("last_dir", os.path.dirname(paths[0]))
            self.import_files(paths)

    def import_files(self, paths: list[str]):
        errors, added = [], 0
        known = {m.path for m in self.project.media.values()}
        before = self.project.to_dict()
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p) or p in known:
                continue
            try:
                m = probe(p)
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
                continue
            self.project.media[m.id] = m
            self.loader.enqueue(m)
            added += 1
        if added:
            self.commit(before)
            self.bin.rebuild(self.project, self.thumbs)
            self.statusBar().showMessage(f"Imported {added} file(s)", 4000)
        if errors:
            QMessageBox.warning(self, "Import", "Some files could not be imported:\n\n" + "\n".join(errors))

    def remove_media(self, ids: list[str]):
        if not ids:
            return
        used = [c for _, c in self.project.all_clips() if c.media_id in ids]
        if used and QMessageBox.question(
                self, "Remove media", f"{len(used)} clip(s) on the timeline use this media. Remove them too?"
        ) != QMessageBox.StandardButton.Yes:
            return
        before = self.project.to_dict()
        remove_clips(self.project, {c.id for c in used})
        for mid in ids:
            self.project.media.pop(mid, None)
        self.commit(before)
        self.bin.rebuild(self.project, self.thumbs)

    def drop_target(self, m, tr, kind: str):
        lst = self.project.tracks(kind)
        if not lst:
            return None
        if tr is None:
            return lst[0]
        if tr.kind == kind:
            return tr
        other = self.project.tracks(tr.kind)
        return lst[min(other.index(tr), len(lst) - 1)]

    def place_media(self, mid: str, t: float, tr):
        m = self.project.media.get(mid)
        if m is None:
            return
        before = self.project.to_dict()
        if not any(True for _ in self.project.all_clips()) and m.has_video and m.kind == "video":
            # first clip in an empty sequence: match its settings (like Premiere does)
            w, h = m.width // 2 * 2, m.height // 2 * 2
            fps = min(FPS_CHOICES, key=lambda f: abs(f - m.fps)) if m.fps else self.project.fps
            if (w, h, fps) != (self.project.width, self.project.height, self.project.fps) and w and h:
                self.project.width, self.project.height, self.project.fps = w, h, fps
                self.statusBar().showMessage(f"Sequence settings matched to clip: {w}×{h} @ {fps:g} fps", 6000)
        t = self.project.frame(t)
        fps = self.project.fps
        dur = max(1 / fps, int(m.duration * fps + 1e-6) / fps)  # whole frames only
        link = new_id() if (m.has_video and m.has_audio) else None
        ids = set()
        for kind, ok in (("video", m.has_video), ("audio", m.has_audio)):
            if not ok:
                continue
            dest = self.drop_target(m, tr, kind)
            if dest is None:
                continue
            ck = kind if kind == "audio" else ("image" if m.kind == "image" else "video")
            c = Clip(new_id(), ck, m.id, t, dur, link=link)
            carve(dest, c.start, c.end)
            dest.clips.append(c)
            dest.sort()
            ids.add(c.id)
        self.timeline.canvas.selection = ids
        self.commit(before)

    # ------------------------------------------------------------- edits
    def selected_clips(self) -> list[Clip]:
        out = []
        for cid in self.timeline.canvas.selection:
            _, c = self.project.find_clip(cid)
            if c:
                out.append(c)
        return out

    def delete_selected(self, ripple: bool):
        ids = set(self.timeline.canvas.selection)
        if not ids:
            return
        before = self.project.to_dict()
        remove_clips(self.project, ids, ripple)
        self.timeline.canvas.selection.clear()
        self.commit(before)

    def split_at_playhead(self):
        t = self.project.frame(self.playhead)
        sel = self.timeline.canvas.selection
        targets = []
        for tr in self.project.tracks():
            if tr.muted:
                continue
            c = tr.clip_at(t)
            if c is not None and (not sel or c.id in sel):
                targets.append(c)
        if not targets:
            return
        before = self.project.to_dict()
        done, seen = False, set()
        for c in targets:
            if c.id in seen:
                continue
            seen.update(x.id for x in self.project.linked(c))
            done |= split_with_links(self.project, c, t)
        if done:
            self.commit(before)

    def select_all(self):
        self.timeline.canvas.selection = {c.id for _, c in self.project.all_clips()}
        self.changed()

    def deselect(self):
        self.timeline.canvas.selection.clear()
        self.changed()

    def toggle_link(self):
        clips = self.selected_clips()
        if not clips:
            return
        before = self.project.to_dict()
        if any(c.link for c in clips):
            for c in clips:
                c.link = None
        else:
            lid = new_id()
            for c in clips:
                c.link = lid
        self.commit(before)

    def add_title(self, t: float | None = None, track=None, pos: tuple[float, float] | None = None,
                  preset: str = "basic"):
        """Add a graphic from a template. Without a track, use the lowest free video track above content."""
        t = self.project.frame(self.playhead if t is None else t)
        dur = preset_duration(preset)
        before = self.project.to_dict()
        dest = track
        if dest is None:
            for tr in reversed(self.project.video_tracks):
                if all(c.end <= t or c.start >= t + dur for c in tr.clips):
                    dest = tr
                else:
                    break
            if dest is None:
                dest = self.project.add_track("video")
        c = Clip(new_id(), "title", None, t, dur)
        apply_preset(c, preset, self.project.width, self.project.height)
        if pos is not None:
            c.pos_x, c.pos_y = pos
        carve(dest, c.start, c.end)
        dest.clips.append(c)
        dest.sort()
        self.timeline.canvas.selection = {c.id}
        if not (c.start <= self.playhead < c.end):
            self._move_playhead(c.start + min(c.duration / 2, 1.5))
        self.commit(before)
        self.inspector.widgets["text"].setFocus()
        self.inspector.widgets["text"].selectAll()

    def _fill_template_menu(self, menu):
        menu.clear()
        for pid, preset in all_presets().items():
            menu.addAction(preset.get("name", pid), lambda pid=pid: self.add_title(preset=pid))

    # ------------------------------------------------------------- graphics templates
    def _load_templates(self):
        try:
            data = json.loads(self.settings.value("graphic_templates", "{}") or "{}")
        except (TypeError, ValueError):
            data = {}
        USER_PRESETS.clear()
        USER_PRESETS.update({k: v for k, v in data.items() if k.startswith("user:")})

    def _store_templates(self):
        self.settings.setValue("graphic_templates", json.dumps(USER_PRESETS))

    def save_template(self):
        clip = self.inspector.clip
        if clip is None or clip.kind != "title":
            return
        default = (clip.text.splitlines() or ["My Title"])[0][:30]
        name, ok = QInputDialog.getText(self, "Save Graphics Template", "Template name:", text=default)
        if not ok or not name.strip():
            return
        pid = "user:" + name.strip()
        USER_PRESETS[pid] = template_from_clip(clip, name.strip(), self.project.width, self.project.height)
        self._store_templates()
        self.graphics.forget_icon(pid)
        self.graphics.rebuild()
        self.left_tabs.setCurrentWidget(self.graphics)
        self.statusBar().showMessage(f"Saved template “{name.strip()}” to the Graphics panel", 5000)

    def delete_template(self, pid: str):
        if pid in USER_PRESETS:
            del USER_PRESETS[pid]
            self._store_templates()
            self.graphics.forget_icon(pid)
            self.graphics.rebuild()

    def _add_track(self, kind: str):
        before = self.project.to_dict()
        self.project.add_track(kind)
        self.commit(before)

    # ------------------------------------------------------------- transitions
    def apply_transition(self, clip_id: str, side: str, kind: str, only_track: bool = False,
                         duration: float | None = None, commit: bool = True, before=None) -> bool:
        tr, clip = self.project.find_clip(clip_id)
        if clip is None:
            return False
        before = before or self.project.to_dict()
        dur = duration or 1.0
        targets = [(tr, clip)]
        if not only_track and self.timeline.canvas.linked_selection:
            for other in self.project.linked(clip):
                if other is clip:
                    continue
                otr, _ = self.project.find_clip(other.id)
                same_edge = abs(other.start - clip.start) < 1e-3 if side == "in" else abs(other.end - clip.end) < 1e-3
                if same_edge:
                    targets.append((otr, other))
        for t, c in targets:
            k = for_track(kind, t.kind)
            if side == "in":
                c.tin, c.tin_dur = k, dur
            else:
                c.tout, c.tout_dur = k, dur
        if commit:
            self.commit(before)
            self.statusBar().showMessage(f"Applied {short_label(for_track(kind, tr.kind))}", 3000)
        return True

    def apply_transition_at_playhead(self, kind: str | None = None):
        """Ctrl+D: transition at the edit point nearest the playhead (selected clips only, if any)."""
        pts = self.project.edit_points()
        if not pts:
            return
        t = min(pts, key=lambda p: abs(p - self.playhead))
        sel = self.timeline.canvas.selection
        before = self.project.to_dict()
        done = 0
        for tr in self.project.tracks():
            if tr.muted:
                continue
            k = for_track(kind or DEFAULT_VIDEO, tr.kind)
            starting = next((c for c in tr.clips if abs(c.start - t) < 1e-3), None)
            ending = next((c for c in tr.clips if abs(c.end - t) < 1e-3), None)
            if starting is not None and (not sel or starting.id in sel or (ending and ending.id in sel)):
                starting.tin, starting.tin_dur = k, 1.0
                done += 1
            elif ending is not None and (not sel or ending.id in sel):
                ending.tout, ending.tout_dur = k, 1.0
                done += 1
        if done:
            self.commit(before)
            self.statusBar().showMessage(f"Transition applied on {done} track(s) at {fmt_tc(t, self.project.fps)}",
                                         3000)
        else:
            self.statusBar().showMessage("No clip edge at the playhead", 3000)

    def remove_transitions(self, clips=None):
        clips = clips if clips is not None else self.selected_clips()
        if not clips:
            return
        before = self.project.to_dict()
        for c in clips:
            c.tin = c.tout = ""
        self.commit(before)

    # ------------------------------------------------------------- silences
    def _audio_missing(self) -> list[str]:
        return [m.name for m in self.project.media.values()
                if m.has_audio and m.id not in self.audio_cache.samples and os.path.exists(m.path)]

    def detect_silences(self):
        if self.project.duration() <= 0 or not any(c for t in self.project.audio_tracks for c in t.clips):
            QMessageBox.information(self, "Detect Silences", "Put some clips with audio on the timeline first.")
            return
        missing = self._audio_missing()
        if missing:
            QMessageBox.information(self, "Detect Silences", "Audio is still being decoded for:\n"
                                    + "\n".join(missing) + "\n\nTry again in a moment.")
            return
        self.stop_play()
        sel = self.selected_clips()
        if sel:
            t0, t1 = min(c.start for c in sel), max(c.end for c in sel)
            scope = f"the selected clips ({len(sel)})"
        else:
            t0, t1 = 0.0, self.project.duration()
            scope = "the whole sequence"
        proj = self.project.clone()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            db = envelope_db(lambda t, n: mix_audio(proj, self.audio_cache, t, n), t0, t1, proj.sample_rate)
        finally:
            QApplication.restoreOverrideCursor()
        dlg = SilenceDialog(self, db, t0, t1, self.project.fps, scope)
        if not dlg.exec() or not dlg.ranges:
            return
        if dlg.action == "mark":
            self.timeline.canvas.markers = list(dlg.ranges)
            self.timeline.canvas.update()
            self.statusBar().showMessage(f"Marked {len(dlg.ranges)} silences — review them, then "
                                         "Clip ▸ Cut Marked Silences", 8000)
            return
        self._ripple_silences(dlg.ranges, dlg.action)

    def _ripple_silences(self, ranges, action: str):
        before = self.project.to_dict()
        mode, factor = ("cut", 1.0) if action == "cut" else ("speed", 4.0 if action == "speed4" else 8.0)
        saved = ripple_ranges(self.project, ranges, mode, factor)
        self.timeline.canvas.markers = []
        self.timeline.canvas.selection.clear()
        self.commit(before)
        verb = "Removed" if mode == "cut" else f"Sped up ×{factor:g}"
        self.statusBar().showMessage(f"{verb} {len(ranges)} silences — sequence is {saved:.1f} s shorter", 8000)

    def cut_marked_silences(self):
        if not self.timeline.canvas.markers:
            self.statusBar().showMessage("No silence marks. Use Detect Silences… with 'Only mark' first.", 4000)
            return
        self._ripple_silences(self.timeline.canvas.markers, "cut")

    def clear_marks(self):
        self.timeline.canvas.markers = []
        self.timeline.canvas.update()

    # ------------------------------------------------------------- noise reduction
    def noise_reduction_dialog(self):
        clips = [c for c in self.selected_clips() if c.kind == "audio"]
        if not clips:
            clips = [c for t in self.project.audio_tracks for c in t.clips]
            if not clips:
                QMessageBox.information(self, "Noise Reduction", "There are no audio clips on the timeline.")
                return
        dlg = NoiseDialog(self, len(clips), max(c.denoise for c in clips))
        if not dlg.exec():
            return
        self.set_denoise(clips, dlg.strength)

    def set_denoise(self, clips, strength: float):
        before = self.project.to_dict()
        for c in clips:
            c.denoise = strength
            self.audio_cache.request(c.media_id, strength)
        self.commit(before)
        if strength > 0:
            self.statusBar().showMessage("Noise reduction: processing in the background…")

    def _on_variant_progress(self, mid: str, frac: float):
        m = self.project.media.get(mid)
        self.statusBar().showMessage(f"Noise reduction: {m.name if m else ''} {frac * 100:.0f}%")

    def _on_variant_ready(self, mid: str, strength: float):
        m = self.project.media.get(mid)
        self.statusBar().showMessage(f"Noise reduction ready: {m.name if m else mid} ({strength * 100:.0f}%)", 5000)

    # ------------------------------------------------------------- context menu
    def on_context_menu(self, clip, gpos, t: float):
        menu = QMenu(self)
        if clip is not None:
            tr, _ = self.project.find_clip(clip.id)
            catalogue = AUDIO_TRANSITIONS if tr.kind == "audio" else VIDEO_TRANSITIONS
            nxt = next((c for c in tr.clips if c is not clip and abs(c.start - clip.end) < 1e-3), None)
            for title, target, side in (("Transition at Start", clip, "in"),
                                        ("Transition at End", nxt or clip, "in" if nxt else "out")):
                sub = menu.addMenu(icons.icon("transition"), title)
                for tid, label in catalogue.items():
                    sub.addAction(label, lambda tid=tid, cid=target.id, side=side:
                                  self.apply_transition(cid, side, tid))
            menu.addAction("Remove Transitions", lambda: self.remove_transitions())
            if tr.kind == "audio":
                sub = menu.addMenu(icons.icon("denoise"), "Noise Reduction")
                audio_sel = [c for c in self.selected_clips() if c.kind == "audio"] or [clip]
                for name, v in (("Off", 0.0), ("Light", 0.3), ("Medium", 0.6), ("Strong", 0.85)):
                    a = sub.addAction(name, lambda v=v: self.set_denoise(audio_sel, v))
                    a.setCheckable(True)
                    a.setChecked(abs(clip.denoise - v) < 0.01)
            menu.addSeparator()
            menu.addAction(icons.icon("split"), "Split at Playhead", self.split_at_playhead)
            menu.addAction(icons.icon("delete"), "Delete", lambda: self.delete_selected(False))
            menu.addAction(icons.icon("ripple_delete"), "Ripple Delete", lambda: self.delete_selected(True))
            menu.addAction(icons.icon("link"), "Link / Unlink", self.toggle_link)
            menu.addSeparator()
        menu.addAction(icons.icon("silence"), "Detect Silences…", self.detect_silences)
        if self.timeline.canvas.markers:
            menu.addAction(icons.icon("marker"), "Cut Marked Silences", self.cut_marked_silences)
            menu.addAction("Clear Silence Marks", self.clear_marks)
        menu.exec(gpos)

    def on_property(self, field: str, value):
        clip = self.inspector.clip
        if clip is None:
            return
        if field == "speed":
            self._set_speed(clip, value)
            return
        key = (clip.id, field)
        if self._prop_key != key:  # coalesce continuous tweaks of one property into one undo step
            self.history.push(self.project.to_dict())
            self._set_modified()
        self._prop_key = key
        setattr(clip, field, value)
        if field == "denoise" and clip.media_id:
            self.audio_cache.request(clip.media_id, value)
        if field in ("tin", "tout"):
            self.inspector.set_clip(self.project, clip)  # show/hide the duration row
        self.rev += 1
        self.timeline.canvas.update()
        self.update_monitor_overlay()
        self.request_frame()

    def _set_speed(self, clip: Clip, speed: float):
        """Change speed keeping the same source range: duration scales; overwrites what it grows into."""
        speed = max(0.0625, min(16.0, speed))
        before = self.project.to_dict()
        group = self.project.linked(clip) if self.timeline.canvas.linked_selection else [clip]
        for c in group:
            new_d = max(1 / self.project.fps, self.project.frame(c.duration * c.speed / speed))
            c.duration, c.speed = new_d, speed
            c.fade_in, c.fade_out = min(c.fade_in, new_d), min(c.fade_out, new_d)
            tr, _ = self.project.find_clip(c.id)
            carve(tr, c.start, c.end, exclude={x.id for x in group})
        self.commit(before)

    # ------------------------------------------------------------- program monitor interaction
    def visual_clip_at_playhead(self, selected_only: bool):
        """Topmost visible visual clip under the playhead (optionally restricted to the selection)."""
        sel = self.timeline.canvas.selection
        for tr in reversed(self.project.video_tracks):
            if tr.muted:
                continue
            c = tr.clip_at(self.playhead)
            if c is not None and (not selected_only or c.id in sel):
                return c
        return None

    def clip_frame_rect(self, c: Clip) -> QRectF | None:
        """Where a visual clip lands, in sequence pixels."""
        P = self.project
        cx, cy = P.width / 2 + c.pos_x, P.height / 2 + c.pos_y
        if c.kind == "title":
            return block_rect(c, P.width, P.height, self.playhead - c.start)
        m = P.media.get(c.media_id)
        if m is None or not m.width or not m.height:
            return None
        fit = min(P.width / m.width, P.height / m.height) * c.scale
        w, h = m.width * fit, m.height * fit
        return QRectF(cx - w / 2, cy - h / 2, w, h)

    def update_monitor_overlay(self):
        c = self.visual_clip_at_playhead(selected_only=True)
        self.monitor.set_overlay(None if (c is None or self.playing) else self.clip_frame_rect(c))

    def on_monitor_press(self, pt: QPointF, mods):
        if self.timeline.canvas.tool == "type":
            self.add_title(pos=(pt.x() - self.project.width / 2, pt.y() - self.project.height / 2))
            return
        clip = self.visual_clip_at_playhead(selected_only=True)
        if clip is None or not (self.clip_frame_rect(clip) or QRectF()).contains(pt):
            # click selects the topmost clip under the pointer
            clip = None
            for tr in reversed(self.project.video_tracks):
                c = tr.clip_at(self.playhead)
                if c is not None and not tr.muted and (self.clip_frame_rect(c) or QRectF()).contains(pt):
                    clip = c
                    break
            canvas = self.timeline.canvas
            canvas.selection = canvas.group_of(clip, mods) if clip else set()
            canvas.update()
            self._refresh_inspector()
        if clip is not None:
            self._mon_drag = {"id": clip.id, "pt": pt, "pos": (clip.pos_x, clip.pos_y),
                              "before": self.project.to_dict(), "changed": False}
        self.update_monitor_overlay()

    def on_monitor_drag(self, pt: QPointF, mods):
        d = getattr(self, "_mon_drag", None)
        if not d:
            return
        _, c = self.project.find_clip(d["id"])
        if c is None:
            return
        dx, dy = pt.x() - d["pt"].x(), pt.y() - d["pt"].y()
        if mods & Qt.KeyboardModifier.ShiftModifier:  # constrain to one axis
            if abs(dx) > abs(dy):
                dy = 0
            else:
                dx = 0
        c.pos_x, c.pos_y = round(d["pos"][0] + dx), round(d["pos"][1] + dy)
        d["changed"] = True
        self.preview_dirty()
        self.update_monitor_overlay()
        self.inspector.set_clip(self.project, c)

    def on_monitor_release(self):
        d, self._mon_drag = getattr(self, "_mon_drag", None), None
        if d and d["changed"]:
            self.commit(d["before"])

    def on_monitor_wheel(self, steps: float):
        c = self.visual_clip_at_playhead(selected_only=True)
        if c is None:
            return
        self.inspector.clip = c
        self.on_property("scale", max(0.01, min(10.0, c.scale * (1.05 ** steps))))
        self.inspector.set_clip(self.project, c)

    def undo(self):
        state = self.history.undo(self.project.to_dict())
        if state is not None:
            self._restore(state)

    def redo(self):
        state = self.history.redo(self.project.to_dict())
        if state is not None:
            self._restore(state)

    def _restore(self, state: dict):
        media_before = set(self.project.media)
        self.project = Project.from_dict(state)
        self._prop_key = None
        self._set_modified()
        for mid, m in self.project.media.items():
            if mid not in media_before and mid not in self.audio_cache.samples:
                self.loader.enqueue(m)
        self.changed(rebuild_bin=set(self.project.media) != media_before)
        self.inspector.set_clip(self.project, None)
        self._refresh_inspector()

    def sequence_dialog(self):
        d = SequenceDialog(self, self.project)
        if d.exec():
            before = self.project.to_dict()
            self.project.width = d.w.value() // 2 * 2
            self.project.height = d.h.value() // 2 * 2
            self.project.fps = d.fps.currentData()
            self.commit(before)

    # ------------------------------------------------------------- files
    def maybe_save(self) -> bool:
        if not self.modified:
            return True
        r = QMessageBox.question(self, APP, "Save changes to the current project?",
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Save:
            return self.save()
        return r == QMessageBox.StandardButton.Discard

    def new_project(self):
        if not self.maybe_save():
            return
        self._load_project(Project.new(), None)

    def open_dialog(self):
        if not self.maybe_save():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Open Project", self.settings.value("last_dir", ""), PROJECT_FILTER)
        if p:
            self.open_path(p)

    def open_path(self, path: str):
        try:
            with open(path) as f:
                proj = Project.from_dict(json.load(f))
        except Exception as e:
            QMessageBox.critical(self, APP, f"Could not open project:\n{e}")
            return
        base = os.path.dirname(os.path.abspath(path))
        for m in proj.media.values():  # allow projects moved together with their media
            if not os.path.exists(m.path):
                alt = os.path.join(base, os.path.basename(m.path))
                if os.path.exists(alt):
                    m.path = alt
        self._load_project(proj, path)

    def _load_project(self, proj: Project, path: str | None):
        self.stop_play()
        self.project = proj
        self.project_path = path
        self.history = History()
        self.timeline.canvas.selection.clear()
        for m in proj.media.values():
            if os.path.exists(m.path):
                self.loader.enqueue(m)
        self._set_modified(False)
        self.playhead = 0.0
        self.changed(rebuild_bin=True)
        self._move_playhead(0.0)
        self.timeline.canvas.zoom_fit()
        missing = [m.name for m in proj.media.values() if not os.path.exists(m.path)]
        if missing:
            QMessageBox.warning(self, APP, "Offline media (file not found):\n\n" + "\n".join(missing))

    def save(self) -> bool:
        if not self.project_path:
            return self.save_as()
        try:
            tmp = self.project_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.project.to_dict(), f, indent=1)
            os.replace(tmp, self.project_path)
        except Exception as e:
            QMessageBox.critical(self, APP, f"Could not save:\n{e}")
            return False
        self._set_modified(False)
        self.statusBar().showMessage(f"Saved {self.project_path}", 3000)
        return True

    def save_as(self) -> bool:
        p, _ = QFileDialog.getSaveFileName(self, "Save Project", self.project_path or "untitled.s8proj",
                                           PROJECT_FILTER)
        if not p:
            return False
        if not p.endswith(".s8proj"):
            p += ".s8proj"
        self.project_path = p
        return self.save()

    # ------------------------------------------------------------- export
    def export_dialog(self):
        if self.project.duration() <= 0:
            QMessageBox.information(self, "Export", "The timeline is empty.")
            return
        self.stop_play()
        base = os.path.splitext(self.project_path)[0] if self.project_path else os.path.join(
            self.settings.value("last_dir", os.path.expanduser("~")), "export")
        d = ExportDialog(self, self.project, base + ".mp4")
        if not d.exec():
            return
        path = d.path.text().strip()
        if not path:
            return
        if os.path.exists(path) and QMessageBox.question(self, "Export", f"Overwrite {path}?") \
                != QMessageBox.StandardButton.Yes:
            return
        missing_audio = [m.name for m in self.project.media.values()
                         if m.has_audio and m.id not in self.audio_cache.samples and os.path.exists(m.path)]
        if missing_audio:
            QMessageBox.information(self, "Export", "Audio is still being decoded for:\n" + "\n".join(missing_audio)
                                    + "\n\nWait a moment and try again.")
            return
        proj = self.project.clone()
        cancel = threading.Event()
        dlg = QProgressDialog("Exporting…", "Cancel", 0, 1000, self)
        dlg.setWindowTitle("Export")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(cancel.set)
        clock = QElapsedTimer()
        clock.start()

        def on_progress(done, total):
            dlg.setValue(int(1000 * done / total))
            el = clock.elapsed() / 1000
            eta = el / done * (total - done) if done else 0
            dlg.setLabelText(f"Exporting frame {done}/{total} · {el:.0f}s elapsed · ~{eta:.0f}s left")

        def on_done(ok, msg):
            self.bridge.export_progress.disconnect(on_progress)
            self.bridge.export_done.disconnect(on_done)
            dlg.reset()
            if msg:
                QMessageBox.critical(self, "Export failed", msg)
            elif ok:
                self.statusBar().showMessage(f"Exported {path}", 8000)
                QMessageBox.information(self, "Export", f"Export finished:\n{path}")
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass

        self.bridge.export_progress.connect(on_progress)
        self.bridge.export_done.connect(on_done)

        def job():
            try:
                ok = export_project(proj, self.audio_cache, path, crf=d.quality.currentData(),
                                    preset=d.speed.currentText(), progress=self.bridge.export_progress.emit,
                                    cancel=cancel)
                self.bridge.export_done.emit(ok, "")
            except Exception as e:
                traceback.print_exc()
                self.bridge.export_done.emit(False, str(e))

        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------- misc
    def show_help(self):
        QMessageBox.information(self, "Keyboard Shortcuts", """
<b>Playback</b><br>Space play/pause · ←/→ frame step · Shift+←/→ 1 second · ↑/↓ previous/next edit · Home/End<br><br>
<b>Tools</b><br>V selection · A track select forward · B ripple edit · N rolling edit · R rate stretch · C razor (Shift: all tracks) · Y slip · U slide · H hand · Z zoom (Alt: out) · T type<br>S snapping · Shift+L linked selection · Program monitor: drag to move the selected clip, wheel to scale, Shift+drag locks an axis<br><br>
<b>Editing</b><br>Ctrl+K split at playhead · Delete remove · Shift+Delete ripple delete ·
Ctrl+L link/unlink · Ctrl+T add title · Ctrl+Z / Ctrl+Shift+Z undo/redo<br>
Drag clip edges to trim · Alt+drag edits one side of a linked clip · Shift+click multi-select<br><br>
<b>Transitions & audio clean-up</b><br>Effects tab: drag a transition onto a cut (Shift: this track only) ·
Ctrl+D default transition at the playhead · right-click a clip for transitions / noise reduction ·
Ctrl+Shift+D detect silences (cut, speed up or mark) · Ctrl+Shift+N noise reduction<br><br>
<b>Graphics</b><br>Graphics tab: drag a template onto a video track (or double-click) · edit text, style and
animation in the Inspector · drag it in the Program Monitor to place it · Inspector ▸ Save as Graphics Template<br><br>
<b>Timeline view</b><br>Wheel scroll · Ctrl+wheel zoom · Shift+wheel scroll tracks · Middle-drag pan ·
= / - zoom (around the playhead) · \\ zoom to fit · Shift+Z zoom to selection ·
Zoom tool: drag a range to fill the view · zoom bar: drag the box to pan, its edges to zoom ·
Alt+wheel or Alt+= / Alt+- track height (drag a track's lower edge in the header for one track)<br><br>
<b>Media</b><br>Ctrl+I import · drag from bin to timeline · double-click media to insert at playhead ·
Ctrl+M export
""")

    def closeEvent(self, e):
        if not self.maybe_save():
            e.ignore()
            return
        self.stop_play()
        self.shutdown()
        e.accept()

    # cross-thread results arrive through bound methods (never lambdas) so PyQt drops them once the
    # window is gone instead of calling into deleted widgets
    def _on_frame(self, img):
        self.monitor.set_image(img)

    def _on_filmstrip(self, mid: str, strip):
        times, frames = strip
        images = []
        for arr in frames:
            h, w = arr.shape[:2]
            arr = np.ascontiguousarray(arr)
            images.append(QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy())
        self.filmstrips[mid] = (times, images)
        self.timeline.canvas.update()

    def _on_audio_loaded(self, mid: str):
        self.timeline.canvas.update()

    def _on_media_error(self, mid: str, msg: str):
        self.statusBar().showMessage(f"Could not decode media: {msg}", 8000)

    def shutdown(self):
        """Stop every background producer before this window's QObjects go away."""
        self.bridge.blockSignals(True)
        self.worker.frame_ready.disconnect(self._on_frame)
        self.audio_cache.on_variant = None
        self.audio_cache.on_variant_progress = None
        self.loader.stop()
        self.worker.stop()


def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    pal = QPalette()
    c = {
        QPalette.ColorRole.Window: "#24262b", QPalette.ColorRole.WindowText: "#d9dce3",
        QPalette.ColorRole.Base: "#1b1d21", QPalette.ColorRole.AlternateBase: "#26292f",
        QPalette.ColorRole.ToolTipBase: "#2b2f37", QPalette.ColorRole.ToolTipText: "#e6e8ec",
        QPalette.ColorRole.Text: "#d9dce3", QPalette.ColorRole.Button: "#2e3138",
        QPalette.ColorRole.ButtonText: "#d9dce3", QPalette.ColorRole.Highlight: "#3d6fb6",
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.Link: "#6ea2ff",
        QPalette.ColorRole.PlaceholderText: "#6d737e",
    }
    for role, col in c.items():
        pal.setColor(role, QColor(col))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#6d737e"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#6d737e"))
    app.setPalette(pal)
    app.setStyleSheet("""
        QSplitter::handle { background: #15161a; }
        QToolBar { border: none; spacing: 4px; padding: 3px; }
        QToolButton { padding: 4px 8px; border-radius: 4px; }
        QToolButton:checked { background: #3d6fb6; }
        QToolButton:hover { background: #3a3f49; }
        QListWidget { border: none; }
        QWidget#toolsPanel { background: #1f2126; border-right: 1px solid #15161a; }
        QWidget#toolsPanel QToolButton { padding: 4px; }
        QToolBar#mainToolbar QToolButton { padding: 4px; }
        QToolBar#mainToolbar QToolButton:checked { background: #34507a; }
    """)


def desktop_file_id() -> str:
    """Wayland matches windows to launchers by desktop-file id; inside our snap that is "<snap>_super8".

    SNAP* variables leak into child processes of other snaps (e.g. a terminal inside the VS Code snap),
    so only trust them when this code is actually installed under $SNAP."""
    snap_dir = os.environ.get("SNAP", "")
    here = os.path.realpath(__file__)
    if snap_dir and here.startswith(os.path.realpath(snap_dir) + os.sep):
        return f"{os.environ.get('SNAP_INSTANCE_NAME') or os.environ.get('SNAP_NAME')}_super8"
    return "super8"


def run(argv) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    app.setDesktopFileName(desktop_file_id())
    apply_dark_theme(app)
    w = MainWindow()
    w.show()
    args = [a for a in argv[1:] if not a.startswith("-")]
    projects = [a for a in args if a.endswith(".s8proj")]
    if projects:
        w.open_path(projects[0])
    elif args:
        w.import_files(args)
    return app.exec()
