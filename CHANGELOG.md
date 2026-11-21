# Changelog

## 0.2.0 — 2026-11-21

First stable release on the Snap Store (`sudo snap install super8-editor`).

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

### Packaging
- Snap build fix: the GNOME SDK's old pip no longer shadows the build's pip

## 0.1.0 — 2026-11-21

First version (edge channel only).

- Multi-track timeline with linked audio/video, snapping, overwrite edits and unlimited undo
- Editing tools: selection, track select forward, ripple, rolling, rate stretch, razor,
  slip, slide, hand, zoom and type; move/scale clips in the Program Monitor
- Transitions: dissolves, dips, wipes, pushes, iris, graphic sweep/bars, audio crossfades
- Animated graphics: 12 templates (lower thirds, captions, credits roll, ticker…),
  full styling, 11 in/out animations, your own saved templates
- Silence detector (cut, speed up or mark pauses) and background noise reduction
- Export to H.264/AAC MP4, MKV or MOV
- Help → Support Super8 with donation addresses
