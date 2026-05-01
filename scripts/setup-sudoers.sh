#!/usr/bin/env bash
# Installs the purge helper at /usr/local/sbin/free-linux-monitor-purge and
# adds a sudoers rule so the current user can run it without a password.
# Mirrors the FreeMacMonitor sudoers-free mode: one helper, one allow rule.
#
# Usage:  sudo ./scripts/setup-sudoers.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root (sudo $0)." >&2
    exit 1
fi

INVOKING_USER="${SUDO_USER:-${USER}}"
if [ -z "$INVOKING_USER" ] || [ "$INVOKING_USER" = "root" ]; then
    echo "Could not detect your normal user (SUDO_USER unset). Re-run as 'sudo $0'." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/free-linux-monitor-purge"
DEST="/usr/local/sbin/free-linux-monitor-purge"
SUDOERS_FILE="/etc/sudoers.d/free-linux-monitor-purge"

if [ ! -f "$SRC" ]; then
    echo "Helper source not found: $SRC" >&2
    exit 1
fi

install -m 0755 -o root -g root "$SRC" "$DEST"
echo "[1/2] Installed helper:  $DEST"

# Use visudo --check to validate before atomically replacing the rule file —
# a malformed sudoers file locks the system out of all sudo.
TMP="$(mktemp /tmp/flm-sudoers.XXXXXX)"
trap 'rm -f "$TMP"' EXIT
printf '%s ALL=(root) NOPASSWD: %s\n' "$INVOKING_USER" "$DEST" > "$TMP"
chmod 0440 "$TMP"
if ! visudo -cf "$TMP" >/dev/null; then
    echo "Generated sudoers file failed validation; aborting." >&2
    exit 1
fi
install -m 0440 -o root -g root "$TMP" "$SUDOERS_FILE"
echo "[2/2] Installed sudoers: $SUDOERS_FILE"

cat <<EOF

OK. From now on '$INVOKING_USER' can run:
  sudo -n $DEST
without a password. Set Auto-Release Memory → "Auto-run — sudoers-free"
in the tray menu to use it from the app.

To uninstall:
  sudo rm $DEST $SUDOERS_FILE
EOF
