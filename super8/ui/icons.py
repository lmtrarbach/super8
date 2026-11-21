"""Vector icons and tool cursors drawn with QPainter (24x24 design grid, no image assets)."""
from __future__ import annotations

from functools import lru_cache

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QCursor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

FG = "#d9dce3"
_DRAW = {}


def _reg(name):
    def deco(fn):
        _DRAW[name] = fn
        return fn
    return deco


class _Ctx:
    """Small drawing helper so icons can be re-drawn as a black outline for cursors."""

    def __init__(self, p: QPainter, color: QColor, extra: float = 0.0):
        self.p, self.c, self.extra = p, color, extra

    def pen(self, w=1.7):
        pen = QPen(self.c, w + self.extra)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        self.p.setPen(pen)
        self.p.setBrush(Qt.BrushStyle.NoBrush)

    def fill(self):
        self.p.setBrush(QBrush(self.c))
        if self.extra:
            self.pen(0.1)
            self.p.setBrush(QBrush(self.c))
        else:
            self.p.setPen(Qt.PenStyle.NoPen)

    def line(self, x1, y1, x2, y2, w=1.7):
        self.pen(w)
        self.p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def arrow(self, x1, y1, x2, y2, head=3.2, w=1.7):
        """Line with an arrow head at (x2, y2). Only horizontal/vertical arrows are needed."""
        self.line(x1, y1, x2, y2, w)
        dx = (x2 > x1) - (x2 < x1)
        dy = (y2 > y1) - (y2 < y1)
        if dx:
            self.line(x2, y2, x2 - dx * head, y2 - head, w)
            self.line(x2, y2, x2 - dx * head, y2 + head, w)
        else:
            self.line(x2, y2, x2 - head, y2 - dy * head, w)
            self.line(x2, y2, x2 + head, y2 - dy * head, w)

    def poly(self, pts, filled=True, w=1.5):
        if filled:
            self.fill()
        else:
            self.pen(w)
        self.p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))

    def rect(self, x, y, w, h, filled=False, radius=0.0, pw=1.6):
        if filled:
            self.fill()
        else:
            self.pen(pw)
        self.p.drawRoundedRect(QRectF(x, y, w, h), radius, radius)

    def bracket(self, x, open_right=True, top=4, bottom=20, lip=4):
        d = lip if open_right else -lip
        self.line(x, top, x, bottom)
        self.line(x, top, x + d, top)
        self.line(x, bottom, x + d, bottom)


# ------------------------------------------------------------------ tools
POINTER = [(5, 2.5), (5, 19), (9.2, 15), (12.2, 21.3), (15, 20), (12.1, 13.9), (17.6, 13.9)]


@_reg("select")
def _(g):
    g.poly(POINTER)


@_reg("track_select")
def _(g):
    g.poly([(x * 0.72 - 1, y * 0.72 + 1) for x, y in POINTER])
    g.arrow(12, 7, 22, 7, 2.6)
    g.arrow(12, 13, 22, 13, 2.6)
    g.arrow(12, 19, 22, 19, 2.6)


@_reg("ripple")
def _(g):
    g.bracket(9, True)
    g.arrow(9, 12, 21, 12, 3)
    g.line(3, 12, 6, 12)


@_reg("rolling")
def _(g):
    g.bracket(10.5, False, lip=3.5)
    g.bracket(13.5, True, lip=3.5)
    g.arrow(9, 12, 2, 12, 3)
    g.arrow(15, 12, 22, 12, 3)


@_reg("rate")
def _(g):
    g.line(3, 5, 3, 19)
    g.line(21, 5, 21, 19)
    g.arrow(12, 9, 5.5, 9, 2.8)
    g.arrow(12, 9, 18.5, 9, 2.8)
    g.p.save()
    g.pen(1.5)
    g.p.drawEllipse(QRectF(8.5, 12, 7, 7))
    g.p.drawLine(QPointF(12, 15.5), QPointF(12, 13.5))
    g.p.drawLine(QPointF(12, 15.5), QPointF(13.6, 15.5))
    g.p.restore()


@_reg("razor")
def _(g):
    p = g.p
    p.save()
    p.translate(12, 12)
    p.rotate(-40)
    path = QPainterPath()
    path.addRoundedRect(QRectF(-4.2, -10.5, 8.4, 21), 1.2, 1.2)
    slot = QPainterPath()
    slot.addRoundedRect(QRectF(-1.1, -6.5, 2.2, 13), 1.1, 1.1)
    path = path.subtracted(slot)
    g.fill()
    p.drawPath(path)
    p.restore()


@_reg("slip")
def _(g):
    g.line(3, 6, 3, 18)
    g.line(21, 6, 21, 18)
    g.rect(6, 8, 12, 8, pw=1.4)
    g.arrow(12, 12, 8, 12, 2)
    g.arrow(12, 12, 16, 12, 2)


@_reg("slide")
def _(g):
    g.rect(8.5, 7, 7, 10, filled=True, radius=1)
    g.arrow(7, 12, 1.8, 12, 2.6)
    g.arrow(17, 12, 22.2, 12, 2.6)
    g.line(5, 5, 5, 19, 1.2)
    g.line(19, 5, 19, 19, 1.2)


@_reg("hand")
def _(g):
    path = QPainterPath()
    for x, y, h in ((6.4, 6, 9), (9.1, 3.6, 11), (11.8, 3, 11.5), (14.5, 4.4, 10)):
        path.addRoundedRect(QRectF(x, y, 2.5, h), 1.25, 1.25)
    path.addRoundedRect(QRectF(6.4, 10.5, 10.6, 10.5), 3.2, 3.2)
    thumb = QPainterPath()
    thumb.addRoundedRect(QRectF(-1.3, -5.5, 2.6, 8), 1.3, 1.3)
    tr = thumb.translated(0, 0)
    from PyQt6.QtGui import QTransform
    t = QTransform()
    t.translate(17.3, 13.5)
    t.rotate(35)
    path = path.united(t.map(tr)).simplified()
    g.fill()
    g.p.drawPath(path)


def _magnifier(g, sign=None):
    g.pen(2)
    g.p.drawEllipse(QRectF(3.5, 3.5, 12, 12))
    g.line(14.2, 14.2, 20.5, 20.5, 3)
    if sign:
        g.line(6.8, 9.5, 12.2, 9.5, 1.6)
        if sign == "+":
            g.line(9.5, 6.8, 9.5, 12.2, 1.6)


@_reg("zoom")
def _(g):
    _magnifier(g, "+")


@_reg("zoom_in")
def _(g):
    _magnifier(g, "+")


@_reg("zoom_out")
def _(g):
    _magnifier(g, "-")


@_reg("zoom_fit")
def _(g):
    g.line(3, 6, 3, 18)
    g.line(21, 6, 21, 18)
    g.arrow(12, 12, 5.5, 12, 3)
    g.arrow(12, 12, 18.5, 12, 3)


@_reg("type")
def _(g):
    g.line(5.5, 5, 18.5, 5, 2.6)
    g.line(12, 5, 12, 19.5, 2.6)
    g.line(9, 19.5, 15, 19.5, 2)
    g.line(5.5, 5, 5.5, 7.5, 2)
    g.line(18.5, 5, 18.5, 7.5, 2)


# ------------------------------------------------------------------ actions
@_reg("new")
def _(g):
    g.poly([(6, 3), (14, 3), (19, 8), (19, 21), (6, 21)], filled=False)
    g.line(14, 3, 14, 8, 1.4)
    g.line(14, 8, 19, 8, 1.4)
    g.line(12.5, 11.5, 12.5, 17.5, 1.6)
    g.line(9.5, 14.5, 15.5, 14.5, 1.6)


@_reg("open")
def _(g):
    g.poly([(3, 6), (9, 6), (11, 8), (20, 8), (20, 19), (3, 19)], filled=False)
    g.line(3, 11, 20, 11, 1.4)


@_reg("save")
def _(g):
    g.poly([(4, 4), (17, 4), (20, 7), (20, 20), (4, 20)], filled=False)
    g.rect(8, 4, 8, 5, pw=1.3)
    g.rect(7, 13, 10, 7, pw=1.3)


@_reg("import")
def _(g):
    g.pen(1.7)
    g.p.drawPolyline(QPolygonF([QPointF(4, 14), QPointF(4, 20), QPointF(20, 20), QPointF(20, 14)]))
    g.arrow(12, 3, 12, 15.5, 4)


@_reg("export")
def _(g):
    g.pen(1.7)
    g.p.drawPolyline(QPolygonF([QPointF(4, 14), QPointF(4, 20), QPointF(20, 20), QPointF(20, 14)]))
    g.arrow(12, 16, 12, 3.5, 4)


def _curved(g, mirror=False):
    p = g.p
    p.save()
    if mirror:
        p.translate(24, 0)
        p.scale(-1, 1)
    path = QPainterPath(QPointF(19, 18))
    path.cubicTo(QPointF(19.5, 9), QPointF(12, 6.5), QPointF(6, 9.5))
    g.pen(1.9)
    p.drawPath(path)
    g.line(6, 9.5, 9.6, 5.4, 1.9)
    g.line(6, 9.5, 10.6, 12, 1.9)
    p.restore()


@_reg("undo")
def _(g):
    _curved(g)


@_reg("redo")
def _(g):
    _curved(g, True)


@_reg("split")
def _(g):
    g.line(12, 2.5, 12, 21.5, 1.4)
    g.rect(2.5, 7, 7.5, 10, filled=True, radius=1)
    g.rect(14, 7, 7.5, 10, filled=True, radius=1)


@_reg("ripple_delete")
def _(g):
    g.rect(2.5, 7, 6, 10, filled=True, radius=1)
    g.rect(10, 7, 4, 10, pw=1.2)
    g.line(10, 7, 14, 17, 1.2)
    g.line(14, 7, 10, 17, 1.2)
    g.arrow(21.5, 12, 15.5, 12, 2.5)


@_reg("delete")
def _(g):
    g.line(4, 6.5, 20, 6.5, 1.8)
    g.line(9.5, 6.5, 10, 3.5, 1.5)
    g.line(10, 3.5, 14, 3.5, 1.5)
    g.line(14, 3.5, 14.5, 6.5, 1.5)
    g.poly([(6, 6.5), (18, 6.5), (16.8, 21), (7.2, 21)], filled=False)
    g.line(10, 10, 10.3, 18, 1.3)
    g.line(14, 10, 13.7, 18, 1.3)


@_reg("magnet")
def _(g):
    path = QPainterPath(QPointF(6.5, 3.5))
    path.lineTo(6.5, 12)
    path.arcTo(QRectF(6.5, 6.5, 11, 11), 180, 180)
    path.lineTo(17.5, 3.5)
    g.pen(3.6)
    g.p.drawPath(path)
    red = QPen(QColor("#e05a5a"), 3.6 + g.extra)
    if not g.extra:
        g.p.setPen(red)
    g.p.drawLine(QPointF(6.5, 3.5), QPointF(6.5, 6.5))
    g.p.drawLine(QPointF(17.5, 3.5), QPointF(17.5, 6.5))


@_reg("link")
def _(g):
    p = g.p
    p.save()
    p.translate(12, 12)
    p.rotate(-45)
    g.pen(2)
    p.drawRoundedRect(QRectF(-9.5, -3.2, 10.5, 6.4), 3.2, 3.2)
    p.drawRoundedRect(QRectF(-1, -3.2, 10.5, 6.4), 3.2, 3.2)
    p.restore()


@_reg("title")
def _(g):
    g.rect(2.5, 4.5, 19, 15, radius=2, pw=1.4)
    g.line(7.5, 8.5, 16.5, 8.5, 2)
    g.line(12, 8.5, 12, 16, 2)


@_reg("track_add")
def _(g):
    g.rect(2.5, 5, 13, 5, filled=True, radius=1)
    g.rect(2.5, 13, 13, 5, pw=1.3, radius=1)
    g.line(19.5, 12, 19.5, 20, 1.8)
    g.line(15.5, 16, 23.5, 16, 1.8)


@_reg("transition")
def _(g):
    g.rect(2.5, 6, 11, 12, filled=True, radius=1.5)
    g.rect(10.5, 6, 11, 12, radius=1.5, pw=1.5)
    g.line(10.5, 18, 13.5, 6, 1.3)


@_reg("silence")
def _(g):
    for x, h in ((3, 4), (5.5, 9), (8, 6), (16, 7), (18.5, 10), (21, 5)):
        g.line(x, 12 - h / 2, x, 12 + h / 2, 1.6)
    g.line(10.5, 12, 13.5, 12, 1.2)
    g.line(10, 4, 10, 20, 1)
    g.line(14, 4, 14, 20, 1)


@_reg("denoise")
def _(g):
    path = QPainterPath(QPointF(2, 12))
    for i in range(1, 21):
        x = 2 + i * 0.5
        y = 12 + (3.5 if i % 2 else -3.5) * (1 - i / 22)
        path.lineTo(x, y)
    path.cubicTo(QPointF(14, 6), QPointF(16, 18), QPointF(18.5, 12))
    path.cubicTo(QPointF(20, 9), QPointF(21, 12), QPointF(22, 12))
    g.pen(1.5)
    g.p.drawPath(path)


@_reg("marker")
def _(g):
    g.poly([(6, 4), (18, 4), (18, 15), (12, 20), (6, 15)])


# ------------------------------------------------------------------ public API
def paint(p: QPainter, name: str, color: QColor, extra: float = 0.0):
    _DRAW[name](_Ctx(p, color, extra))


@lru_cache(maxsize=None)
def icon(name: str, color: str = FG) -> QIcon:
    ic = QIcon()
    for size in (24, 48):
        for mode, col in ((QIcon.Mode.Normal, QColor(color)), (QIcon.Mode.Disabled, QColor("#5d626c")),
                          (QIcon.Mode.Active, QColor("#ffffff")), (QIcon.Mode.Selected, QColor("#ffffff"))):
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.scale(size / 24, size / 24)
            paint(p, name, col)
            p.end()
            ic.addPixmap(pm, mode)
    return ic


HOTSPOTS = {"select": (5, 3), "track_select": (3, 3), "razor": (12, 12), "zoom": (9, 9), "hand": (12, 12),
            "type": (12, 12)}


@lru_cache(maxsize=None)
def cursor(name: str) -> QCursor:
    """White glyph with a dark outline so it reads on any background."""
    pm = QPixmap(24, 24)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    paint(p, name, QColor(0, 0, 0, 230), extra=2.2)
    paint(p, name, QColor("#ffffff"))
    p.end()
    hx, hy = HOTSPOTS.get(name, (12, 12))
    return QCursor(pm, hx, hy)
