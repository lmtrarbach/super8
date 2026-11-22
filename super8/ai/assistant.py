"""The editing assistant: a streamed Messages API tool loop on a worker thread.

Text streams to the chat panel as it is generated; every tool call is handed to the GUI thread (the
tools edit the live project) and the worker waits for its result. The conversation is kept in memory
for the open project.
"""
from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .tools import TOOLS, ToolError

MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
}
DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallbacks (the "default" routing) exist for these; Haiku has none.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
EFFORT = "medium"
MAX_TOKENS = 64000
MAX_ROUNDS = 40  # tool round-trips per request

SYSTEM_PROMPT = """You are the editing assistant inside Super8, a non-linear video editor. The user is \
editing a project and talks to you in a side panel; you can read and change their project with tools.

How the editor works:
- One sequence with video tracks V1..Vn (V1 is the bottom layer; higher tracks draw on top) and audio \
tracks A1..An (mixed). Times are in seconds; edits snap to the frame grid.
- Video with sound appears as a linked video clip and audio clip; moving or trimming one moves its \
partner. Placing or moving a clip overwrites whatever it lands on.
- Titles are graphics clips made from templates and can be animated in and out.
- Every change you make for one request is grouped into a single undo step, so the user can undo all \
of it with Ctrl+Z.

How to work:
- Call get_project before you edit, and use its ids; never invent ids. Use view_frames to look at the \
footage when content matters (what is shown, where a title fits, whether an edit looks right).
- For removing pauses or rambling, use find_silences, then cut_ranges; say how much time was saved.
- Make the changes the user asked for. If a request is ambiguous in a way that matters (which clip, \
which part), ask one short question instead of guessing.
- Keep replies short and concrete: what you changed and where (timecodes), or the answer. Use plain \
text with occasional bullet points; no headings.
- You cannot export, import new files, or play audio; tell the user how to do those in Super8 \
(File > Import Media / Export Media, Space to play)."""


@dataclass
class ToolCall:
    name: str
    args: object
    event: threading.Event = field(default_factory=threading.Event)
    result: object = None
    is_error: bool = False


class GuiExecutor(QObject):
    """Lives on the GUI thread; runs tool calls posted from the worker thread."""

    requested = pyqtSignal(object)

    def __init__(self, toolkit):
        super().__init__()
        self.toolkit = toolkit
        self.requested.connect(self._run)

    def _run(self, call: ToolCall):
        try:
            call.result = self.toolkit.execute(call.name, call.args)
        except ToolError as e:
            call.result, call.is_error = f"Error: {e}", True
        except Exception as e:  # a bug on our side: report it to the model rather than crash the app
            traceback.print_exc()
            call.result, call.is_error = f"Internal error in {call.name}: {e}", True
        finally:
            call.event.set()

    def run(self, name: str, args, cancelled: threading.Event) -> tuple[object, bool]:
        """Called from the worker thread: post the call and wait for the GUI to finish it."""
        call = ToolCall(name, args)
        self.requested.emit(call)
        while not call.event.wait(0.1):
            if cancelled.is_set():
                call.event.wait(30)  # a started tool finishes; we only stop waiting for the next one
                break
        return call.result, call.is_error


def summarize_args(args) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for k, v in args.items():
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        parts.append(f"{k}={s[:40] + '…' if len(s) > 40 else s}")
    return ", ".join(parts)


def summarize_result(result) -> str:
    if isinstance(result, list):
        n = sum(1 for b in result if b.get("type") == "image")
        return f"{n} frame(s)"
    s = str(result)
    return s if len(s) <= 140 else s[:140] + "…"


class AssistantWorker(QThread):
    """Runs one user request to completion (possibly many tool rounds)."""

    text = pyqtSignal(str)
    tool_started = pyqtSignal(str, str)
    tool_finished = pyqtSignal(str, bool, str)
    notice = pyqtSignal(str)
    failed = pyqtSignal(str, str)  # kind (auth|permission|rate|network|model|api|internal), message

    def __init__(self, client_factory, messages: list, executor: GuiExecutor, model: str):
        super().__init__()
        self.client_factory = client_factory
        self.messages = messages  # shared conversation; only touched by this thread while running
        self.executor = executor
        self.model = model
        self.cancelled = threading.Event()

    def cancel(self):
        self.cancelled.set()

    def request_params(self) -> dict:
        params = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            tools=TOOLS,
            messages=self.messages,
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},  # tools + system + history are a stable prefix
        )
        if self.model in FALLBACK_MODELS:
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        return params

    def run(self):
        import anthropic
        try:
            client = self.client_factory()
        except Exception as e:
            self.failed.emit("auth", f"Could not use your Claude sign-in: {e}")
            return
        json_retries = 0
        rounds = 0
        while not self.cancelled.is_set():
            rounds += 1
            if rounds > MAX_ROUNDS:
                self.notice.emit("Stopped after many steps; ask me to continue if needed.")
                return
            try:
                with client.beta.messages.stream(**self.request_params()) as stream:
                    for event in stream:
                        if self.cancelled.is_set():
                            break
                        if event.type == "text":
                            self.text.emit(event.text)
                    if self.cancelled.is_set():
                        return  # the unfinished turn is not added, so the history stays valid
                    response = stream.get_final_message()
                json_retries = 0
            except ValueError:
                # tool input the SDK could not parse at all; no tool_use_id to answer, so re-issue
                json_retries += 1
                if json_retries > 2:
                    self.failed.emit("api", "Claude's reply could not be read (invalid tool input).")
                    return
                continue
            except anthropic.AuthenticationError:
                self.failed.emit("auth", "Your Claude sign-in has expired or is invalid. Sign in again.")
                return
            except anthropic.PermissionDeniedError as e:
                self.failed.emit("permission", f"Your Claude account can't use this: {e.message}")
                return
            except anthropic.NotFoundError:
                self.failed.emit("model", f"{MODELS.get(self.model, self.model)} isn't available to your "
                                          "account. Pick another model.")
                return
            except anthropic.RateLimitError:
                self.failed.emit("rate", "Rate limit reached on your Claude account. Try again in a moment.")
                return
            except anthropic.APIStatusError as e:
                self.failed.emit("api", f"Claude API error {e.status_code}: {e.message}")
                return
            except anthropic.APIConnectionError:
                self.failed.emit("network", "Couldn't reach Claude. Check your internet connection.")
                return
            except Exception as e:
                traceback.print_exc()
                self.failed.emit("internal", f"Unexpected error: {e}")
                return

            # keep the full content (thinking, fallback and tool_use blocks) exactly as returned
            self.messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "refusal":
                self.messages.pop()  # never replay a declined turn
                self.notice.emit("Claude declined this request.")
                return
            if not tool_uses:
                return  # end_turn: answer complete
            if response.stop_reason == "max_tokens":
                self.messages.pop()  # a truncated tool input parses as a partial object: don't run it
                self.notice.emit("The reply was cut off before finishing; try a smaller request.")
                return
            self.text.emit("\n\n")
            results = []
            for block in tool_uses:
                if self.cancelled.is_set():
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": "Cancelled by the user."})
                    continue
                self.tool_started.emit(block.name, summarize_args(block.input))
                if not isinstance(block.input, dict):
                    content, is_error = json.dumps({"INVALID_JSON": json.dumps(block.input)}), True
                else:
                    content, is_error = self.executor.run(block.name, block.input, self.cancelled)
                if content is None:
                    content, is_error = "Cancelled by the user.", True
                self.tool_finished.emit(block.name, not is_error, summarize_result(content))
                result = {"type": "tool_result", "tool_use_id": block.id, "content": content}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            self.messages.append({"role": "user", "content": results})  # all results in one message
            if self.cancelled.is_set():
                return
