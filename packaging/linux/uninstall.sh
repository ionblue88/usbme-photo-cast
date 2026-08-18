#!/usr/bin/env bash
# Remove the USBme Photo Cast desktop integration for the current user.
# Your photos and settings in ~/USBmePhotoCast are left untouched.
set -euo pipefail

rm -f "$HOME/.local/bin/USBmePhotoCast"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/usbme-photo-cast.png"
rm -f "$HOME/.local/share/applications/usbme-photo-cast.desktop"

update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Removed USBme Photo Cast desktop integration."
echo "Your photos in ~/USBmePhotoCast were left untouched."
