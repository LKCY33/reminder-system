#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
ROOT_DIR="${REMINDER_SYSTEM_ROOT:-${SCRIPT_DIR:h}}"
STATE_PATH="${REMINDER_SYSTEM_STATE:-${ROOT_DIR}/data/state.json}"
LOG_DIR="${REMINDER_SYSTEM_LOG_DIR:-${ROOT_DIR}/logs}"
LOG_FILE="${LOG_DIR}/reminder-scheduler.log"

mkdir -p "$LOG_DIR"

{
  ts=$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')
  echo "[${ts}] start cron_reminder_scheduler"
  cd "$ROOT_DIR"
  /usr/bin/python3 "${ROOT_DIR}/scripts/scheduler_lookahead.py" --state "$STATE_PATH"
  rc=$?
  ts=$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')
  echo "[${ts}] done rc=${rc}"
  exit $rc
} >>"$LOG_FILE" 2>&1
