# Super8

A non-linear video editor for Linux in the spirit of Premiere, built with PyQt6 and PyAV (FFmpeg).

## Install

From the Snap Store:

```bash
sudo snap install super8-editor
```

From source:

```bash
./super8.sh                       # first run creates .venv and installs deps
./super8.sh clip1.mp4 clip2.mov   # open with media imported
./super8.sh project.s8proj        # open a project
python tools/make_samples.py samples   # generate test clips
```

## Features
- Media bin with thumbnails: video, audio, images (with alpha), drag & drop from your file manager
- Multi-track timeline (V1..Vn stacked, A1..An mixed), linked A/V clips, snapping, unlimited undo/redo
- Editing tools: selection, track select forward, ripple, rolling, rate stretch, razor, slip, slide, hand, zoom, type
- Transform clips: fit / fill / stretch / original size, scale (also width and height separately), rotation,
  flips, crop and opacity, with on-screen handles in the Program Monitor (drag corners to resize, edges to
  stretch, the top handle to rotate) and snapping to the frame centre and edges
- Aspect ratio presets for any platform (16:9, 9:16 for Shorts/Reels/TikTok, 1:1, 4:5, 4:3, 21:9) and
  resolutions from 480p to 4K; safe-margin, rule-of-thirds and centre guides; export at another resolution
- Per-clip speed, volume and fade in/out
- Transitions: cross dissolve, dip to black/white, wipes, pushes, iris, graphic sweep/bars;
  constant power/gain audio crossfades
- Essential graphics: 12 animated templates (lower thirds, captions, chapter, typewriter, kinetic words,
  callout, quote, news ticker, credits roll…), full styling (font, outline, shadow, box/bar/band background),
  11 in/out text animations, and your own saved templates
- Silence detector for rambling footage: ripple-cut pauses (jump cuts), speed them up 4×/8×, or mark them for review
- Noise reduction (spectral gating, noise profile learned from the quietest moments), processed in the background
- Real-time preview with audio, frame-accurate scrubbing and stepping
- JSON project files (`.s8proj`); export to H.264/AAC MP4/MKV/MOV

Shortcuts: press **F1** in the app.

## AI assistant (Claude)

The **AI Assistant** tab (or **AI ▸ AI Assistant**, Ctrl+Shift+I) lets you edit in plain words:
*"cut the long pauses"*, *"add a lower third for Jane Doe at 0:05"*, *"what happens around 0:45?"*.
Claude reads your timeline, looks at frames of your video, and makes the edits with the same tools you
use. Everything it does for one request is a single undo step (Ctrl+Z).

- **Your own Claude account.** Click *Sign in with Claude*: your browser opens the Claude sign-in page,
  you approve Super8 and paste back the code it shows. This uses Anthropic's official CLI (`ant`, bundled
  in the snap; from source, install it from the
  [anthropic-cli releases](https://github.com/anthropics/anthropic-cli/releases)). It signs in to a
  Claude Console (Anthropic API) account, and usage is billed to that account. Super8 never sees your
  password or an API key, and keeps its login in its own folder (`~/.config/super8/anthropic`), separate
  from any other Claude logins on your machine.
- **What is sent.** Only when you ask something: the project's structure (clip names, times, titles) and
  any frames Claude chooses to look at. Your media files are never uploaded.
- **Models.** Claude Opus 5.5 by default; Sonnet 5.5 and Haiku 5.5 are cheaper choices in the panel.
- Developers can use `ANTHROPIC_API_KEY` instead of signing in.

## Support Super8

Super8 is free and open source, and always will be. If it helps you tell your stories,
you can support its development with a donation:

| Coin | Address |
|---|---|
| Bitcoin | `bc1qnyceh488yqr0l6ffljygf47243p8wsvct4mkw7` |
| Ethereum | `0x8bc6D2D3d47D15EEd881975A09F008609F3bAdF2` |
| Solana | `3Gq3t2Xoix8eRvyxCE8mdB5BPsNCwHxDVc8oLXMTeFe1` |

The same addresses, with copy buttons, are in the app under **Help → Support Super8**.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest          # generates its own sample media on first run
.venv/bin/python tools/make_icon.py # redraws the app icon (super8/resources, snap/gui)
```

## Layout
- `super8/model.py`: project model and edit operations (no Qt)
- `super8/media.py`: probing, frame-accurate decoding, audio decode, background loader
- `super8/compositor.py`: layer compositing, transitions and audio mixing
- `super8/transitions.py`: transition catalogue and pixel blending
- `super8/audiofx.py`: loudness envelope, silence detection, noise reduction
- `super8/graphics.py`: title layout, styling, animation and templates
- `super8/export.py`: encoder
- `super8/ai/`: Claude assistant: sign-in (`auth.py`), editor tools (`tools.py`), streamed tool loop (`assistant.py`)
- `super8/ui/`: main window, timeline, monitor/bin/inspector/effects/graphics/AI panels, about & support
- `snap/`: snapcraft.yaml, desktop entry and icon
- `tests/`: pytest suite

## License

Super8 is free software, released under the [GNU General Public License v3.0](LICENSE).
