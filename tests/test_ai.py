"""AI assistant: toolkit, agent loop (scripted fake Claude), and sign-in (fake `ant`). No real API calls."""
import base64
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QImage

from super8.ai import assistant, auth
from super8.ai.tools import TOOLS, ToolError, Toolkit, validate


# ================================================================== toolkit
@pytest.fixture
def kit(win):
    win.place_media(win.ids["talk.mp4"], 0, None)
    return win, Toolkit(win)


def run(kit_, name, **args):
    out = kit_[1].execute(name, args)
    return json.loads(out) if isinstance(out, str) else out


def test_tool_schemas_are_strict_and_stream_eagerly():
    for t in TOOLS:
        s = t["input_schema"]
        assert s["type"] == "object" and s["additionalProperties"] is False and "required" in s, t["name"]
        assert t["eager_input_streaming"] is True


def test_validation_rejects_bad_model_input():
    schema = next(t for t in TOOLS if t["name"] == "set_clip_properties")["input_schema"]
    good = {"clip_id": "x", "properties": {"opacity": 0.5}}
    validate(good, schema)
    for bad in ({"clip_id": "x"}, {"clip_id": 3, "properties": {"opacity": 1}},
                {"clip_id": "x", "properties": {"opacity": 2}}, {"clip_id": "x", "properties": {}},
                {"clip_id": "x", "properties": {"opacity": True}},
                {"clip_id": "x", "properties": {"color": "red"}},
                {"clip_id": "x", "properties": {"anim_in": "spin"}},
                {"clip_id": "x", "properties": {"opacity": 1}, "extra": 1}):
        with pytest.raises(ToolError):
            validate(bad, schema)


def test_get_project_describes_everything(kit):
    w, _ = kit
    p = run(kit, "get_project")
    assert p["sequence"]["width"] == 640 and p["sequence"]["duration"] == pytest.approx(30, abs=0.1)
    assert {m["name"] for m in p["media"]} >= {"talk.mp4", "clip_a.mp4"}
    v1 = next(t for t in p["tracks"] if t["name"] == "V1")
    clip = v1["clips"][0]
    assert clip["media"] == "talk.mp4" and clip["linked_to"]
    assert "lower_bar" in p["title_templates"] and "dissolve" in p["video_transitions"]


def test_edit_tools(kit):
    w, _ = kit
    v = w.project.video_tracks[0].clips[0]
    added = run(kit, "add_clip", media_id=w.ids["clip_a.mp4"], start=30, track="V1")["added"]
    assert {c["kind"] for c in added} == {"video", "audio"}
    a_id = next(c["id"] for c in added if c["kind"] == "video")
    run(kit, "move_clip", clip_id=a_id, start=31)
    assert w.project.find_clip(a_id)[1].start == 31
    run(kit, "trim_clip", clip_id=v.id, new_start=2.0, new_end=20.0)
    v = w.project.find_clip(v.id)[1]
    assert (v.start, v.end, v.in_point) == (2.0, 20.0, 2.0)
    assert w.project.linked(v)[1].start == 2.0  # the linked audio followed
    with pytest.raises(ToolError, match="footage"):
        run(kit, "trim_clip", clip_id=v.id, new_end=40.0)  # beyond the source
    new = run(kit, "split", time=10.0)["new_clip_ids"]
    assert len(new) == 2
    run(kit, "delete_clips", clip_ids=new, ripple=True)
    assert w.project.find_clip(a_id)[1].start == pytest.approx(31 - 10)
    with pytest.raises(ToolError, match="cannot be set"):
        run(kit, "set_clip_properties", clip_id=v.id, properties={"volume": 0.5})  # video clip, audio prop
    with pytest.raises(ToolError, match="no clip"):
        run(kit, "move_clip", clip_id="nope", start=1)


def test_titles_transitions_and_properties(kit):
    w, _ = kit
    t = run(kit, "add_title", template="lower_bar", start=1.0, duration=3.0, text="Jane Doe",
            subtext="Host", properties={"anim_in": "typewriter"})["added"]
    c = w.project.find_clip(t["id"])[1]
    assert (c.text, c.subtext, c.anim_in, c.duration, c.bg_style) == ("Jane Doe", "Host", "typewriter", 3.0, "bar")
    run(kit, "set_clip_properties", clip_id=c.id, properties={"color": "#ff0000", "font_size": 70.4})
    assert c.color == "#ff0000" and c.font_size == 70
    v = w.project.video_tracks[0].clips[0]
    run(kit, "add_transition", clip_id=v.id, side="in", kind="dip_black", duration=0.5)
    assert v.tin == "dip_black" and w.project.linked(v)[1].tin == "crossfade"
    with pytest.raises(ToolError, match="audio transition"):
        run(kit, "add_transition", clip_id=v.id, side="in", kind="crossfade")
    run(kit, "add_transition", clip_id=v.id, side="in", kind="none")
    assert v.tin == "" and w.project.linked(v)[1].tin == ""


def test_silences_cut_and_view_frames(kit):
    w, _ = kit
    found = run(kit, "find_silences")
    assert found["count"] >= 6 and w.timeline.canvas.markers
    before = w.project.duration()
    res = run(kit, "cut_ranges", ranges=found["silences"], mode="cut")
    assert res["duration_after"] == pytest.approx(before - found["total_seconds"], abs=0.05)
    v = [(round(c.start, 3), round(c.end, 3)) for c in w.project.video_tracks[0].clips]
    a = [(round(c.start, 3), round(c.end, 3)) for c in w.project.audio_tracks[0].clips]
    assert v == a  # still in sync
    blocks = run(kit, "view_frames", times=[0.5, 3.0])
    imgs = [b for b in blocks if b["type"] == "image"]
    assert len(imgs) == 2 and imgs[0]["source"]["media_type"] == "image/jpeg"
    img = QImage.fromData(base64.b64decode(imgs[0]["source"]["data"]), "JPEG")
    assert img.width() == 768 and img.height() == 432


# ================================================================== agent loop (fake Claude)
def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


class FakeStream:
    def __init__(self, text, content, stop_reason):
        self.events = [SimpleNamespace(type="text", text=t) for t in text]
        self.final = SimpleNamespace(content=content, stop_reason=stop_reason)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_message(self):
        return self.final


class FakeClaude:
    """Plays back scripted turns and records every request."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params):
        self.requests.append(json.loads(json.dumps(params, default=lambda o: o.__dict__)))
        return FakeStream(*self.turns.pop(0))


def run_panel(win, fake, prompt, monkeypatch, model=None):
    monkeypatch.setattr(auth, "make_client", lambda: fake)
    monkeypatch.setattr(auth, "is_signed_in", lambda: True)
    panel = win.ai
    if model:
        panel.model.setCurrentIndex(panel.model.findData(model))
    panel.send(prompt)
    for _ in range(300):
        win.pump(0.02)
        if panel.worker is None:
            break
    assert panel.worker is None, "assistant did not finish"
    return panel


def test_agent_loop_edits_project_as_one_undo_step(win, monkeypatch):
    win.place_media(win.ids["talk.mp4"], 0, None)
    undo_depth = len(win.history.undo_stack)
    v = win.project.video_tracks[0].clips[0]
    fake = FakeClaude([
        (["Let me look. "], [block("text", text="Let me look. "),
                             block("tool_use", id="tu1", name="get_project", input={})], "tool_use"),
        (["Adding a title"], [block("tool_use", id="tu2", name="add_title",
                                    input={"template": "basic", "start": 1, "text": "Welcome"}),
                              block("tool_use", id="tu3", name="add_transition",
                                    input={"clip_id": v.id, "side": "in", "kind": "dissolve"})], "tool_use"),
        (["Done: title at 0:01 and a dissolve in."], [block("text", text="Done.")], "end_turn"),
    ])
    panel = run_panel(win, fake, "Add a welcome title", monkeypatch)
    titles = [c for t in win.project.video_tracks for c in t.clips if c.kind == "title"]
    assert titles and titles[0].text == "Welcome"
    assert win.project.find_clip(v.id)[1].tin == "dissolve"
    # history sent back: user, assistant(tool_use), user(tool_result), assistant(2 tools), user(2 results)
    msgs = fake.requests[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    results = msgs[4]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu2", "tu3"] and not any(r.get("is_error") for r in results)
    assert json.loads(msgs[2]["content"][0]["content"])["sequence"]["fps"] == 30
    # one undo step for the whole request
    assert len(win.history.undo_stack) == undo_depth + 1
    win.undo()
    assert not [c for t in win.project.video_tracks for c in t.clips if c.kind == "title"]
    assert any("Done" in e["text"] for e in panel.entries if e["role"] == "assistant")


def test_request_parameters(win, monkeypatch):
    fake = FakeClaude([(["Hi"], [block("text", text="Hi")], "end_turn")])
    run_panel(win, fake, "hello", monkeypatch, model="claude-opus-5-5")
    r = fake.requests[0]
    assert r["model"] == "claude-opus-5-5" and r["thinking"] == {"type": "adaptive"}
    assert r["output_config"] == {"effort": "medium"} and r["cache_control"] == {"type": "ephemeral"}
    assert r["fallbacks"] == "default" and r["betas"] == [assistant.FALLBACK_BETA]
    assert all(t["eager_input_streaming"] for t in r["tools"])
    fake = FakeClaude([(["Hi"], [block("text", text="Hi")], "end_turn")])
    win.ai.new_chat()
    run_panel(win, fake, "hello", monkeypatch, model="claude-haiku-5-5")
    assert "fallbacks" not in fake.requests[0]  # Haiku has no server-side fallback


def test_refusal_and_truncated_tool_input_are_not_executed(win, monkeypatch):
    win.place_media(win.ids["talk.mp4"], 0, None)
    n_clips = sum(1 for _ in win.project.all_clips())
    fake = FakeClaude([([], [block("tool_use", id="tx", name="delete_clips",
                                   input={"clip_ids": [win.project.video_tracks[0].clips[0].id]})], "max_tokens")])
    panel = run_panel(win, fake, "delete everything", monkeypatch)
    assert sum(1 for _ in win.project.all_clips()) == n_clips
    assert any("cut off" in e["text"] for e in panel.entries)
    assert panel.messages[-1]["role"] == "user"  # the truncated turn was not kept
    fake = FakeClaude([([], [block("text", text="")], "refusal")])
    panel = run_panel(win, fake, "something", monkeypatch)
    assert any("declined" in e["text"] for e in panel.entries)


def test_invalid_tool_input_is_reported_to_claude(win, monkeypatch):
    win.place_media(win.ids["talk.mp4"], 0, None)
    fake = FakeClaude([
        ([], [block("tool_use", id="t1", name="move_clip", input={"clip_id": "nope", "start": "soon"})],
         "tool_use"),
        (["Sorry."], [block("text", text="Sorry.")], "end_turn"),
    ])
    run_panel(win, fake, "move it", monkeypatch)
    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "must be a number" in result["content"]


def test_auth_error_asks_to_sign_in_again(win, monkeypatch):
    import anthropic

    class Boom(FakeClaude):
        def _stream(self, **params):
            raise anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)

    panel = run_panel(win, Boom([]), "hi", monkeypatch)
    assert any(e["role"] == "error" and "Sign in again" in e["text"] for e in panel.entries)
    assert not win.in_batch


# ================================================================== sign-in (fake ant)
FAKE_ANT = r'''#!{python}
import json, os, sys
cfg = os.environ["ANTHROPIC_CONFIG_DIR"]
assert "ANTHROPIC_API_KEY" not in os.environ
args = sys.argv[1:]
if args[:2] == ["auth", "logout"]:
    sys.exit(0)
assert args == ["auth", "login", "--no-browser", "--profile", "super8"], args
sys.stderr.write("Open this URL to authorize:\n\n  https://platform.claude.com/oauth/authorize?state=abc123\n\n")
sys.stderr.write("After authorizing, the page will display a code. Paste it here:\nCode: ")
sys.stderr.flush()
code = sys.stdin.readline().strip()
if code != "good#abc123":
    sys.stderr.write("error: invalid authorization code\n")
    sys.exit(1)
os.makedirs(os.path.join(cfg, "credentials"), exist_ok=True)
json.dump({{"type": "user_oauth"}}, open(os.path.join(cfg, "credentials", "super8.json"), "w"))
sys.stderr.write('✓ Logged in as jane@example.com (profile "super8").\n')
'''


@pytest.fixture
def fake_ant(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "ant"
    path.parent.mkdir()
    path.write_text(FAKE_ANT.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SNAP", raising=False)
    monkeypatch.setenv("PATH", f"{path.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-reach-ant")
    return str(path)


def drive_login(qapp, ant, code):
    from conftest import pump
    proc = auth.LoginProcess(ant)
    got = {}
    proc.url_ready.connect(lambda u: (got.setdefault("url", u), proc.submit_code(code)))
    proc.finished.connect(lambda ok, msg: got.update(ok=ok, msg=msg))
    proc.start()
    for _ in range(200):
        pump(qapp, 0.02)
        if "ok" in got:
            break
    return got


def test_sign_in_flow_with_ant(qapp, fake_ant, monkeypatch):
    assert auth.find_ant() == fake_ant
    assert not auth.has_profile_login()
    bad = drive_login(qapp, fake_ant, "wrong")
    assert bad["url"].startswith("https://platform.claude.com/oauth/authorize")
    assert bad["ok"] is False and "invalid authorization code" in bad["msg"]
    good = drive_login(qapp, fake_ant, "good#abc123")
    assert good["ok"] is True and good["msg"] == 'Logged in as jane@example.com (profile "super8")'
    assert auth.has_profile_login()
    # the login lives in Super8's private folder, never the shared ~/.config/anthropic
    assert auth.config_dir() == auth.Path(os.environ["XDG_CONFIG_HOME"]) / "super8" / "anthropic"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", "restored-after-test")  # make_client sets it; undo afterwards
    import anthropic
    seen = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: seen.update(kw) or "client")
    assert auth.make_client() == "client" and seen == {"profile": "super8"}
    assert os.environ["ANTHROPIC_CONFIG_DIR"] == str(auth.config_dir())
    auth.sign_out()
    assert not auth.has_profile_login()
