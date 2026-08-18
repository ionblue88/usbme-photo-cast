#!/usr/bin/env bash
# Install USBme Photo Cast as a desktop application for the current user (no root needed).
# Run this from the unpacked release folder — it must sit next to the USBmePhotoCast binary.
set -euo pipefail

APP=USBmePhotoCast
src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$src/$APP" ]; then
  echo "error: '$APP' not found next to install.sh — run it from the unpacked release folder." >&2
  exit 1
fi

bindir="$HOME/.local/bin"
icondir="$HOME/.local/share/icons/hicolor/256x256/apps"
deskdir="$HOME/.local/share/applications"
mkdir -p "$bindir" "$icondir" "$deskdir"

install -m 0755 "$src/$APP" "$bindir/$APP"
[ -f "$src/usbme-photo-cast.png" ] && install -m 0644 "$src/usbme-photo-cast.png" "$icondir/usbme-photo-cast.png"

cat > "$deskdir/usbme-photo-cast.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=USBme Photo Cast
Comment=Cast photos to a 6-colour e-ink frame over USB
Exec=$bindir/$APP
Icon=usbme-photo-cast
Terminal=false
Categories=Graphics;Photography;Utility;
EOF
chmod 0644 "$deskdir/usbme-photo-cast.desktop"

update-desktop-database "$deskdir" >/dev/null 2>&1 || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Installed USBme Photo Cast."
echo "  binary  : $bindir/$APP"
echo "  it should now appear in your application menu (search 'USBme')."
case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "note: $bindir isn't on your PATH, but the menu entry and ./$APP still work." ;;
esac
