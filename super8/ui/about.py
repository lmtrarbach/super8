"""The About window: what Super8 is, its license, and how to support it."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFontDatabase, QGuiApplication, QIcon, QPixmap
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QGridLayout, QLabel, QLineEdit, QPushButton,
                             QVBoxLayout)

from .. import __version__

LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
ICON_PATH = Path(__file__).resolve().parent.parent / "resources" / "super8.png"

# Same addresses as Candle (Help → Support Candle).
DONATIONS = (
    ("Bitcoin", "bc1qnyceh488yqr0l6ffljygf47243p8wsvct4mkw7"),
    ("Ethereum", "0x8bc6D2D3d47D15EEd881975A09F008609F3bAdF2"),
    ("Solana", "3Gq3t2Xoix8eRvyxCE8mdB5BPsNCwHxDVc8oLXMTeFe1"),
)


def app_icon() -> QIcon:
    return QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()


class AboutDialog(QDialog):
    def __init__(self, parent=None, focus_support: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Support Super8" if focus_support else "About Super8")
        self.setFixedWidth(620)

        icon = QLabel()
        icon.setPixmap(QPixmap(str(ICON_PATH)).scaled(
            96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel(f"<span style='font-size:20px; font-weight:bold'>Super8 {__version__}</span>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        intro = QLabel(
            "<p><b>Super8 is free and open source.</b> It costs nothing, has no ads and needs no account. "
            "Editing happens on your computer; the optional AI assistant uses <i>your own</i> Claude account "
            "and only sends Anthropic your project's structure and the frames Claude looks at when you "
            "ask it something.</p>"
            "<p>A non-linear video editor for Linux: multi-track timeline, Premiere-style editing tools, "
            "transitions, animated graphics, silence cutting, noise reduction and an AI editing assistant, powered by FFmpeg.</p>"
            f"<p>Released under the <a href='{LICENSE_URL}'>GNU General Public License v3.0</a>: you may use, "
            "study, share and improve it.</p>")
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)

        support = QLabel("<b>Support Super8</b><br>If Super8 helps you tell your stories, you can support its "
                         "development with a donation to one of these addresses:")
        support.setWordWrap(True)

        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        grid = QGridLayout()
        self.copy_buttons: dict[str, QPushButton] = {}
        for row, (coin, address) in enumerate(DONATIONS):
            field = QLineEdit(address)
            field.setReadOnly(True)
            field.setFont(mono)
            field.setCursorPosition(0)
            field.setMinimumWidth(380)
            copy = QPushButton("Copy")
            copy.clicked.connect(lambda _=False, a=address, b=copy: self._copy(a, b))
            self.copy_buttons[coin] = copy
            grid.addWidget(QLabel(coin), row, 0)
            grid.addWidget(field, row, 1)
            grid.addWidget(copy, row, 2)

        box = QFrame()
        box.setObjectName("card")
        box.setStyleSheet("QFrame#card { background:#2b2f37; border:1px solid #3a3f49; border-radius:8px; }")
        bv = QVBoxLayout(box)
        bv.addWidget(support)
        bv.addLayout(grid)
        thanks = QLabel("Thank you ♥")
        thanks.setStyleSheet("color:#9aa0ab")
        bv.addWidget(thanks)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addWidget(box)
        layout.addWidget(buttons)
        # wrapped labels only know their height once the width is known
        self.setMinimumHeight(layout.totalHeightForWidth(self.width()))

    def _copy(self, address: str, button: QPushButton):
        QGuiApplication.clipboard().setText(address)
        button.setText("Copied ✓")
        QTimer.singleShot(1500, lambda: button.setText("Copy"))
