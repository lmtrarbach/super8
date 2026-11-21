"""Project data model and pure editing operations (no Qt here)."""
from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field, fields

MIN_CLIP = 1e-3  # seconds; smallest clip we allow to exist


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class MediaItem:
    id: str
    path: str
    name: str
    kind: str  # 'video' | 'audio' | 'image'
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False

    @property
    def finite(self) -> bool:
        """Images can be stretched forever; video/audio are limited by source length."""
        return self.kind != "image"


@dataclass
class Clip:
    id: str
    kind: str  # 'video' | 'audio' | 'image' | 'title'
    media_id: str | None
    start: float  # position on the timeline (s)
    duration: float  # length on the timeline (s)
    in_point: float = 0.0  # offset into the source (s)
    link: str | None = None  # clips sharing a link id move/trim/delete together
    speed: float = 1.0  # playback rate; source consumed = duration * speed
    # video properties
    opacity: float = 1.0
    scale: float = 1.0
    pos_x: float = 0.0  # offset in sequence pixels from centre
    pos_y: float = 0.0
    # audio properties
    volume: float = 1.0
    # both
    fade_in: float = 0.0
    fade_out: float = 0.0
    # transitions: "in" sits on the cut with the previous adjacent clip (centred), or fades in from the
    # layers below when there is none; "out" is only used when no clip follows directly
    tin: str = ""
    tin_dur: float = 1.0
    tout: str = ""
    tout_dur: float = 1.0
    # audio processing
    denoise: float = 0.0  # noise reduction strength 0..1
    # graphics / title properties (see graphics.py)
    text: str = ""
    font_size: int = 96
    color: str = "#ffffff"
    subtext: str = ""  # secondary line(s), e.g. the role under a name in a lower third
    sub_color: str = "#d0d4dc"
    font_family: str = "Sans"
    bold: bool = True
    italic: bool = False
    align: str = "center"  # left | center | right
    outline_width: float = 0.0  # sequence pixels
    outline_color: str = "#000000"
    shadow: float = 0.6  # 0..1
    bg_style: str = "none"  # none | box | bar | band
    bg_color: str = "#000000"
    bg_opacity: float = 0.6
    accent_color: str = "#ffc533"
    anim_in: str = ""
    anim_in_dur: float = 0.6
    anim_out: str = ""
    anim_out_dur: float = 0.5
    roll: str = ""  # "" static | up (credits) | left (crawl)

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def src_end(self) -> float:
        return self.in_point + self.duration * self.speed

    def source_time(self, t: float) -> float:
        """Source-media time shown at timeline time t."""
        return self.in_point + (t - self.start) * self.speed

    def clone(self, **changes) -> "Clip":
        c = copy.copy(self)
        c.__dict__.update(changes)
        return c

    def envelope(self, local_t: float) -> float:
        """Fade gain at clip-local time."""
        g = 1.0
        if self.fade_in > 0:
            g = min(g, max(0.0, local_t / self.fade_in))
        if self.fade_out > 0:
            g = min(g, max(0.0, (self.duration - local_t) / self.fade_out))
        return g


@dataclass
class Track:
    id: str
    kind: str  # 'video' | 'audio'
    name: str
    clips: list[Clip] = field(default_factory=list)
    muted: bool = False  # audio: muted / video: hidden

    def sort(self) -> None:
        self.clips.sort(key=lambda c: c.start)

    def clip_at(self, t: float) -> Clip | None:
        for c in self.clips:
            if c.start <= t < c.end:
                return c
        return None


@dataclass
class Project:
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    sample_rate: int = 48000
    media: dict[str, MediaItem] = field(default_factory=dict)
    video_tracks: list[Track] = field(default_factory=list)
    audio_tracks: list[Track] = field(default_factory=list)

    @classmethod
    def new(cls, n_video: int = 3, n_audio: int = 3) -> "Project":
        p = cls()
        for _ in range(n_video):
            p.add_track("video")
        for _ in range(n_audio):
            p.add_track("audio")
        return p

    def add_track(self, kind: str) -> Track:
        lst = self.video_tracks if kind == "video" else self.audio_tracks
        t = Track(new_id(), kind, f"{'V' if kind == 'video' else 'A'}{len(lst) + 1}")
        lst.append(t)
        return t

    def tracks(self, kind: str | None = None) -> list[Track]:
        if kind == "video":
            return self.video_tracks
        if kind == "audio":
            return self.audio_tracks
        return self.video_tracks + self.audio_tracks

    def all_clips(self):
        for tr in self.tracks():
            yield from ((tr, c) for c in tr.clips)

    def find_clip(self, cid: str) -> tuple[Track | None, Clip | None]:
        for tr, c in self.all_clips():
            if c.id == cid:
                return tr, c
        return None, None

    def linked(self, clip: Clip) -> list[Clip]:
        if clip.link is None:
            return [clip]
        return [c for _, c in self.all_clips() if c.link == clip.link]

    def expand_links(self, ids) -> set[str]:
        out = set()
        for cid in ids:
            _, c = self.find_clip(cid)
            if c:
                out.update(x.id for x in self.linked(c))
        return out

    def duration(self) -> float:
        return max((c.end for _, c in self.all_clips()), default=0.0)

    def edit_points(self) -> list[float]:
        pts = {0.0}
        for _, c in self.all_clips():
            pts.add(round(c.start, 6))
            pts.add(round(c.end, 6))
        return sorted(pts)

    def frame(self, t: float) -> float:
        """Snap a time to the frame grid."""
        return round(t * self.fps) / self.fps

    # ---------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "sample_rate": self.sample_rate,
            "media": [asdict(m) for m in self.media.values()],
            "video_tracks": [_track_dict(t) for t in self.video_tracks],
            "audio_tracks": [_track_dict(t) for t in self.audio_tracks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        p = cls(
            width=int(d.get("width", 1920)),
            height=int(d.get("height", 1080)),
            fps=float(d.get("fps", 30.0)),
            sample_rate=int(d.get("sample_rate", 48000)),
        )
        mfields = {f.name for f in fields(MediaItem)}
        for m in d.get("media", []):
            item = MediaItem(**{k: v for k, v in m.items() if k in mfields})
            p.media[item.id] = item
        p.video_tracks = [_track_from(t) for t in d.get("video_tracks", [])]
        p.audio_tracks = [_track_from(t) for t in d.get("audio_tracks", [])]
        return p

    def clone(self) -> "Project":
        return Project.from_dict(self.to_dict())


_CLIP_FIELDS = {f.name for f in fields(Clip)}


def _track_dict(t: Track) -> dict:
    return {"id": t.id, "kind": t.kind, "name": t.name, "muted": t.muted,
            "clips": [asdict(c) for c in t.clips]}


def _track_from(d: dict) -> Track:
    t = Track(d["id"], d["kind"], d["name"], muted=d.get("muted", False))
    t.clips = [Clip(**{k: v for k, v in c.items() if k in _CLIP_FIELDS}) for c in d.get("clips", [])]
    t.sort()
    return t


# ================================================================== edits
def carve(track: Track, a: float, b: float, exclude: set[str] = frozenset()) -> None:
    """Overwrite edit: clear [a, b) on `track`, trimming/splitting/removing clips."""
    if b - a <= 0:
        return
    keep: list[Clip] = []
    for c in track.clips:
        if c.id in exclude or c.end <= a + 1e-9 or c.start >= b - 1e-9:
            keep.append(c)
            continue
        if c.start >= a - 1e-9 and c.end <= b + 1e-9:
            continue  # fully covered -> removed
        if c.start < a and c.end > b:  # new range sits inside -> split in two
            right = c.clone(id=new_id(), link=None, start=b, in_point=c.source_time(b),
                            duration=c.end - b, fade_in=0.0, tin="")
            c.duration = a - c.start
            c.fade_out = 0.0
            c.tout = ""
            keep += [c, right]
        elif c.start < a:  # overlaps on the right side of c
            c.duration = a - c.start
            c.fade_out = min(c.fade_out, c.duration)
            keep.append(c)
        else:  # overlaps on the left side of c
            cut = b - c.start
            c.in_point += cut * c.speed
            c.duration -= cut
            c.start = b
            c.fade_in = min(c.fade_in, c.duration)
            keep.append(c)
    track.clips = [c for c in keep if c.duration > MIN_CLIP]
    track.sort()


def split_clip(project: Project, clip: Clip, t: float, new_link: str | None = None) -> Clip | None:
    """Split one clip at timeline time t. Returns the right-hand piece."""
    if not (clip.start + MIN_CLIP < t < clip.end - MIN_CLIP):
        return None
    track, _ = project.find_clip(clip.id)
    right = clip.clone(id=new_id(), link=new_link, start=t, in_point=clip.source_time(t),
                       duration=clip.end - t, fade_in=0.0, tin="")
    clip.duration = t - clip.start
    clip.fade_out = 0.0
    clip.tout = ""
    track.clips.append(right)
    track.sort()
    return right


def split_with_links(project: Project, clip: Clip, t: float) -> bool:
    group = project.linked(clip)
    link = new_id() if clip.link else None
    done = False
    for c in group:
        if split_clip(project, c, t, link):
            done = True
    return done


def remove_clips(project: Project, ids: set[str], ripple: bool = False) -> None:
    for tr in project.tracks():
        removed = sorted((c for c in tr.clips if c.id in ids), key=lambda c: c.start, reverse=True)
        tr.clips = [c for c in tr.clips if c.id not in ids]
        if ripple:
            for r in removed:  # from the latest removal backwards so shifts stay correct
                for c in tr.clips:
                    if c.start >= r.end - 1e-9:
                        c.start -= r.duration
        tr.sort()


def transition_windows(track: Track):
    """Yield (t0, t1, kind, outgoing_clip|None, incoming_clip|None) for every transition on a track."""
    clips = track.clips
    for i, c in enumerate(clips):
        prev = clips[i - 1] if i > 0 and abs(clips[i - 1].end - c.start) < 1e-3 else None
        nxt = clips[i + 1] if i + 1 < len(clips) and abs(clips[i + 1].start - c.end) < 1e-3 else None
        if c.tin and c.tin_dur > 0:
            if prev is not None:
                d = min(c.tin_dur, 2 * prev.duration, 2 * c.duration)
                yield c.start - d / 2, c.start + d / 2, c.tin, prev, c
            else:
                d = min(c.tin_dur, c.duration)
                yield c.start, c.start + d, c.tin, None, c
        if c.tout and c.tout_dur > 0 and nxt is None:
            d = min(c.tout_dur, c.duration)
            yield c.end - d, c.end, c.tout, c, None


def split_all_at(project: Project, t: float) -> None:
    """Cut every clip on every track that spans t (keeping A/V links paired)."""
    seen: set[str] = set()
    for tr in project.tracks():
        for c in list(tr.clips):
            if c.id in seen or not (c.start + MIN_CLIP < t < c.end - MIN_CLIP):
                continue
            seen.update(x.id for x in project.linked(c))
            split_with_links(project, c, t)


def ripple_ranges(project: Project, ranges: list[tuple[float, float]], mode: str = "cut",
                  factor: float = 4.0) -> float:
    """Remove (mode='cut') or speed up (mode='speed') timeline ranges on ALL tracks, closing the gaps so
    everything stays in sync. Returns the total time saved."""
    eps = 1e-6
    saved = 0.0
    for a, b in sorted(ranges, reverse=True):  # last first, so earlier ranges keep their positions
        if b - a <= MIN_CLIP:
            continue
        split_all_at(project, b)
        split_all_at(project, a)
        inside = {c.id for _, c in project.all_clips() if c.start >= a - eps and c.end <= b + eps}
        if mode == "cut":
            remove_clips(project, inside)
            shift = b - a
        else:
            span = max(1 / project.fps, project.frame((b - a) / factor))
            k = (b - a) / span
            for _, c in project.all_clips():
                if c.id in inside:
                    c.start = a + (c.start - a) / k
                    c.duration /= k
                    c.speed *= k
                    c.fade_in, c.fade_out = min(c.fade_in, c.duration), min(c.fade_out, c.duration)
            shift = (b - a) - span
        for tr in project.tracks():
            for c in tr.clips:
                if c.id not in inside and c.start >= b - eps:
                    c.start -= shift
            tr.sort()
        saved += shift
    return saved
