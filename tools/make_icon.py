"""Draw the Super8 app icon (two film reels stacked into an "8") and write the PNGs the app and snap use."""
import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QRadialGradient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 512, size / 512)
    # tile
    g = QLinearGradient(0, 0, 512, 512)
    g.setColorAt(0, QColor("#2e3a55"))
    g.setColorAt(1, QColor("#141925"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawRoundedRect(QRectF(24, 24, 464, 464), 104, 104)
    # film-strip perforations down both sides
    p.setBrush(QColor(255, 255, 255, 38))
    for i in range(7):
        y = 70 + i * 56
        p.drawRoundedRect(QRectF(50, y, 30, 34), 7, 7)
        p.drawRoundedRect(QRectF(432, y, 30, 34), 7, 7)
    # two reels make the "8"
    for cy, r in ((150, 96), (358, 112)):  # the loops touch at y=246, like the strokes of an 8
        rg = QRadialGradient(QPointF(256 - r * 0.3, cy - r * 0.3), r * 1.6)
        rg.setColorAt(0, QColor("#ffd970"))
        rg.setColorAt(1, QColor("#f2a516"))
        reel = QPainterPath()
        reel.addEllipse(QPointF(256, cy), r, r)
        holes = QPainterPath()
        for k in range(5):
            a = math.radians(-90 + k * 72)
            holes.addEllipse(QPointF(256 + math.cos(a) * r * 0.55, cy + math.sin(a) * r * 0.55), r * 0.2, r * 0.2)
        holes.addEllipse(QPointF(256, cy), r * 0.11, r * 0.11)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawPath(reel.translated(0, 8))  # soft drop shadow
        p.setBrush(rg)
        p.drawPath(reel.subtracted(holes))
    # record dot
    p.setBrush(QColor("#e5484d"))
    p.drawEllipse(QPointF(400, 104), 26, 26)
    p.end()
    return img


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    big = draw(512)
    for path in (os.path.join(ROOT, "super8", "resources", "super8.png"), os.path.join(ROOT, "snap", "gui", "super8.png")):
        big.save(path)
        print("wrote", path)
