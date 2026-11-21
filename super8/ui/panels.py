"""Program monitor, media bin and inspector panels."""
from __future__ import annotations

import os
import threading
import traceback

from PyQt6.QtCore import QMimeData, QPointF, QRectF, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFontComboBox, QFormLayout, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QToolButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout,
                             QWidget)

from ..compositor import Compositor
from ..graphics import ALIGNS, ANIMATIONS, BACKGROUNDS, ROLLS
from ..transitions import AUDIO_TRANSITIONS, VIDEO_TRANSITIONS
from ..model import Clip, Project
from . import icons
from .timeline import GRAPHIC_MIME, MEDIA_MIME, TRANSITION_MIME, fmt_tc


# ================================================================ rendering
class RenderWorker(QThread):
    """Renders preview frames off the GUI thread. Only the newest request is honoured."""

    frame_ready = pyqtSignal(QImage)

    def __init__(self, audio_cache):
        super().__init__()
        self.comp = Compositor(audio_cache)
        self.cond = threading.Condition()
        self.req = None
        self.alive = True

    def request(self, project: Project, t: float, w: int, h: int):
        with self.cond:
            self.req = (project, t, w, h)
            self.cond.notify()

    def stop(self):
        with self.cond:
            self.alive = False
            self.cond.notify()
        self.wait(2000)

    def run(self):
        while True:
            with self.cond:
                while self.req is None and self.alive:
                    self.cond.wait()
                if not self.alive:
                    break
                project, t, w, h = self.req
                self.req = None
            try:
                arr = self.comp.render(project, t, w, h)
                img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
                self.frame_ready.emit(img)
            except Exception:
                traceback.print_exc()
        self.comp.close()


class Monitor(QWidget):
    """Program monitor. Mouse positions are reported in sequence pixel coordinates."""

    resized = pyqtSignal()
    pressed = pyqtSignal(QPointF, object)
    dragged = pyqtSignal(QPointF, object)
    released = pyqtSignal()
    wheeled = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.image: QImage | None = None
        self.aspect = 16 / 9
        self.seq_size = (1920, 1080)
        self.overlay: QRectF | None = None  # selected clip bounds, sequence coords
        self._down = False
        self.setMinimumSize(320, 180)

    def to_seq(self, pos) -> QPointF:
        r = self.frame_rect()
        k = self.seq_size[0] / max(1.0, r.width())
        return QPointF((pos.x() - r.left()) * k, (pos.y() - r.top()) * k)

    def set_overlay(self, rect: QRectF | None):
        if rect != self.overlay:
            self.overlay = rect
            self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.pressed.emit(self.to_seq(e.position()), e.modifiers())

    def mouseMoveEvent(self, e):
        if self._down:
            self.dragged.emit(self.to_seq(e.position()), e.modifiers())

    def mouseReleaseEvent(self, e):
        if self._down and e.button() == Qt.MouseButton.LeftButton:
            self._down = False
            self.released.emit()

    def wheelEvent(self, e):
        self.wheeled.emit(e.angleDelta().y() / 120)

    def frame_rect(self) -> QRectF:
        W, H = self.width(), self.height()
        w = min(W, H * self.aspect)
        h = w / self.aspect
        return QRectF((W - w) / 2, (H - h) / 2, w, h)

    def render_size(self) -> tuple[int, int]:
        r = self.frame_rect()
        dpr = self.devicePixelRatioF()
        w = int(r.width() * dpr) // 2 * 2
        h = int(r.height() * dpr) // 2 * 2
        return max(2, min(w, 1920)), max(2, min(h, 1080))

    def set_image(self, img: QImage):
        self.image = img
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.resized.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0d0e10"))
        r = self.frame_rect()
        p.fillRect(r, QColor("#000"))
        if self.image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(r, self.image)
        if self.overlay is not None:
            k = r.width() / self.seq_size[0]
            o = QRectF(r.left() + self.overlay.left() * k, r.top() + self.overlay.top() * k,
                       self.overlay.width() * k, self.overlay.height() * k)
            p.setClipRect(r)
            p.setPen(QPen(QColor("#4da3ff"), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(o)
            p.setBrush(QColor("#ffffff"))
            for hx in (o.left(), o.center().x(), o.right()):
                for hy in (o.top(), o.center().y(), o.bottom()):
                    if (hx, hy) != (o.center().x(), o.center().y()):
                        p.drawRect(QRectF(hx - 3, hy - 3, 6, 6))
            p.drawEllipse(o.center(), 3.5, 3.5)
        p.end()


# ================================================================ media bin
class MediaList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(120, 68))
        self.setGridSize(QSize(140, 116))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWordWrap(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)

    files_dropped = pyqtSignal(list)

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData(MEDIA_MIME, item.data(Qt.ItemDataRole.UserRole).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(item.icon().pixmap(96, 54))
        drag.exec(Qt.DropAction.CopyAction)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            e.acceptProposedAction()


def placeholder_icon(kind: str) -> QIcon:
    pm = QPixmap(120, 68)
    pm.fill(QColor({"audio": "#2f6b4f", "image": "#5e3f86"}.get(kind, "#2f4f7f")))
    p = QPainter(pm)
    p.setPen(QColor("#ffffff"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, {"audio": "♫ AUDIO", "image": "IMAGE"}.get(kind, "VIDEO"))
    p.end()
    return QIcon(pm)


class MediaBin(QWidget):
    import_requested = pyqtSignal()
    add_requested = pyqtSignal(str)
    remove_requested = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        title = QLabel("<b>Project Media</b>")
        b_import = QToolButton(text="Import…")
        b_import.clicked.connect(self.import_requested)
        b_add = QToolButton(text="Insert ↓")
        b_add.setToolTip("Add the selected media to the timeline at the playhead")
        b_add.clicked.connect(self._add_current)
        b_rm = QToolButton(text="Remove")
        b_rm.clicked.connect(lambda: self.remove_requested.emit(self.selected_ids()))
        bar.addWidget(title)
        bar.addStretch(1)
        for b in (b_import, b_add, b_rm):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.list = MediaList()
        self.list.itemDoubleClicked.connect(lambda it: self.add_requested.emit(it.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.list, 1)
        hint = QLabel("Drag files here, then drag clips onto the timeline")
        hint.setStyleSheet("color:#7d8390; font-size:11px")
        lay.addWidget(hint)
        self.items: dict[str, QListWidgetItem] = {}

    def selected_ids(self) -> list[str]:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self.list.selectedItems()]

    def _add_current(self):
        for mid in self.selected_ids():
            self.add_requested.emit(mid)

    def rebuild(self, project: Project, thumbs: dict):
        self.list.clear()
        self.items.clear()
        for m in project.media.values():
            it = QListWidgetItem(f"{m.name}\n{fmt_tc(m.duration, 30)[3:-3] if m.kind != 'image' else 'still'}")
            it.setData(Qt.ItemDataRole.UserRole, m.id)
            it.setToolTip(f"{m.path}\n{m.width}x{m.height} {m.fps:.3g} fps" if m.has_video else m.path)
            it.setIcon(QIcon(QPixmap.fromImage(thumbs[m.id])) if m.id in thumbs else placeholder_icon(m.kind))
            if not os.path.exists(m.path):
                it.setForeground(QColor("#e06c6c"))
                it.setToolTip("OFFLINE: " + m.path)
            self.list.addItem(it)
            self.items[m.id] = it

    def set_thumb(self, mid: str, img: QImage):
        it = self.items.get(mid)
        if it is not None:
            it.setIcon(QIcon(QPixmap.fromImage(img)))


# ================================================================ inspector
class ColorButton(QPushButton):
    picked = pyqtSignal(str)

    def __init__(self, title="Color"):
        super().__init__()
        self.title = title
        self.value = "#ffffff"
        self.clicked.connect(self._pick)

    def set_value(self, name: str):
        self.value = name
        self.setText(name)
        self.setStyleSheet(f"background:{name}; color:{'#000' if QColor(name).lightness() > 128 else '#fff'}")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self.value), self, self.title)
        if c.isValid():
            self.set_value(c.name())
            self.picked.emit(c.name())


class Inspector(QWidget):
    """Properties of the selected clip, grouped in sections; rows hide when they don't apply."""

    changed = pyqtSignal(str, object)  # field, value
    save_template = pyqtSignal()

    PCT = {"opacity", "scale", "volume", "speed", "denoise", "shadow", "bg_opacity"}

    def __init__(self):
        super().__init__()
        self.setMinimumWidth(270)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.header = QLabel("<b>Inspector</b>")
        outer.addWidget(self.header)
        self.info = QLabel("Select a clip on the timeline.")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#9aa0ab")
        outer.addWidget(self.info)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 6, 0, 0)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addWidget(self.form_host)
        outer.addStretch(1)
        self.widgets: dict[str, QWidget] = {}
        self.rows: list[tuple[str, QWidget, object]] = []  # (key, widget, visible(clip) predicate)
        self.sections: list[tuple[QLabel, list[str]]] = []
        self.clip: Clip | None = None
        self._loading = False

        def dspin(lo, hi, step, dec, suffix=""):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(dec)
            s.setSuffix(suffix)
            s.setKeyboardTracking(False)
            return s

        def combo(options: dict):
            c = QComboBox()
            for k, v in options.items():
                c.addItem(v, k)
            return c

        def text_edit():
            t = QPlainTextEdit()
            t.setMaximumHeight(64)
            return t

        title = lambda c: c.kind == "title"
        visual = lambda c: c.kind != "audio"
        audio = lambda c: c.kind == "audio"
        av = lambda c: c.kind in ("video", "audio")
        always = lambda c: True
        sections = [
            ("Text", [
                ("text", "Title", text_edit(), title),
                ("subtext", "Subtitle", text_edit(), title),
            ]),
            ("Style", [
                ("font_family", "Font", QFontComboBox(), title),
                ("font_size", "Size", dspin(6, 800, 2, 0, " px"), title),
                ("bold", "Bold", QCheckBox(), title),
                ("italic", "Italic", QCheckBox(), title),
                ("align", "Align", combo(ALIGNS), title),
                ("color", "Text color", ColorButton("Text color"), title),
                ("sub_color", "Subtitle color", ColorButton("Subtitle color"), lambda c: title(c) and bool(c.subtext)),
                ("outline_width", "Outline", dspin(0, 60, 1, 1, " px"), title),
                ("outline_color", "Outline color", ColorButton("Outline color"),
                 lambda c: title(c) and c.outline_width > 0),
                ("shadow", "Shadow", dspin(0, 100, 10, 0, " %"), title),
                ("bg_style", "Background", combo(BACKGROUNDS), title),
                ("bg_color", "Bg color", ColorButton("Background color"),
                 lambda c: title(c) and c.bg_style in ("box", "band")),
                ("bg_opacity", "Bg opacity", dspin(0, 100, 5, 0, " %"),
                 lambda c: title(c) and c.bg_style in ("box", "band")),
                ("accent_color", "Accent color", ColorButton("Accent color"),
                 lambda c: title(c) and c.bg_style in ("box", "bar")),
            ]),
            ("Animation", [
                ("anim_in", "Animate in", combo(ANIMATIONS), title),
                ("anim_in_dur", "  duration", dspin(0.04, 30, 0.1, 2, " s"), lambda c: title(c) and bool(c.anim_in)),
                ("anim_out", "Animate out", combo(ANIMATIONS), title),
                ("anim_out_dur", "  duration", dspin(0.04, 30, 0.1, 2, " s"),
                 lambda c: title(c) and bool(c.anim_out)),
                ("roll", "Motion", combo(ROLLS), title),
            ]),
            ("Transform", [
                ("opacity", "Opacity", dspin(0, 100, 5, 0, " %"), visual),
                ("scale", "Scale", dspin(1, 1000, 5, 0, " %"), visual),
                ("pos_x", "Position X", dspin(-10000, 10000, 10, 0, " px"), visual),
                ("pos_y", "Position Y", dspin(-10000, 10000, 10, 0, " px"), visual),
            ]),
            ("Clip", [
                ("speed", "Speed", dspin(6.25, 1600, 10, 1, " %"), av),
                ("volume", "Volume", dspin(0, 400, 5, 0, " %"), audio),
                ("denoise", "Noise reduction", dspin(0, 100, 10, 0, " %"), audio),
                ("fade_in", "Fade in", dspin(0, 3600, 0.25, 2, " s"), always),
                ("fade_out", "Fade out", dspin(0, 3600, 0.25, 2, " s"), always),
            ]),
            ("Transitions", [
                ("tin", "Transition in", QComboBox(), always),
                ("tin_dur", "  duration", dspin(0.04, 30, 0.25, 2, " s"), lambda c: bool(c.tin)),
                ("tout", "Transition out", QComboBox(), always),
                ("tout_dur", "  duration", dspin(0.04, 30, 0.25, 2, " s"), lambda c: bool(c.tout)),
            ]),
        ]
        for name, rows in sections:
            head = QLabel(name.upper())
            head.setStyleSheet("color:#7d8390; font-size:10px; font-weight:bold; letter-spacing:1px;"
                               "padding-top:8px; border-bottom:1px solid #33363d")
            self.form.addRow(head)
            self.sections.append((head, [r[0] for r in rows]))
            for key, label, w, pred in rows:
                self.form.addRow(label, w)
                self.widgets[key] = w
                self.rows.append((key, w, pred))
                self._connect(key, w)
        self.widgets["text"].setPlaceholderText("Title text")
        self.widgets["subtext"].setPlaceholderText("Optional second line (name role, credit…)")
        self.reset_btn = QPushButton("Reset transform")
        self.reset_btn.clicked.connect(self._reset)
        self.form.addRow(self.reset_btn)
        self.template_btn = QPushButton(icons.icon("title"), "Save as Graphics Template…")
        self.template_btn.clicked.connect(self.save_template)
        self.form.addRow(self.template_btn)
        self.set_clip(None, None)

    def _connect(self, key, w):
        if isinstance(w, (QDoubleSpinBox, QSpinBox)):
            w.valueChanged.connect(lambda v: self._emit(key, v / 100 if key in self.PCT else v))
        elif isinstance(w, QFontComboBox):
            w.currentFontChanged.connect(lambda f: self._emit(key, f.family()))
        elif isinstance(w, QComboBox):
            w.currentIndexChanged.connect(lambda i: self._emit(key, w.itemData(i) or ""))
        elif isinstance(w, QCheckBox):
            w.toggled.connect(lambda on: self._emit(key, on))
        elif isinstance(w, ColorButton):
            w.picked.connect(lambda name: self._emit(key, name))
        elif isinstance(w, QPlainTextEdit):
            w.textChanged.connect(lambda: self._emit(key, w.toPlainText()))

    # fields whose change alters which rows are visible
    STRUCTURAL = {"tin", "tout", "bg_style", "anim_in", "anim_out", "outline_width", "subtext"}

    def _emit(self, k, v):
        if not self._loading and self.clip is not None:
            self.changed.emit(k, v)
            if k in self.STRUCTURAL:
                self._update_visibility()

    def _reset(self):
        for k, v in (("opacity", 1.0), ("scale", 1.0), ("pos_x", 0.0), ("pos_y", 0.0)):
            self.changed.emit(k, v)

    def _update_visibility(self):
        clip = self.clip
        if clip is None:
            return
        visible = {}
        for key, w, pred in self.rows:
            visible[key] = bool(pred(clip))
            self.form.setRowVisible(w, visible[key])
        for head, keys in self.sections:
            self.form.setRowVisible(head, any(visible[k] for k in keys))
        self.form.setRowVisible(self.reset_btn, clip.kind != "audio")
        self.form.setRowVisible(self.template_btn, clip.kind == "title")

    def set_clip(self, project: Project | None, clip: Clip | None):
        self.clip = clip
        self._loading = True
        try:
            if clip is None:
                self.header.setText("<b>Inspector</b>")
                self.info.setText("Select a clip on the timeline.")
                self.form_host.setVisible(False)
                return
            self.form_host.setVisible(True)
            m = project.media.get(clip.media_id) if clip.media_id else None
            kind = "Graphic" if clip.kind == "title" else clip.kind.title()
            self.header.setText(f"<b>{kind}</b> — {m.name if m else (clip.text.splitlines() or ['Title'])[0][:30]}")
            self.info.setText(f"Start {fmt_tc(clip.start, project.fps)}  ·  Dur {fmt_tc(clip.duration, project.fps)}"
                              + (f"<br>Source in {fmt_tc(clip.in_point, project.fps)}" if m and m.finite else ""))
            catalogue = AUDIO_TRANSITIONS if clip.kind == "audio" else VIDEO_TRANSITIONS
            for k in ("tin", "tout"):
                combo = self.widgets[k]
                combo.clear()
                combo.addItem("None", "")
                for tid, label in catalogue.items():
                    combo.addItem(label, tid)
            for key, w, _ in self.rows:
                v = getattr(clip, key)
                if isinstance(w, QFontComboBox):
                    w.setCurrentFont(QFont(v))
                elif isinstance(w, QComboBox):
                    w.setCurrentIndex(max(0, w.findData(v or "")))
                elif isinstance(w, (QDoubleSpinBox, QSpinBox)):
                    if key in ("fade_in", "fade_out", "anim_in_dur", "anim_out_dur"):
                        w.setMaximum(max(0.04, clip.duration))
                    w.setValue(v * 100 if key in self.PCT else v)
                elif isinstance(w, QCheckBox):
                    w.setChecked(bool(v))
                elif isinstance(w, ColorButton):
                    w.set_value(v)
                elif isinstance(w, QPlainTextEdit) and w.toPlainText() != v:
                    w.setPlainText(v)
            self._update_visibility()
        finally:
            self._loading = False


# ================================================================ effects


class _TransitionTree(QTreeWidget):
    def startDrag(self, actions):
        item = self.currentItem()
        kind = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not kind:
            return
        mime = QMimeData()
        mime.setData(TRANSITION_MIME, kind.encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(icons.icon("transition").pixmap(32, 32))
        drag.exec(Qt.DropAction.CopyAction)


class EffectsPanel(QWidget):
    """Transitions (drag onto a cut) and audio clean-up tools."""

    apply_transition = pyqtSignal(str)  # double-click: apply at the edit nearest the playhead
    detect_silences = pyqtSignal()
    noise_reduction = pyqtSignal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.tree = _TransitionTree()
        self.tree.setHeaderHidden(True)
        self.tree.setDragEnabled(True)
        self.tree.setIconSize(QSize(18, 18))
        for title, catalogue in (("Video Transitions", VIDEO_TRANSITIONS), ("Audio Transitions", AUDIO_TRANSITIONS)):
            top = QTreeWidgetItem([title])
            top.setFlags(Qt.ItemFlag.ItemIsEnabled)
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            self.tree.addTopLevelItem(top)
            for tid, label in catalogue.items():
                it = QTreeWidgetItem([label])
                it.setIcon(0, icons.icon("transition"))
                it.setData(0, Qt.ItemDataRole.UserRole, tid)
                it.setToolTip(0, "Drag onto a cut or clip edge · double-click: apply at the playhead")
                top.addChild(it)
            top.setExpanded(True)
        self.tree.itemDoubleClicked.connect(self._double)
        lay.addWidget(self.tree, 1)
        tools = QLabel("<b>Audio clean-up</b>")
        lay.addWidget(tools)
        for text, icon_name, sig, tip in (
                ("Detect Silences…", "silence", self.detect_silences,
                 "Find pauses and dead air; cut them, speed them up or mark them"),
                ("Noise Reduction…", "denoise", self.noise_reduction,
                 "Remove hiss, hum and room noise from the selected audio clips")):
            b = QPushButton(icons.icon(icon_name), text)
            b.setToolTip(tip)
            b.clicked.connect(sig)
            b.setStyleSheet("text-align:left; padding:6px")
            lay.addWidget(b)
        hint = QLabel("Drag a transition onto the edge between two clips. Shift+drag to apply only to one track.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7d8390; font-size:11px")
        lay.addWidget(hint)

    def _double(self, item, col):
        kind = item.data(0, Qt.ItemDataRole.UserRole)
        if kind:
            self.apply_transition.emit(kind)


# ================================================================ graphics templates
class _GraphicList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setIconSize(QSize(144, 81))
        self.setGridSize(QSize(156, 112))
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWordWrap(True)
        self.setDragEnabled(True)
        self.setSpacing(2)

    def startDrag(self, actions):
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setData(GRAPHIC_MIME, item.data(Qt.ItemDataRole.UserRole).encode())
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(item.icon().pixmap(120, 68))
        drag.exec(Qt.DropAction.CopyAction)


class GraphicsPanel(QWidget):
    """Essential-graphics templates: drag onto a video track or double-click to add at the playhead."""

    add_requested = pyqtSignal(str)
    delete_template = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(QLabel("<b>Graphics templates</b>"))
        self.list = _GraphicList()
        self.list.itemDoubleClicked.connect(lambda it: self.add_requested.emit(it.data(Qt.ItemDataRole.UserRole)))
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.list, 1)
        hint = QLabel("Drag onto a video track, or double-click to add at the playhead. Edit text, style and "
                      "animation in the Inspector; drag it in the Program Monitor to position it. "
                      "Graphic Sweep / Bars transitions are in the Effects tab.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#7d8390; font-size:11px")
        lay.addWidget(hint)
        self._icons: dict[str, QIcon] = {}

    def rebuild(self):
        from ..graphics import all_presets, preview_image
        self.list.clear()
        for pid, preset in all_presets().items():
            if pid not in self._icons:
                self._icons[pid] = QIcon(QPixmap.fromImage(preview_image(pid)))
            it = QListWidgetItem(self._icons[pid], preset.get("name", pid))
            it.setData(Qt.ItemDataRole.UserRole, pid)
            it.setToolTip(preset.get("name", pid) + ("  (your template)" if pid.startswith("user:") else ""))
            self.list.addItem(it)

    def forget_icon(self, pid: str):
        self._icons.pop(pid, None)

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        pid = it.data(Qt.ItemDataRole.UserRole)
        from PyQt6.QtWidgets import QMenu
        m = QMenu(self)
        m.addAction("Add at Playhead", lambda: self.add_requested.emit(pid))
        if pid.startswith("user:"):
            m.addAction("Delete Template", lambda: self.delete_template.emit(pid))
        m.exec(self.list.mapToGlobal(pos))
