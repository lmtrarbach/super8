"""Silence detector and noise-reduction dialogs."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                             QHBoxLayout, QLabel, QPushButton, QRadioButton, QSlider, QVBoxLayout, QWidget)

from ..audiofx import ENV_WIN, auto_threshold, find_silences
from .timeline import fmt_tc

DB_MIN, DB_MAX = -80.0, 0.0


class EnvelopePlot(QWidget):
    """Loudness over time with the threshold line and detected silences. Click to set the threshold."""

    threshold_picked = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(640, 170)
        self.db = np.zeros(0, np.float32)
        self.thr = -40.0
        self.t0 = 0.0
        self.ranges: list[tuple[float, float]] = []
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip("Click to set the threshold")

    def set_data(self, db, t0):
        self.db, self.t0 = db, t0
        self.update()

    def y_of(self, db):
        h = self.height() - 20
        return 4 + (DB_MAX - max(DB_MIN, min(DB_MAX, db))) / (DB_MAX - DB_MIN) * h

    def mousePressEvent(self, e):
        h = self.height() - 20
        db = DB_MAX - (e.position().y() - 4) / h * (DB_MAX - DB_MIN)
        self.threshold_picked.emit(float(max(DB_MIN, min(DB_MAX, db))))

    def paintEvent(self, ev):
        p = QPainter(self)
        W, H = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#16181c"))
        n = len(self.db)
        if n == 0:
            p.setPen(QColor("#8a909b"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No audio in range")
            return
        dur = n * ENV_WIN
        sx = W / dur
        # silences
        for a, b in self.ranges:
            p.fillRect(QRectF((a - self.t0) * sx, 0, max(1.0, (b - a) * sx), H - 16), QColor(224, 80, 80, 70))
        # envelope: max per pixel column
        cols = np.array_split(self.db, min(W, n))
        peaks = np.array([c.max() for c in cols])
        path = QPainterPath(QPointF(0, self.y_of(DB_MIN)))
        step = W / len(peaks)
        for i, v in enumerate(peaks):
            path.lineTo(i * step, self.y_of(float(v)))
        path.lineTo(W, self.y_of(DB_MIN))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(60, 154, 106, 150))
        p.drawPath(path)
        # grid + threshold
        p.setPen(QColor("#3a3f49"))
        for db in range(-70, 0, 10):
            y = self.y_of(db)
            p.drawLine(0, int(y), W, int(y))
            p.drawText(3, int(y) - 2, f"{db} dB")
        y = self.y_of(self.thr)
        p.setPen(QPen(QColor("#ffd34d"), 1.5, Qt.PenStyle.DashLine))
        p.drawLine(0, int(y), W, int(y))
        p.setPen(QColor("#ffd34d"))
        p.drawText(W - 120, int(y) - 4, f"threshold {self.thr:.1f} dB")
        # time axis
        p.setPen(QColor("#8a909b"))
        for i in range(6):
            t = self.t0 + dur * i / 5
            x = int(W * i / 5)
            p.drawText(min(x + 2, W - 70), H - 3, fmt_tc(t, 30)[3:])
        p.end()


class SilenceDialog(QDialog):
    """Find pauses and dead air; cut them, speed them up, or just mark them for review."""

    ACTIONS = [("cut", "Remove silences (ripple cut — jump cuts)"),
               ("speed4", "Speed silences up 4×"),
               ("speed8", "Speed silences up 8×"),
               ("mark", "Only mark them on the timeline")]
    PRESETS = {"Tight (podcast / vlog)": (0.35, 0.08), "Natural": (0.6, 0.15), "Gentle": (1.2, 0.25)}

    def __init__(self, parent, db: np.ndarray, t0: float, t1: float, fps: float, scope_text: str):
        super().__init__(parent)
        self.setWindowTitle("Detect Silences")
        self.db, self.t0, self.t1, self.fps = db, t0, t1, fps
        self.ranges: list[tuple[float, float]] = []
        lay = QVBoxLayout(self)
        info = QLabel(f"Analysing <b>{scope_text}</b> · {fmt_tc(t1 - t0, fps)}. Quiet stretches are the red "
                      "areas; click the graph to move the threshold.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.plot = EnvelopePlot()
        self.plot.set_data(db, t0)
        self.plot.threshold_picked.connect(lambda v: self.thr.setValue(v))
        lay.addWidget(self.plot, 1)

        form = QFormLayout()
        row = QHBoxLayout()
        self.thr = QDoubleSpinBox()
        self.thr.setRange(DB_MIN, DB_MAX)
        self.thr.setSuffix(" dB")
        self.thr.setDecimals(1)
        self.thr.setSingleStep(1)
        auto = QPushButton("Auto")
        auto.setToolTip("Estimate from the noise floor and the speech level")
        auto.clicked.connect(lambda: self.thr.setValue(auto_threshold(self.db)))
        row.addWidget(self.thr, 1)
        row.addWidget(auto)
        form.addRow("Threshold", row)
        self.min_sil = QDoubleSpinBox()
        self.min_sil.setRange(0.05, 30)
        self.min_sil.setSingleStep(0.05)
        self.min_sil.setSuffix(" s")
        self.min_sil.setToolTip("Pauses shorter than this are kept (natural rhythm of speech)")
        form.addRow("Minimum silence", self.min_sil)
        self.pad = QDoubleSpinBox()
        self.pad.setRange(0, 2)
        self.pad.setSingleStep(0.02)
        self.pad.setSuffix(" s")
        self.pad.setToolTip("Audio kept on each side of a cut so words and breaths aren't clipped")
        form.addRow("Keep padding", self.pad)
        self.preset = QComboBox()
        self.preset.addItems(list(self.PRESETS))
        self.preset.currentTextChanged.connect(self._apply_preset)
        form.addRow("Preset", self.preset)
        self.action_group = QButtonGroup(self)
        act_box = QVBoxLayout()
        for i, (key, label) in enumerate(self.ACTIONS):
            rb = QRadioButton(label)
            rb.setProperty("key", key)
            self.action_group.addButton(rb, i)
            act_box.addWidget(rb)
        self.action_group.button(0).setChecked(True)
        form.addRow("Action", act_box)
        lay.addLayout(form)
        self.stats = QLabel()
        self.stats.setStyleSheet("color:#ffd34d")
        lay.addWidget(self.stats)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        for w in (self.thr, self.min_sil, self.pad):
            w.valueChanged.connect(self.recompute)
        self.preset.setCurrentText("Natural")
        self._apply_preset("Natural")
        self.thr.setValue(auto_threshold(db))
        self.resize(820, 560)

    def _apply_preset(self, name):
        m, pad = self.PRESETS[name]
        self.min_sil.setValue(m)
        self.pad.setValue(pad)

    @property
    def action(self) -> str:
        return self.action_group.checkedButton().property("key")

    def recompute(self):
        self.ranges = find_silences(self.db, self.t0, self.thr.value(), self.min_sil.value(), self.pad.value(),
                                    self.fps)
        self.plot.thr = self.thr.value()
        self.plot.ranges = self.ranges
        self.plot.update()
        total = sum(b - a for a, b in self.ranges)
        span = max(1e-6, self.t1 - self.t0)
        self.stats.setText(f"{len(self.ranges)} silences · {total:.1f} s ({100 * total / span:.0f}% of the range)"
                           f" · result ≈ {fmt_tc(span - total, self.fps)} if cut")


class NoiseDialog(QDialog):
    LEVELS = [("Light", 0.3), ("Medium", 0.6), ("Strong", 0.85)]

    def __init__(self, parent, n_clips: int, current: float):
        super().__init__(parent)
        self.setWindowTitle("Noise Reduction")
        lay = QVBoxLayout(self)
        txt = QLabel(f"Applies to <b>{n_clips}</b> audio clip(s). The noise profile (hiss, hum, fan, room tone) "
                     "is learned automatically from the quietest moments of each clip, so recordings with "
                     "pauses work best. Processing runs in the background; play the timeline to compare.")
        txt.setWordWrap(True)
        lay.addWidget(txt)
        row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(current * 100)) or 60)
        self.value = QLabel()
        self.slider.valueChanged.connect(lambda v: self.value.setText(f"{v}%" if v else "Off"))
        self.slider.valueChanged.emit(self.slider.value())
        row.addWidget(QLabel("Strength"))
        row.addWidget(self.slider, 1)
        row.addWidget(self.value)
        lay.addLayout(row)
        presets = QHBoxLayout()
        for name, v in [("Off", 0.0)] + self.LEVELS:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, v=v: self.slider.setValue(int(v * 100)))
            presets.addWidget(b)
        lay.addLayout(presets)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(480, 10)

    @property
    def strength(self) -> float:
        return self.slider.value() / 100
