"""Shared fixtures. Qt runs offscreen; settings are redirected to a temp dir; sample media is generated once."""
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / ".samples"
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def samples():
    if not (SAMPLES / "talk.mp4").exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "make_samples.py"), str(SAMPLES)], check=True)
    return {p.name: str(p) for p in SAMPLES.iterdir()}


@pytest.fixture(scope="session")
def qapp(tmp_path_factory):
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
    cfg = tmp_path_factory.mktemp("settings")
    for fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
        QSettings.setPath(fmt, QSettings.Scope.UserScope, str(cfg))
    app = QApplication.instance() or QApplication(["super8-tests"])
    from super8.ui.main_window import apply_dark_theme
    apply_dark_theme(app)
    return app


def pump(app, sec=0.2):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.fixture
def win(qapp, samples):
    """A main window with clip_a, clip_b and talk imported and decoded."""
    from super8.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1500, 920)
    w.show()
    w.import_files([samples["clip_a.mp4"], samples["clip_b.mp4"], samples["talk.mp4"], samples["logo.png"]])
    for _ in range(100):
        pump(qapp, 0.1)
        if len(w.audio_cache.samples) == 3 and len(w.thumbs) == 4:
            break
    w.ids = {m.name: m.id for m in w.project.media.values()}
    w.pump = lambda sec=0.2: pump(qapp, sec)
    yield w
    w.modified = False
    w.close()
