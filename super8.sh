#!/usr/bin/env bash
# Launch Super8 (creates the virtualenv on first run).
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$DIR/requirements.txt"
fi

# The GTK3 platform-theme plugin bundled with PyQt6 calls gtk_init(), which aborts on
# systems whose GNOME settings schemas don't match the installed GTK ("does not contain
# a key named 'antialiasing'"). Super8 draws its own dark theme, so move the plugin out
# of Qt's search path (Qt loads any file in that folder, whatever its name).
for GTK_PLUGIN in "$VENV"/lib/python3*/site-packages/PyQt6/Qt6/plugins/platformthemes/libqgtk3.so*; do
  [ -e "$GTK_PLUGIN" ] || continue
  mkdir -p "$VENV/disabled-qt-plugins"
  mv -f "$GTK_PLUGIN" "$VENV/disabled-qt-plugins/libqgtk3.so"
done

cd "$DIR" && exec "$VENV/bin/python" -m super8 "$@"
