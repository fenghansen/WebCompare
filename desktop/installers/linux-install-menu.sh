#!/bin/sh
set -eu
app_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
data_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}
mkdir -p "$data_dir/applications" "$data_dir/icons/hicolor/256x256/apps"
# Desktop Entry quoted arguments escape backslashes, quotes, dollar signs and backticks.
app_exec=$(printf '%s' "$app_dir/webcompare-desktop" | sed 's/%/%%/g; s/[\\"`$]/\\&/g')
cat > "$data_dir/applications/webcompare-desktop.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=WebCompare
Comment=Local image and video comparison
Exec="$app_exec"
Icon=webcompare-desktop
Terminal=false
Categories=Graphics;Photography;
StartupWMClass=WebCompare
ENTRY
cp "$app_dir/icon.png" "$data_dir/icons/hicolor/256x256/apps/webcompare-desktop.png"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$data_dir/applications" || true
fi
printf '%s\n' 'WebCompare is now in the application menu. Keep this extracted directory in place.'
