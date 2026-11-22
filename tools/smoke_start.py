"""Start the installed app offscreen, let it run two seconds, and quit: catches a broken install
(e.g. a package missing from the wheel) that tests run from the source tree cannot see.

Run it from outside the source tree with the installed package's Python:
    QT_QPA_PLATFORM=offscreen python tools/smoke_start.py
"""
import os
import sys

if os.path.isdir(os.path.join(os.getcwd(), "super8")) and "--allow-source" not in sys.argv:
    sys.exit("run this from outside the source tree so the installed package is the one imported")

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import super8  # noqa: E402
from super8.ui import main_window  # noqa: E402

print("starting Super8", super8.__version__, "from", os.path.dirname(super8.__file__))
_exec = QApplication.exec


def _exec_then_quit(*_):
    QTimer.singleShot(2000, QApplication.quit)
    return _exec()


QApplication.exec = staticmethod(_exec_then_quit)
code = main_window.run(["super8"])
print("window started and closed cleanly" if code == 0 else f"exit code {code}")
sys.exit(code)
