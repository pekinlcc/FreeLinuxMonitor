#!/usr/bin/env bash
# Install Free Linux Monitor for the current user (no root needed).
# - Copies the package to ~/.local/share/free-linux-monitor
# - Drops a launcher script at ~/.local/bin/free-linux-monitor
# - Installs the .desktop file to ~/.local/share/applications/
# - Installs the indicator icon to ~/.local/share/icons/hicolor/scalable/apps/
#
# System dependencies must be present (apt install on Ubuntu):
#   python3-gi python3-psutil
#   gir1.2-gtk-3.0 gir1.2-webkit2-4.1 gir1.2-ayatanaappindicator3-0.1
#   libnotify-bin policykit-1
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Don't run this with sudo — it installs into your user prefix." >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PKG_NAME="free_linux_monitor"

DEST_BASE="$HOME/.local/share/free-linux-monitor"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

mkdir -p "$DEST_BASE" "$BIN_DIR" "$APPS_DIR" "$ICON_DIR"

# Refresh the package each install so updates land cleanly.
rm -rf "$DEST_BASE/$PKG_NAME"
cp -r "$REPO_DIR/$PKG_NAME" "$DEST_BASE/$PKG_NAME"

# Launcher
cat > "$BIN_DIR/free-linux-monitor" <<EOF
#!/usr/bin/env bash
exec python3 -c "import sys; sys.path.insert(0, '$DEST_BASE'); from $PKG_NAME.app import main; sys.exit(main())" "\$@"
EOF
chmod 0755 "$BIN_DIR/free-linux-monitor"

# Desktop file — point Exec at the launcher we just wrote so it works
# regardless of whether ~/.local/bin is on PATH for graphical sessions.
sed "s|^Exec=.*|Exec=$BIN_DIR/free-linux-monitor|" \
    "$REPO_DIR/free-linux-monitor.desktop" > "$APPS_DIR/free-linux-monitor.desktop"

# Indicator icon for the system tray (referenced by Icon= name)
cp "$REPO_DIR/$PKG_NAME/icons/free-linux-monitor.svg" "$ICON_DIR/free-linux-monitor.svg"

# Refresh icon cache; non-fatal if gtk-update-icon-cache is absent.
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi
update-desktop-database "$APPS_DIR" 2>/dev/null || true

cat <<EOF

Installed.

  Launcher : $BIN_DIR/free-linux-monitor
  App entry: $APPS_DIR/free-linux-monitor.desktop

Run it with:
  free-linux-monitor &
or open it from the application grid.

If the tray icon doesn't appear on GNOME, enable the AppIndicator extension:
  gnome-extensions enable ubuntu-appindicators@ubuntu.com

For the sudoers-free auto-release mode, run once:
  sudo $REPO_DIR/scripts/setup-sudoers.sh
EOF
