#!/usr/bin/env bash
# Build a Debian package for Free Linux Monitor.
#
# Output: dist/free-linux-monitor_<version>_all.deb
# The .deb is `arch=all` because the package is pure Python — no compiled
# extensions ship with it. (System Python and the listed gir1.2-* packages
# do the heavy lifting at runtime.)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

VERSION="$(python3 -c 'import re; print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", open("free_linux_monitor/__init__.py").read()).group(1))')"
PKG="free-linux-monitor"
DEB="${PKG}_${VERSION}_all.deb"

DIST="$REPO_DIR/dist"
STAGE="$(mktemp -d /tmp/flm-deb.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$DIST"
echo "[1/5] Staging package tree under $STAGE"

# Library code
install -d "$STAGE/usr/lib/python3/dist-packages/free_linux_monitor"
cp -r free_linux_monitor/. "$STAGE/usr/lib/python3/dist-packages/free_linux_monitor/"
# Drop bytecode that may have leaked in from a dev run
find "$STAGE/usr/lib/python3/dist-packages/free_linux_monitor" \
    -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Launcher (wraps `python3 -m free_linux_monitor`)
install -d "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/free-linux-monitor" <<'EOF'
#!/usr/bin/env bash
exec python3 -m free_linux_monitor "$@"
EOF
chmod 0755 "$STAGE/usr/bin/free-linux-monitor"

# Desktop entry — point Exec at the system launcher
install -d "$STAGE/usr/share/applications"
sed 's|^Exec=.*|Exec=/usr/bin/free-linux-monitor|' free-linux-monitor.desktop \
    > "$STAGE/usr/share/applications/free-linux-monitor.desktop"

# Indicator icon (referenced by Icon= in the desktop entry) plus the
# attention variant the tray swaps to on alert. Both must ship so a
# stale icon cache never resolves to a missing file.
install -d "$STAGE/usr/share/icons/hicolor/scalable/apps"
cp free_linux_monitor/icons/free-linux-monitor.svg \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/free-linux-monitor.svg"
cp free_linux_monitor/icons/free-linux-monitor-attention.svg \
    "$STAGE/usr/share/icons/hicolor/scalable/apps/free-linux-monitor-attention.svg"

# Sudoers helper (optional — won't be active until /etc/sudoers.d entry
# is written by setup-sudoers.sh)
install -d "$STAGE/usr/sbin"
install -m 0755 scripts/free-linux-monitor-purge \
    "$STAGE/usr/sbin/free-linux-monitor-purge"

# Bundled docs
install -d "$STAGE/usr/share/doc/$PKG"
cp README.md "$STAGE/usr/share/doc/$PKG/README.md"
install -m 0755 scripts/setup-sudoers.sh \
    "$STAGE/usr/share/doc/$PKG/setup-sudoers.sh"

echo "[2/5] Computing installed-size"
INSTALLED_SIZE=$(du -ks "$STAGE" | cut -f1)

echo "[3/5] Writing DEBIAN/control"
install -d "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: pekinlcc <pekinlcc@gmail.com>
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.10),
 python3-gi,
 python3-psutil,
 gir1.2-gtk-3.0,
 gir1.2-webkit2-4.1,
 gir1.2-ayatanaappindicator3-0.1,
 libnotify-bin,
 policykit-1
Recommends: gnome-shell-extension-appindicator
Homepage: https://github.com/pekinlcc/FreeLinuxMonitor
Description: Minimal system tray monitor with retro CRT / Liquid Glass themes
 Free Linux Monitor is a small Ubuntu / GNOME tray indicator that shows
 live CPU, memory, GPU and disk usage. Click the indicator to open a
 320x460 dashboard with bar charts; right-click for theme + auto-release
 controls. Pure Python 3 + GTK 3 + WebKit2 + AyatanaAppIndicator3, with
 a verbatim port of the FreeMacMonitor dashboard (HTML/CSS/JS).
EOF

echo "[4/5] Writing postinst / postrm (icon + desktop cache refresh)"
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postinst"

# postrm: refresh caches after the icon and .desktop file are gone, so
# launchers don't keep a phantom entry pointing at a deleted file.
cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ] || [ "$1" = "upgrade" ]; then
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor 2>/dev/null || true
    fi
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications 2>/dev/null || true
    fi
fi
exit 0
EOF
chmod 0755 "$STAGE/DEBIAN/postrm"

echo "[5/5] Building $DEB"
fakeroot dpkg-deb --build --root-owner-group -Zxz "$STAGE" "$DIST/$DEB"

echo
echo "Built: $DIST/$DEB"
ls -lh "$DIST/$DEB"
echo
echo "Verify:"
echo "  dpkg-deb -I $DIST/$DEB | head -20"
echo "  dpkg-deb -c $DIST/$DEB | head -20"
echo "Install:"
echo "  sudo apt install $DIST/$DEB"
