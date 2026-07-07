#!/usr/bin/env bash
# Nightly data update, run by cron on trading days after market close.
# Layered alerting:
#   - `nrfm update` itself emails on validation failure or Python crash
#   - this wrapper emails as a fallback if the process died in a way
#     Python could not report (missing venv, OOM kill, etc.)
# Logs go to data/logs/, pruned after 90 days.

set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO/data/logs"
LOG_FILE="$LOG_DIR/update-$(date +%F).log"
mkdir -p "$LOG_DIR"

"$REPO/.venv/bin/nrfm" update >> "$LOG_FILE" 2>&1
status=$?
echo "$(date -u +%FT%TZ) update exit=$status" >> "$LOG_FILE"

if [ "$status" -eq 0 ]; then
    # data is fresh and validated: run the daily decision engine
    # (emails a trade list only when action is needed)
    "$REPO/.venv/bin/nrfm" daily >> "$LOG_FILE" 2>&1
    status=$?
    echo "$(date -u +%FT%TZ) daily exit=$status" >> "$LOG_FILE"
fi

if [ "$status" -ne 0 ]; then
    # exit 1 = validation failed, exit 2 = crash: both already emailed by
    # Python (best effort). Anything else means nrfm never got to run or
    # died silently -- send the fallback alert.
    if [ "$status" -ne 1 ] && [ "$status" -ne 2 ]; then
        "$REPO/.venv/bin/python" - "$status" "$LOG_FILE" <<'EOF' || true
import sys
from nrfm.notify import try_send_email

status, log_file = sys.argv[1], sys.argv[2]
try:
    with open(log_file) as f:
        tail = "".join(f.readlines()[-50:])
except OSError as e:
    tail = f"(could not read log: {e})"
try_send_email(
    f"[NRFM] NIGHTLY UPDATE FAILED (exit {status})",
    "The nightly update wrapper caught an unexpected failure.\n"
    "The data store may be stale -- no trading decisions should be "
    f"made until a later run succeeds.\n\nLast log lines:\n{tail}",
)
EOF
    fi
fi

find "$LOG_DIR" -name 'update-*.log' -mtime +90 -delete 2>/dev/null
exit "$status"
