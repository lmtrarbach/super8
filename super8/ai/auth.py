"""Sign in with Claude: each user's own account, through Anthropic's official CLI (`ant`).

`ant auth login --no-browser` prints an authorize URL, the user signs in to their Claude account in the
browser, and the Console shows a code that is pasted back. The token is stored in Super8's own
credentials folder (~/.config/super8/anthropic, via ANTHROPIC_CONFIG_DIR), where the Anthropic SDK
finds and refreshes it. A private folder matters: `ant auth login --profile X` also makes X the active
profile, so logging in inside the shared ~/.config/anthropic would retarget the user's other tools.
Super8 never sees a password or an API key.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

PROFILE = "super8"
ANT_RELEASES_URL = "https://github.com/anthropics/anthropic-cli/releases"
URL_RE = re.compile(r"https://\S+")


def config_dir() -> Path:
    """Super8's private credentials folder (never the shared ~/.config/anthropic)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "super8" / "anthropic"


def find_ant() -> str | None:
    """The CLI bundled in our snap, else one on PATH."""
    snap = os.environ.get("SNAP")
    if snap:
        bundled = Path(snap) / "bin" / "ant"
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    return shutil.which("ant")


def has_profile_login() -> bool:
    return (config_dir() / "credentials" / f"{PROFILE}.json").is_file()


def env_credentials() -> str | None:
    """Developers can still use an API key / token from the environment."""
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if os.environ.get(k):
            return k
    return None


def is_signed_in() -> bool:
    return has_profile_login() or env_credentials() is not None


def make_client():
    """An Anthropic client for the signed-in user (their profile, or env credentials for developers)."""
    import anthropic
    if has_profile_login() and not env_credentials():
        # the SDK reads ANTHROPIC_CONFIG_DIR; scope it to this process so it finds our private folder
        os.environ["ANTHROPIC_CONFIG_DIR"] = str(config_dir())
        return anthropic.Anthropic(profile=PROFILE)
    return anthropic.Anthropic()


class LoginProcess(QObject):
    """Drives `ant auth login --no-browser --profile super8`.

    Signals: url_ready(authorize_url) once the CLI prints it; finished(ok, message) when it exits.
    Call submit_code() with the code the Console page shows after the user authorizes.
    """

    url_ready = pyqtSignal(str)
    output = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, ant: str, parent=None):
        super().__init__(parent)
        self.ant = ant
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(self._error)
        self.text = ""
        self.url = None
        self._reported = False

    def start(self):
        env = QProcessEnvironment.systemEnvironment()
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
            env.remove(k)  # they would shadow or redirect the profile we log into
        config_dir().mkdir(parents=True, exist_ok=True)
        env.insert("ANTHROPIC_CONFIG_DIR", str(config_dir()))
        self.proc.setProcessEnvironment(env)
        self.proc.start(self.ant, ["auth", "login", "--no-browser", "--profile", PROFILE])

    def submit_code(self, code: str):
        code = code.strip()
        if code:
            self.proc.write((code + "\n").encode())

    def cancel(self):
        if self.proc.state() != QProcess.ProcessState.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(2000)

    def _read(self):
        chunk = bytes(self.proc.readAllStandardOutput()).decode(errors="replace")
        self.text += chunk
        self.output.emit(chunk)
        if self.url is None:
            m = URL_RE.search(self.text)
            if m:
                self.url = m.group(0)
                self.url_ready.emit(self.url)

    def _done(self, code, status):
        self._read()
        if self._reported:
            return
        self._reported = True
        ok = code == 0 and status == QProcess.ExitStatus.NormalExit
        lines = [ln.strip() for ln in self.text.splitlines() if ln.strip()]
        # the "Code: " prompt has no newline, so the success text can share its line
        m = re.search(r"Logged in[^\n]*", self.text) if ok else None
        message = m.group(0).strip().rstrip(".") if m else None
        if not ok:
            message = next((ln for ln in reversed(lines) if ln.lower().startswith(("error", "ant:"))),
                           lines[-1] if lines else "sign-in did not finish")
        self.finished.emit(ok, message or "Signed in")

    def _error(self, err):
        if err == QProcess.ProcessError.FailedToStart and not self._reported:
            self._reported = True
            self.finished.emit(False, f"could not start {self.ant}")


def sign_out() -> None:
    """Forget Super8's login (only our own profile; other Claude logins are untouched)."""
    ant = find_ant()
    if ant:
        p = QProcess()
        env = QProcessEnvironment.systemEnvironment()
        env.insert("ANTHROPIC_CONFIG_DIR", str(config_dir()))
        p.setProcessEnvironment(env)
        p.start(ant, ["auth", "logout", "--profile", PROFILE])
        p.waitForFinished(10000)
    cred = config_dir() / "credentials" / f"{PROFILE}.json"
    if cred.exists():
        cred.unlink()
