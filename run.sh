#!/bin/bash
# Cron wrapper: rotate log at 10MB, then run one sync cycle inside the venv.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/logs/tvtime.log"
mkdir -p "$HOME/logs"
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG")" -gt 10485760 ]; then
    mv "$LOG" "$LOG.1"
fi
exec "$DIR/venv/bin/python" -m plex_tvtime_sync.sync >> "$LOG" 2>&1
