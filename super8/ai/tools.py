"""Editor tools the AI assistant can call. Every tool runs on the GUI thread and acts on the open project.

Inputs come from the model, so each one is validated against its schema before it runs (and clip ids,
media ids and ranges are checked against the project): with eager input streaming the API no longer
validates tool input for us.
"""
from __future__ import annotations

import base64
import json
import math

import numpy as np
from PyQt6.QtCore import QBuffer, QIODevice
from PyQt6.QtGui import QImage

from ..graphics import ANIMATIONS, BACKGROUNDS, ROLLS, all_presets
from ..model import carve, ripple_ranges, split_all_at, split_with_links
from ..transform import ASPECTS, FIT_MODES, RESOLUTIONS, clamp_crop, ratio_text, size_for
from ..transitions import AUDIO_TRANSITIONS, VIDEO_TRANSITIONS


class ToolError(Exception):
    """A problem the model can fix (bad id, out of range...): returned to it as an error result."""


# ------------------------------------------------------------------ schemas
def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


NUM = {"type": "number"}
SECONDS = {"type": "number", "minimum": 0, "description": "Timeline time in seconds."}

CLIP_PROPS = {
    "opacity": {"type": "number", "minimum": 0, "maximum": 1},
    "scale": {"type": "number", "minimum": 0.01, "maximum": 20, "description": "1 = the size the fit mode gives."},
    "fit": {"type": "string", "enum": list(FIT_MODES),
            "description": "How a picture fills the frame: fit (letterbox), fill (cover, cropping overflow), "
                           "stretch, none (original pixel size)."},
    "scale_x": {"type": "number", "minimum": 0.01, "maximum": 20, "description": "Extra width factor."},
    "scale_y": {"type": "number", "minimum": 0.01, "maximum": 20, "description": "Extra height factor."},
    "rotation": {"type": "number", "minimum": -3600, "maximum": 3600, "description": "Degrees, clockwise."},
    "flip_h": {"type": "boolean"}, "flip_v": {"type": "boolean"},
    "crop_l": {"type": "number", "minimum": 0, "maximum": 0.95, "description": "Fraction cut from the left."},
    "crop_r": {"type": "number", "minimum": 0, "maximum": 0.95},
    "crop_t": {"type": "number", "minimum": 0, "maximum": 0.95},
    "crop_b": {"type": "number", "minimum": 0, "maximum": 0.95},
    "pos_x": {"type": "number", "description": "Horizontal offset from centre, in sequence pixels."},
    "pos_y": {"type": "number", "description": "Vertical offset from centre, in sequence pixels (down is +)."},
    "volume": {"type": "number", "minimum": 0, "maximum": 4, "description": "Audio gain, 1 = unchanged."},
    "speed": {"type": "number", "minimum": 0.0625, "maximum": 16,
              "description": "Playback rate; the clip's length changes so the same source plays."},
    "fade_in": {"type": "number", "minimum": 0},
    "fade_out": {"type": "number", "minimum": 0},
    "denoise": {"type": "number", "minimum": 0, "maximum": 1,
                "description": "Noise reduction strength for audio clips (0 off, 0.3 light, 0.6 medium, 0.85 strong)."},
    "text": {"type": "string"}, "subtext": {"type": "string"},
    "font_size": {"type": "number", "minimum": 6, "maximum": 800},
    "font_family": {"type": "string"}, "bold": {"type": "boolean"}, "italic": {"type": "boolean"},
    "align": {"type": "string", "enum": ["left", "center", "right"]},
    "color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "sub_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "outline_width": {"type": "number", "minimum": 0, "maximum": 60},
    "outline_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "shadow": {"type": "number", "minimum": 0, "maximum": 1},
    "bg_style": {"type": "string", "enum": list(BACKGROUNDS)},
    "bg_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "bg_opacity": {"type": "number", "minimum": 0, "maximum": 1},
    "accent_color": {"type": "string", "pattern": "^#[0-9a-fA-F]{6}$"},
    "anim_in": {"type": "string", "enum": list(ANIMATIONS)},
    "anim_in_dur": {"type": "number", "minimum": 0.04, "maximum": 30},
    "anim_out": {"type": "string", "enum": list(ANIMATIONS)},
    "anim_out_dur": {"type": "number", "minimum": 0.04, "maximum": 30},
    "roll": {"type": "string", "enum": list(ROLLS)},
}
TITLE_ONLY = {"text", "subtext", "font_size", "font_family", "bold", "italic", "align", "color", "sub_color",
              "outline_width", "outline_color", "shadow", "bg_style", "bg_color", "bg_opacity", "accent_color",
              "anim_in", "anim_in_dur", "anim_out", "anim_out_dur", "roll"}
AUDIO_ONLY = {"volume", "denoise"}
VISUAL_ONLY = {"opacity", "scale", "pos_x", "pos_y", "rotation"}
PICTURE_ONLY = {"fit", "scale_x", "scale_y", "flip_h", "flip_v", "crop_l", "crop_r", "crop_t", "crop_b"}

TOOLS: list[dict] = [
    {
        "name": "get_project",
        "description": "Read the open project: sequence settings, playhead, selection, media library, every "
                       "track and clip with its properties, silence marks, and the available transitions, "
                       "animations and graphics templates. Call this before editing and again after big "
                       "changes; ids are needed by the other tools.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "view_frames",
        "description": "Render what the program monitor shows at up to 6 timeline times and return them as "
                       "images, so you can see the footage, titles and composition. Use it to understand "
                       "content, pick good moments, or check your edits.",
        "input_schema": _obj({"times": {"type": "array", "items": SECONDS, "minItems": 1, "maxItems": 6}},
                             ["times"]),
    },
    {
        "name": "add_clip",
        "description": "Place a media item from the library on the timeline. Video with sound adds a linked "
                       "video clip and audio clip. Whatever the new clip covers on that track is overwritten.",
        "input_schema": _obj({
            "media_id": {"type": "string"},
            "start": SECONDS,
            "track": {"type": "string", "description": "Track name such as V1, V2 or A1 (default V1/A1)."},
        }, ["media_id", "start"]),
    },
    {
        "name": "move_clip",
        "description": "Move a clip (and its linked clips) to a new start time and optionally to another track "
                       "of the same kind. Overwrites whatever it lands on.",
        "input_schema": _obj({
            "clip_id": {"type": "string"}, "start": SECONDS,
            "track": {"type": "string", "description": "Optional destination track name, e.g. V2."},
        }, ["clip_id", "start"]),
    },
    {
        "name": "trim_clip",
        "description": "Change where a clip (and its linked clips) starts and/or ends on the timeline, keeping "
                       "the footage in place (a trim, like dragging the clip's edges).",
        "input_schema": _obj({
            "clip_id": {"type": "string"},
            "new_start": SECONDS, "new_end": SECONDS,
        }, ["clip_id"]),
    },
    {
        "name": "split",
        "description": "Cut clips at a timeline time: the listed clips, or every clip on every track there.",
        "input_schema": _obj({
            "time": SECONDS,
            "clip_ids": {"type": "array", "items": {"type": "string"}},
        }, ["time"]),
    },
    {
        "name": "delete_clips",
        "description": "Remove clips. With ripple=true, later clips on the same tracks move left to close the "
                       "gaps. Linked clips are not removed unless listed.",
        "input_schema": _obj({
            "clip_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "ripple": {"type": "boolean"},
        }, ["clip_ids"]),
    },
    {
        "name": "set_clip_properties",
        "description": "Change properties of one clip. Visual: opacity, scale, pos_x, pos_y, rotation. Video "
                       "and image clips also: fit, scale_x, scale_y, flip_h, flip_v, crop_l/r/t/b. Audio: "
                       "volume, denoise. Both: speed, fade_in, fade_out. Titles (graphics) also take text, "
                       "subtext, fonts, colours, background, animations and roll.",
        "input_schema": _obj({
            "clip_id": {"type": "string"},
            "properties": {"type": "object", "properties": CLIP_PROPS, "additionalProperties": False,
                           "minProperties": 1},
        }, ["clip_id", "properties"]),
    },
    {
        "name": "add_title",
        "description": "Add a text graphic from a template (see get_project for template ids, e.g. basic, "
                       "lower_bar, lower_box, caption, chapter, credits). Optionally set its text, timing, "
                       "track and any title property.",
        "input_schema": _obj({
            "template": {"type": "string"},
            "start": SECONDS,
            "duration": {"type": "number", "minimum": 0.1, "maximum": 3600},
            "text": {"type": "string"},
            "subtext": {"type": "string"},
            "track": {"type": "string", "description": "Video track name; default: a free track on top."},
            "properties": {"type": "object", "properties": {k: CLIP_PROPS[k] for k in TITLE_ONLY | VISUAL_ONLY},
                           "additionalProperties": False},
        }, ["template", "start"]),
    },
    {
        "name": "add_transition",
        "description": "Put a transition on a clip edge. side=in: at the clip's start (a cut from the clip "
                       "before it, or fading in from below if there is none); side=out: at its end when "
                       "nothing follows. kind=none removes it. Audio clips take audio transitions.",
        "input_schema": _obj({
            "clip_id": {"type": "string"},
            "side": {"type": "string", "enum": ["in", "out"]},
            "kind": {"type": "string", "enum": ["none"] + list(VIDEO_TRANSITIONS) + list(AUDIO_TRANSITIONS)},
            "duration": {"type": "number", "minimum": 0.04, "maximum": 30},
            "include_linked": {"type": "boolean", "description": "Also apply to linked clips (default true)."},
        }, ["clip_id", "side", "kind"]),
    },
    {
        "name": "find_silences",
        "description": "Measure the audio mix and list silent stretches (pauses, dead air) between start and "
                       "end (default: whole sequence). Marks them in red on the timeline for the user. Use "
                       "cut_ranges to remove or speed them up.",
        "input_schema": _obj({
            "start": SECONDS, "end": SECONDS,
            "threshold_db": {"type": "number", "minimum": -90, "maximum": 0,
                             "description": "Level below which audio counts as silent; omit for automatic."},
            "min_silence": {"type": "number", "minimum": 0.05, "maximum": 30,
                            "description": "Shortest pause to report (default 0.6 s)."},
            "padding": {"type": "number", "minimum": 0, "maximum": 2,
                        "description": "Audio kept on each side of a cut (default 0.15 s)."},
        }, []),
    },
    {
        "name": "cut_ranges",
        "description": "Remove (mode=cut, closing the gaps) or speed up (mode=speed) timeline ranges on ALL "
                       "tracks together so picture and sound stay in sync. Use for silences, filler or "
                       "unwanted sections.",
        "input_schema": _obj({
            "ranges": {"type": "array", "minItems": 1, "items": {
                "type": "array", "items": SECONDS, "minItems": 2, "maxItems": 2}},
            "mode": {"type": "string", "enum": ["cut", "speed"]},
            "speed_factor": {"type": "number", "minimum": 1.5, "maximum": 16},
        }, ["ranges", "mode"]),
    },
    {
        "name": "set_sequence",
        "description": "Change the sequence frame: aspect ratio (e.g. 9:16 for Shorts/Reels/TikTok), "
                       "resolution (short side in pixels) and/or frame rate. Clips keep their fit mode and "
                       "re-fit; set fit=fill on clips that should fill the new shape.",
        "input_schema": _obj({
            "aspect": {"type": "string", "enum": [a[0].split()[0] for a in ASPECTS]},
            "resolution": {"type": "number", "enum": RESOLUTIONS},
            "fps": {"type": "number", "enum": [23.976, 24, 25, 29.97, 30, 50, 59.94, 60]},
        }, []),
    },
    {
        "name": "add_track",
        "description": "Add an empty video or audio track.",
        "input_schema": _obj({"kind": {"type": "string", "enum": ["video", "audio"]}}, ["kind"]),
    },
    {
        "name": "show",
        "description": "Move the playhead and/or select clips so the user sees what you are talking about.",
        "input_schema": _obj({
            "time": SECONDS,
            "clip_ids": {"type": "array", "items": {"type": "string"}},
        }, []),
    },
]

for _t in TOOLS:  # client tools on a streamed request: stream their input as it is generated
    _t["eager_input_streaming"] = True


# ------------------------------------------------------------------ validation
def validate(value, schema: dict, path: str = "input") -> None:
    """Check a value against the (small) JSON-schema subset used above. Raises ToolError."""
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            raise ToolError(f"{path} must be an object")
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                raise ToolError(f"{path}.{k} is required")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise ToolError(f"{path} has unknown field(s): {', '.join(sorted(extra))}")
        if len(value) < schema.get("minProperties", 0):
            raise ToolError(f"{path} needs at least {schema['minProperties']} field(s)")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(value, list):
            raise ToolError(f"{path} must be an array")
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            raise ToolError(f"{path} has {len(value)} items (allowed {schema.get('minItems', 0)}-"
                            f"{schema.get('maxItems', 'any')})")
        for i, v in enumerate(value):
            validate(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ToolError(f"{path} must be a number")
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ToolError(f"{path}={value} is out of range [{schema.get('minimum')}, {schema.get('maximum')}]")
        if "enum" in schema and not any(abs(value - e) < 1e-9 for e in schema["enum"]):
            raise ToolError(f"{path}={value} is not one of {schema['enum']}")
    elif t == "string":
        if not isinstance(value, str):
            raise ToolError(f"{path} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolError(f"{path}={value!r} is not one of {schema['enum']}")
        if "pattern" in schema:
            import re
            if not re.match(schema["pattern"], value):
                raise ToolError(f"{path}={value!r} must match {schema['pattern']}")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise ToolError(f"{path} must be true or false")


SCHEMAS = {t["name"]: t["input_schema"] for t in TOOLS}


# ------------------------------------------------------------------ execution
def _r(x: float) -> float:
    return round(float(x), 3)


class Toolkit:
    """Runs tools against the main window's project. Must be called on the GUI thread."""

    FRAME_WIDTH = 768

    def __init__(self, main):
        self.main = main

    @property
    def project(self):
        return self.main.project

    def execute(self, name: str, args) -> list[dict] | str:
        """Returns tool_result content (a string or content blocks). Raises ToolError for model mistakes."""
        if name not in SCHEMAS:
            raise ToolError(f"unknown tool {name!r}")
        validate(args, SCHEMAS[name])
        result = getattr(self, "t_" + name)(**args)
        self.main.changed()
        return result if isinstance(result, list) else json.dumps(result, ensure_ascii=False)

    # helpers ---------------------------------------------------------------
    def _clip(self, cid: str):
        tr, c = self.project.find_clip(cid)
        if c is None:
            raise ToolError(f"no clip with id {cid!r} (call get_project for current ids)")
        return tr, c

    def _track(self, name: str, kind: str | None = None):
        for tr in self.project.tracks():
            if tr.name.lower() == name.lower():
                if kind and tr.kind != kind:
                    raise ToolError(f"track {name} is a {tr.kind} track, not {kind}")
                return tr
        raise ToolError(f"no track named {name!r}; tracks are "
                        + ", ".join(t.name for t in self.project.tracks()))

    def _clip_json(self, c) -> dict:
        p = self.project
        m = p.media.get(c.media_id) if c.media_id else None
        d = {"id": c.id, "kind": c.kind, "start": _r(c.start), "end": _r(c.end), "duration": _r(c.duration)}
        if m:
            d.update(media_id=m.id, media=m.name, source_in=_r(c.in_point), source_out=_r(c.src_end))
        if c.link:
            d["linked_to"] = [x.id for x in p.linked(c) if x is not c]
        defaults = {"speed": 1.0, "opacity": 1.0, "scale": 1.0, "pos_x": 0.0, "pos_y": 0.0, "volume": 1.0,
                    "fade_in": 0.0, "fade_out": 0.0, "denoise": 0.0, "scale_x": 1.0, "scale_y": 1.0,
                    "rotation": 0.0, "crop_l": 0.0, "crop_r": 0.0, "crop_t": 0.0, "crop_b": 0.0}
        for k, dv in defaults.items():
            v = getattr(c, k)
            if abs(v - dv) > 1e-9 and not (c.kind == "audio" and k in VISUAL_ONLY | PICTURE_ONLY) \
                    and not (c.kind != "audio" and k in AUDIO_ONLY):
                d[k] = _r(v)
        if c.kind in ("video", "image"):
            d["fit"] = c.fit
            for k in ("flip_h", "flip_v"):
                if getattr(c, k):
                    d[k] = True
        if c.tin:
            d["transition_in"] = {"kind": c.tin, "duration": _r(c.tin_dur)}
        if c.tout:
            d["transition_out"] = {"kind": c.tout, "duration": _r(c.tout_dur)}
        if c.kind == "title":
            d.update({k: getattr(c, k) for k in ("text", "subtext", "font_size", "font_family", "align", "color",
                                                  "bg_style", "anim_in", "anim_out", "roll", "pos_x", "pos_y")})
        return d

    # tools -----------------------------------------------------------------
    def t_get_project(self):
        p, c = self.project, self.main.timeline.canvas
        return {
            "sequence": {"width": p.width, "height": p.height, "fps": p.fps, "duration": _r(p.duration()),
                         "aspect": ratio_text(p.width, p.height)},
            "playhead": _r(self.main.playhead),
            "selected_clip_ids": sorted(c.selection),
            "media": [{"id": m.id, "name": m.name, "kind": m.kind, "duration": _r(m.duration),
                       "video": m.has_video, "audio": m.has_audio,
                       **({"size": f"{m.width}x{m.height}"} if m.has_video else {})} for m in p.media.values()],
            "tracks": [{"name": t.name, "kind": t.kind, "hidden_or_muted": t.muted,
                        "clips": [self._clip_json(x) for x in t.clips]}
                       for t in p.video_tracks[::-1] + p.audio_tracks],
            "silence_marks": [[_r(a), _r(b)] for a, b in c.markers],
            "notes": "Video tracks are listed top layer first; V1 is the bottom layer. Times are seconds.",
            "video_transitions": list(VIDEO_TRANSITIONS), "audio_transitions": list(AUDIO_TRANSITIONS),
            "title_templates": {k: v.get("name", k) for k, v in all_presets().items()},
            "title_animations": [a for a in ANIMATIONS if a],
        }

    def t_view_frames(self, times):
        from ..compositor import Compositor
        p = self.project
        w = self.FRAME_WIDTH // 2 * 2
        h = max(2, int(round(w * p.height / p.width)) // 2 * 2)
        comp = Compositor(self.main.audio_cache)
        blocks: list[dict] = []
        try:
            for t in times:
                arr = np.ascontiguousarray(comp.render(p.clone(), float(t), w, h))
                img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                img.save(buf, "JPEG", 82)
                blocks.append({"type": "text", "text": f"Frame at {t:.2f} s:"})
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(bytes(buf.data())).decode()}})
        finally:
            comp.close()
        return blocks

    def t_add_clip(self, media_id, start, track=None):
        p = self.project
        m = p.media.get(media_id)
        if m is None:
            raise ToolError(f"no media with id {media_id!r}")
        tr = self._track(track) if track else None
        before = {c.id for _, c in p.all_clips()}
        self.main.place_media(media_id, start, tr)
        new = [c for _, c in self.project.all_clips() if c.id not in before]
        return {"added": [self._clip_json(c) for c in new]}

    def t_move_clip(self, clip_id, start, track=None):
        p = self.project
        tr0, c = self._clip(clip_id)
        group = p.linked(c)
        dt = p.frame(start) - c.start
        if min(x.start for x in group) + dt < 0:
            raise ToolError("that would move a linked clip before 0 s")
        if track:
            dest = self._track(track, tr0.kind)
            if dest is not tr0:
                tr0.clips.remove(c)
                dest.clips.append(c)
        for x in group:
            x.start += dt
        ids = {x.id for x in group}
        for x in group:
            t, _ = p.find_clip(x.id)
            t.sort()
            carve(t, x.start, x.end, exclude=ids)
        return {"moved": [self._clip_json(x) for x in group if p.find_clip(x.id)[1]]}

    def t_trim_clip(self, clip_id, new_start=None, new_end=None):
        p = self.project
        _, c = self._clip(clip_id)
        if new_start is None and new_end is None:
            raise ToolError("give new_start and/or new_end")
        s = p.frame(new_start) if new_start is not None else c.start
        e = p.frame(new_end) if new_end is not None else c.end
        if e - s < 1 / p.fps:
            raise ToolError("the clip would be shorter than one frame")
        group = p.linked(c)
        ds, de = s - c.start, e - c.end
        for x in group:  # check the source has enough footage for every linked clip
            m = p.media.get(x.media_id) if x.media_id else None
            if m is not None and m.finite:
                if x.in_point + ds * x.speed < -1e-6:
                    raise ToolError(f"clip {x.id} has only {x.in_point / x.speed:.2f}s of footage before its start")
                if x.src_end + de * x.speed > m.duration + 1e-6:
                    raise ToolError(f"clip {x.id} has only {(m.duration - x.src_end) / x.speed:.2f}s of "
                                    "footage after its end")
        ids = {x.id for x in group}
        for x in group:
            x.in_point += ds * x.speed
            x.start += ds
            x.duration += de - ds
            x.fade_in, x.fade_out = min(x.fade_in, x.duration), min(x.fade_out, x.duration)
            t, _ = p.find_clip(x.id)
            carve(t, x.start, x.end, exclude=ids)
        return {"trimmed": [self._clip_json(x) for x in group]}

    def t_split(self, time, clip_ids=None):
        p = self.project
        t = p.frame(time)
        before = {c.id for _, c in p.all_clips()}
        if clip_ids:
            for cid in clip_ids:
                _, c = self._clip(cid)
                if not (c.start < t < c.end):
                    raise ToolError(f"clip {cid} does not span {t:.3f}s")
                split_with_links(p, c, t)
        else:
            split_all_at(p, t)
        new = [c.id for _, c in p.all_clips() if c.id not in before]
        return {"time": _r(t), "new_clip_ids": new}

    def t_delete_clips(self, clip_ids, ripple=False):
        from ..model import remove_clips
        for cid in clip_ids:
            self._clip(cid)
        remove_clips(self.project, set(clip_ids), ripple)
        self.main.timeline.canvas.selection -= set(clip_ids)
        return {"deleted": clip_ids, "ripple": ripple, "sequence_duration": _r(self.project.duration())}

    def t_set_clip_properties(self, clip_id, properties):
        tr, c = self._clip(clip_id)
        bad = [k for k in properties if (k in TITLE_ONLY and c.kind != "title")
               or (k in AUDIO_ONLY and c.kind != "audio") or (k in VISUAL_ONLY and c.kind == "audio")
               or (k in PICTURE_ONLY and c.kind not in ("video", "image"))
               or (k == "speed" and c.kind not in ("video", "audio"))]
        if bad:
            raise ToolError(f"{', '.join(bad)} cannot be set on a {c.kind} clip")
        props = dict(properties)
        if "speed" in props:
            self.main._set_speed(c, props.pop("speed"))  # keeps the source range, resizes the clip
            _, c = self._clip(clip_id)
        for k, v in props.items():
            setattr(c, k, int(round(v)) if k == "font_size" else v)
            if k == "denoise" and c.media_id:
                self.main.audio_cache.request(c.media_id, v)
        c.fade_in, c.fade_out = min(c.fade_in, c.duration), min(c.fade_out, c.duration)
        c.crop_l, c.crop_r = clamp_crop(c.crop_l, c.crop_r)
        c.crop_t, c.crop_b = clamp_crop(c.crop_t, c.crop_b)
        return {"updated": self._clip_json(c)}

    def t_set_sequence(self, aspect=None, resolution=None, fps=None):
        if aspect is None and resolution is None and fps is None:
            raise ToolError("give aspect, resolution and/or fps")
        p = self.project
        if aspect is None:
            aw, ah = p.width, p.height
        else:
            aw, ah = next((w, h) for label, w, h in ASPECTS if label.split()[0] == aspect)
        short = int(resolution) if resolution is not None else min(p.width, p.height)
        w, h = size_for(aw, ah, short) if aspect is not None else (
            (int(round(short * aw / ah / 2)) * 2, short) if aw >= ah else (short, int(round(short * ah / aw / 2)) * 2))
        self.main.set_sequence_format(w, h, float(fps) if fps else None)
        return {"sequence": {"width": p.width, "height": p.height, "fps": p.fps,
                             "aspect": ratio_text(p.width, p.height)}}

    def t_add_title(self, template, start, duration=None, text=None, subtext=None, track=None, properties=None):
        if template not in all_presets():
            raise ToolError(f"unknown template {template!r}; available: {', '.join(all_presets())}")
        tr = self._track(track, "video") if track else None
        c = self.main.add_title(start, tr, preset=template, focus_text=False)
        if duration is not None:
            c.duration = self.project.frame(duration)
        for k, v in (("text", text), ("subtext", subtext)):
            if v is not None:
                setattr(c, k, v)
        for k, v in (properties or {}).items():
            setattr(c, k, int(round(v)) if k == "font_size" else v)
        t, _ = self.project.find_clip(c.id)
        carve(t, c.start, c.end, exclude={c.id})
        return {"added": self._clip_json(c)}

    def t_add_transition(self, clip_id, side, kind, duration=1.0, include_linked=True):
        tr, c = self._clip(clip_id)
        if kind == "none":
            targets = self.project.linked(c) if include_linked else [c]
            for x in targets:
                if side == "in":
                    x.tin = ""
                else:
                    x.tout = ""
            return {"removed": side, "clips": [x.id for x in targets]}
        if kind in AUDIO_TRANSITIONS and tr.kind == "video":
            raise ToolError(f"{kind} is an audio transition; video clips take {', '.join(VIDEO_TRANSITIONS)}")
        self.main.apply_transition(clip_id, side, kind, only_track=not include_linked, duration=duration,
                                   commit=False)
        return {"clip": self._clip_json(self._clip(clip_id)[1])}

    def t_find_silences(self, start=None, end=None, threshold_db=None, min_silence=0.6, padding=0.15):
        from ..audiofx import auto_threshold, envelope_db, find_silences
        from ..compositor import mix_audio
        p = self.project
        t0 = 0.0 if start is None else start
        t1 = p.duration() if end is None else min(end, p.duration())
        if t1 - t0 < 0.1:
            raise ToolError("the range is empty (is there audio on the timeline?)")
        missing = self.main._audio_missing()
        if missing:
            raise ToolError("audio is still being decoded for: " + ", ".join(missing) + "; try again shortly")
        proj = p.clone()
        db = envelope_db(lambda t, n: mix_audio(proj, self.main.audio_cache, t, n), t0, t1, p.sample_rate)
        thr = auto_threshold(db) if threshold_db is None else threshold_db
        ranges = find_silences(db, t0, thr, min_silence, padding, p.fps)
        self.main.timeline.canvas.markers = list(ranges)
        total = sum(b - a for a, b in ranges)
        return {"threshold_db": round(float(thr), 1), "count": len(ranges), "total_seconds": _r(total),
                "range": [_r(t0), _r(t1)], "silences": [[_r(a), _r(b)] for a, b in ranges]}

    def t_cut_ranges(self, ranges, mode, speed_factor=4.0):
        p = self.project
        clean = []
        for a, b in ranges:
            a, b = p.frame(min(a, b)), p.frame(max(a, b))
            if b - a >= 1 / p.fps:
                clean.append((a, b))
        if not clean:
            raise ToolError("no range is at least one frame long")
        clean.sort()
        for (a0, b0), (a1, b1) in zip(clean, clean[1:]):
            if a1 < b0:
                raise ToolError(f"ranges overlap: [{a0}, {b0}] and [{a1}, {b1}]")
        before = p.duration()
        saved = ripple_ranges(p, clean, "cut" if mode == "cut" else "speed", speed_factor)
        self.main.timeline.canvas.markers = []
        self.main.timeline.canvas.selection.clear()
        return {"ranges": len(clean), "seconds_saved": _r(saved), "duration_before": _r(before),
                "duration_after": _r(p.duration())}

    def t_add_track(self, kind):
        t = self.project.add_track(kind)
        return {"added_track": t.name}

    def t_show(self, time=None, clip_ids=None):
        if clip_ids is not None:
            for cid in clip_ids:
                self._clip(cid)
            self.main.timeline.canvas.selection = set(clip_ids)
        if time is not None:
            self.main._move_playhead(self.project.frame(time))
        return {"playhead": _r(self.main.playhead), "selected": sorted(self.main.timeline.canvas.selection)}
