#!/bin/bash
# Launcher: run the TUI, then file any new notes into the vault if the
# process-meeting pipeline is configured. Safe to symlink into ~/.local/bin.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
CONF_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/meeting-notes/process-meeting.conf"

NOTES_DIR="$HOME/Documents/meeting-notes/notes"
# shellcheck source=/dev/null
[ -f "$CONF_FILE" ] && source "$CONF_FILE"

shopt -s nullglob

# Snapshot existing notes so only new ones get processed
declare -A seen
for f in "$NOTES_DIR"/*.md; do
    seen["$f"]=1
done

cd "$REPO_DIR" || exit 1
venv/bin/python run.py "$@"
status=$?

if [ -f "$CONF_FILE" ]; then
    for f in "$NOTES_DIR"/*.md; do
        if [ -z "${seen["$f"]:-}" ]; then
            echo "→ Processing: $(basename "$f")"
            ./process-meeting.sh "$f" || echo "⚠ Could not process $f"
        fi
    done
fi

exit "$status"
