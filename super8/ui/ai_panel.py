"""AI Assistant panel: chat with Claude (the user's own account) about the open project."""
from __future__ import annotations

import html

from PyQt6.QtCore import QEvent, QSettings, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                             QPushButton, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from ..ai import auth
from ..ai.assistant import DEFAULT_MODEL, MODELS, AssistantWorker, GuiExecutor
from ..ai.tools import Toolkit

SUGGESTIONS = [
    ("Describe my video", "Look at my timeline and frames and describe what this video is about."),
    ("Cut the pauses", "Find the long pauses and dead air in my talking and cut them out."),
    ("Add a lower third", "Add a lower third near the start introducing the speaker. Ask me the name."),
    ("Polish the edit", "Suggest and apply small improvements: transitions between clips, fades at the "
                        "start and end, and a title."),
]


TOOL_LABELS = {
    "get_project": "Read the project",
    "view_frames": "Looked at frames",
    "add_clip": "Added a clip",
    "move_clip": "Moved a clip",
    "trim_clip": "Trimmed a clip",
    "split": "Split clips",
    "delete_clips": "Deleted clips",
    "set_clip_properties": "Changed clip properties",
    "add_title": "Added a title",
    "add_transition": "Set a transition",
    "find_silences": "Found silences",
    "cut_ranges": "Cut ranges",
    "add_track": "Added a track",
    "show": "Moved the playhead",
}


class LoginDialog(QDialog):
    """Sign in with Claude via `ant auth login --no-browser` (browser sign-in, then paste the code)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sign in with Claude")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        intro = QLabel("Super8 uses <b>your own Claude account</b>. Sign in with your Claude Console "
                       "(Anthropic API) account in the browser; usage is billed to that account. "
                       "Super8 never sees your password or an API key.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.step = QLabel("Starting sign-in…")
        self.step.setWordWrap(True)
        self.step.setOpenExternalLinks(True)
        self.step.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lay.addWidget(self.step)
        row = QHBoxLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText("Paste the code shown after you authorize")
        self.code.setEnabled(False)
        self.code.returnPressed.connect(self._submit)
        self.submit_btn = QPushButton("Sign in")
        self.submit_btn.setEnabled(False)
        self.submit_btn.clicked.connect(self._submit)
        row.addWidget(self.code, 1)
        row.addWidget(self.submit_btn)
        lay.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#9aa0ab")
        lay.addWidget(self.status)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.message = ""
        self.proc = None
        ant = auth.find_ant()
        if ant is None:
            self.step.setText(
                "Sign-in needs Anthropic's command-line tool <code>ant</code>, which isn't installed. "
                f"Get it from <a href='{auth.ANT_RELEASES_URL}'>{auth.ANT_RELEASES_URL}</a>, then try again. "
                "(The Super8 snap includes it.)")
            return
        self.proc = auth.LoginProcess(ant, self)
        self.proc.url_ready.connect(self._url_ready)
        self.proc.finished.connect(self._finished)
        self.proc.start()

    def _url_ready(self, url: str):
        self.url = url
        QDesktopServices.openUrl(QUrl(url))
        self.step.setText(f"1. Your browser opened the Claude sign-in page (or <a href='{html.escape(url)}'>"
                          "open it here</a>). Sign in and approve Super8.<br>"
                          "2. Copy the code the page shows and paste it below.")
        self.code.setEnabled(True)
        self.submit_btn.setEnabled(True)
        self.code.setFocus()

    def _submit(self):
        if self.proc and self.code.text().strip():
            self.proc.submit_code(self.code.text())
            self.code.setEnabled(False)
            self.submit_btn.setEnabled(False)
            self.status.setText("Checking…")

    def _finished(self, ok: bool, message: str):
        if ok:
            self.message = message
            self.accept()
        else:
            self.status.setText(f"Sign-in failed: {message}")
            self.step.setText("Close this window and try again.")

    def reject(self):
        if self.proc:
            self.proc.cancel()
        super().reject()


class AIPanel(QWidget):
    """Chat with Claude; it reads and edits the project through tools (one undo step per request)."""

    busy_changed = pyqtSignal(bool)

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.settings = QSettings("super8", "super8")
        self.toolkit = Toolkit(main)
        self.executor = GuiExecutor(self.toolkit)
        self.messages: list = []  # API conversation for the open project
        self.entries: list[dict] = []  # what the transcript shows
        self.worker: AssistantWorker | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        top = QHBoxLayout()
        title = QLabel("<b>Claude</b>")
        self.account = QLabel()
        self.account.setStyleSheet("color:#9aa0ab")
        self.sign_btn = QToolButton()
        self.sign_btn.clicked.connect(self._sign_in_out)
        top.addWidget(title)
        top.addWidget(self.account, 1)
        top.addWidget(self.sign_btn)
        lay.addLayout(top)
        row = QHBoxLayout()
        self.model = QComboBox()
        for mid, name in MODELS.items():
            self.model.addItem(name, mid)
        saved = self.settings.value("ai_model", DEFAULT_MODEL)
        self.model.setCurrentIndex(max(0, self.model.findData(saved)))
        self.model.currentIndexChanged.connect(
            lambda: self.settings.setValue("ai_model", self.model.currentData()))
        self.model.setToolTip("Model used for requests (billed to your Claude account)")
        new_chat = QToolButton(text="New chat")
        new_chat.setToolTip("Start a fresh conversation")
        new_chat.clicked.connect(self.new_chat)
        row.addWidget(self.model, 1)
        row.addWidget(new_chat)
        lay.addLayout(row)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        lay.addWidget(self.view, 1)
        self.suggest_row = QWidget()
        sl = QVBoxLayout(self.suggest_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(2)
        for label, prompt in SUGGESTIONS:
            b = QPushButton(label)
            b.setStyleSheet("text-align:left; padding:4px 8px")
            b.clicked.connect(lambda _=False, p=prompt: self.send(p))
            sl.addWidget(b)
        lay.addWidget(self.suggest_row)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Ask Claude to edit your video…  (Enter to send, Shift+Enter for a new line)")
        self.input.setMaximumHeight(84)
        self.input.installEventFilter(self)
        lay.addWidget(self.input)
        bottom = QHBoxLayout()
        note = QLabel("Uses your Claude account. Sends the project's structure, and any frames Claude looks "
                      "at, to Anthropic.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#7d8390; font-size:10px")
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send_or_stop)
        bottom.addWidget(note, 1)
        bottom.addWidget(self.send_btn)
        lay.addLayout(bottom)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._render)
        self.refresh_account()
        self._render()

    # ------------------------------------------------------------------ account
    def refresh_account(self):
        if auth.has_profile_login():
            self.account.setText("signed in")
            self.sign_btn.setText("Sign out")
        elif auth.env_credentials():
            self.account.setText(f"using {auth.env_credentials()}")
            self.sign_btn.setText("Sign in")
        else:
            self.account.setText("not signed in")
            self.sign_btn.setText("Sign in with Claude")

    def _sign_in_out(self):
        if auth.has_profile_login():
            auth.sign_out()
            self.refresh_account()
            self._add("notice", "Signed out of Claude.")
            return
        self.sign_in()

    def sign_in(self) -> bool:
        dlg = LoginDialog(self)
        ok = bool(dlg.exec())
        self.refresh_account()
        if ok:
            self._add("notice", f"Signed in to Claude — {dlg.message}." if dlg.message else "Signed in to Claude.")
        return ok

    # ------------------------------------------------------------------ chat
    def eventFilter(self, obj, ev):
        if obj is self.input and ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Return,
                                                                                     Qt.Key.Key_Enter):
            if not ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._send_or_stop(from_enter=True)
                return True
        return super().eventFilter(obj, ev)

    @property
    def busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _send_or_stop(self, from_enter: bool = False):
        if self.busy:
            if not from_enter:
                self.worker.cancel()
                self.send_btn.setEnabled(False)
            return
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.send(text)

    def send(self, text: str):
        if self.busy:
            return
        if not auth.is_signed_in() and not self.sign_in():
            return
        self.suggest_row.setVisible(False)
        self._add("user", text)
        self.messages.append({"role": "user", "content": text})
        self.main.begin_batch()
        self.worker = AssistantWorker(auth.make_client, self.messages, self.executor, self.model.currentData())
        self.worker.text.connect(self._on_text)
        self.worker.tool_started.connect(self._on_tool_started)
        self.worker.tool_finished.connect(self._on_tool_finished)
        self.worker.notice.connect(lambda m: self._add("notice", m))
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_done)
        self._set_busy(True)
        self.worker.start()

    def new_chat(self):
        if self.busy:
            return
        self.messages.clear()
        self.entries.clear()
        self.suggest_row.setVisible(True)
        self._render()

    def _set_busy(self, busy: bool):
        self.send_btn.setText("Stop" if busy else "Send")
        self.send_btn.setEnabled(True)
        self.model.setEnabled(not busy)
        self.busy_changed.emit(busy)

    def _on_text(self, chunk: str):
        if not self.entries or self.entries[-1]["role"] != "assistant":
            self.entries.append({"role": "assistant", "text": ""})
        self.entries[-1]["text"] += chunk
        self._render_timer.start()

    def _on_tool_started(self, name: str, args: str):
        self._add("tool", TOOL_LABELS.get(name, name), status="running", args=args)

    def _on_tool_finished(self, name: str, ok: bool, summary: str):
        for e in reversed(self.entries):
            if e["role"] == "tool" and e.get("status") == "running":
                e["status"] = "ok" if ok else "error"
                e["result"] = summary
                break
        self._render_timer.start()

    def _on_failed(self, kind: str, message: str):
        self._add("error", message)
        if kind == "auth":
            self.refresh_account()

    def _on_done(self):
        cancelled = self.worker.cancelled.is_set() if self.worker else False
        changed = self.main.end_batch()
        if cancelled:
            self._add("notice", "Stopped.")
        if changed:
            self._add("notice", "Changes applied — Ctrl+Z undoes everything from this request.")
        self._set_busy(False)
        self.worker = None

    # ------------------------------------------------------------------ transcript
    def _add(self, role: str, text: str, **extra):
        self.entries.append({"role": role, "text": text, **extra})
        self._render()

    def _render(self):
        if not self.entries:
            self.view.setHtml(
                "<div style='color:#9aa0ab; padding:8px'><p><b>Edit with Claude.</b></p>"
                "<p>Ask in plain words: <i>“cut the long pauses”</i>, <i>“add a title saying Welcome at the "
                "start”</i>, <i>“what happens around 0:45?”</i>. Claude can see your timeline and frames and "
                "make edits; each request is one undo step.</p></div>")
            return
        md = []
        for e in self.entries:
            role, text = e["role"], e["text"]
            if role == "user":
                md.append("> **You:** " + text.replace("\n", "\n> "))
            elif role == "assistant":
                if text.strip():
                    md.append(text.strip())
                elif e is self.entries[-1] and self.busy:
                    md.append("…")
            elif role == "tool":
                mark = {"running": "⏳", "ok": "✓", "error": "⚠"}[e.get("status", "running")]
                line = f"{mark} {text}"
                if e.get("status") == "error" and e.get("result"):
                    line += f": {e['result']}"
                md.append(f"_{line}_")
            elif role == "error":
                md.append(f"**⚠ {text}**")
            else:
                md.append(f"*{text}*")
        self.view.setMarkdown("\n\n".join(md))
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def shutdown(self):
        if self.busy:
            self.worker.cancel()
            self.worker.wait(5000)
