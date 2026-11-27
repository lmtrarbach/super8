# Changelog

## Unreleased

### New logo
- The new Super8 logo is the app, window and Snap Store icon

### Donations
- **Donate with card** in Help ▸ Support Super8, through a Stripe Payment Link (pay what you want);
  shown once the link is set in `super8/donate.py`. `tools/stripe_setup.py` creates the product, price
  and link in Stripe and is safe to re-run

### Transform and aspect ratio
- Clip transforms: fit mode (fit, fill, stretch, original size), width/height scaling, rotation,
  horizontal/vertical flip and crop, in the Inspector and as **Clip ▸ Transform** actions
- Program Monitor handles: drag corners to resize, edges to stretch, the top handle to rotate (Shift:
  15° steps); moving snaps to the frame centre and edges (Ctrl: no snapping); Alt+wheel rotates
- Aspect ratio button under the monitor (16:9, 9:16, 1:1, 4:5, 4:3, 21:9 and resolutions); reworked
  Sequence Settings; safe-margin, rule-of-thirds and centre guides
- Export at a different resolution, keeping the aspect ratio
- Faster compositing: titles, transparent images, opacity and rotated clips are drawn by Qt directly into
  the frame (a rotated full-HD layer went from ~96 ms to ~26 ms)
- The AI assistant can transform clips and change the sequence's aspect ratio

### AI assistant
- New **AI Assistant** tab: chat with Claude about your project; it reads the timeline, looks at frames,
  and edits with tools (add/move/trim/split/delete clips, properties, titles, transitions, silence
  cutting). Each request is one undo step
- **Sign in with Claude** using your own account (Anthropic's official CLI, bundled in the snap); the
  login is kept in Super8's own folder
- Model choice: Claude Opus 5.5 (default), Sonnet 5.5, Haiku 5.5
- The snap gains the `network` permission, used only by the assistant

## 0.2.0 — 2026-11-21

### Timeline zoom
- Zoom bar with a mini-map of the whole sequence: drag the view box to pan, drag its
  edges to zoom, click to jump, double-click to fit
- Zoom slider, Zoom to Fit (`\`), Zoom to Selection (Shift+Z), and the Zoom tool now
  zooms to a dragged range; keyboard zoom keeps the playhead in place
- Zoom from ~3 hours down to ~130 px per frame
- Video clips show a filmstrip of their real frames; audio clips show the
  sample-accurate waveform when zoomed in
- Resizable track heights (drag a track's lower edge in its header, Alt+wheel,
  Alt+= / Alt+-) and a vertical scrollbar
- Clip names stay visible when a clip starts off-screen

## 0.1.0 — 2026-11-21

First release, on the Snap Store's stable channel (`sudo snap install super8-editor`).

- Multi-track timeline with linked audio/video, snapping, overwrite edits and unlimited undo
- Editing tools: selection, track select forward, ripple, rolling, rate stretch, razor,
  slip, slide, hand, zoom and type; move/scale clips in the Program Monitor
- Transitions: dissolves, dips, wipes, pushes, iris, graphic sweep/bars, audio crossfades
- Animated graphics: 12 templates (lower thirds, captions, credits roll, ticker…),
  full styling, 11 in/out animations, your own saved templates
- Silence detector (cut, speed up or mark pauses) and background noise reduction
- Export to H.264/AAC MP4, MKV or MOV
- Help → Support Super8 with donation addresses
- Snap build fix: the GNOME SDK's old pip no longer shadows the build's pip
