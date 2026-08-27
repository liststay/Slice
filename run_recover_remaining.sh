#!/usr/bin/env bash
# Overnight / later recovery of remaining truncated sessions.
# Original videos are not modified. Output: session_*/videos_recovered/
set -euo pipefail
cd "$(dirname "$0")"

LOG="${LOG:-$PWD/recover_remaining.log}"
PIDFILE="$PWD/recover_remaining.pid"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running pid=$(cat "$PIDFILE")  log=$LOG"
  exit 1
fi

echo "starting remaining recovery  log=$LOG"
nohup python3 -u recover_remaining.py >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
echo "pid=$(cat "$PIDFILE")"
echo "watch:  tail -f $LOG"
echo "stop:   kill \$(cat $PIDFILE)"
