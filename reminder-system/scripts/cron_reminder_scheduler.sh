#!/bin/zsh
set -euo pipefail

ROOT_DIR="/Users/openclaw/.openclaw/workspace/repositories/reminder-system/reminder-system"
LOG_DIR="/Users/openclaw/.openclaw/workspace/logs"
LOG_FILE="${LOG_DIR}/reminder-scheduler.log"

mkdir -p "$LOG_DIR"

{
  ts=$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')
  echo "[${ts}] start cron_reminder_scheduler"
  cd "$ROOT_DIR"
  /usr/bin/python3 "${ROOT_DIR}/scripts/scheduler_lookahead.py"
  rc=$?
  ts=$(/bin/date '+%Y-%m-%dT%H:%M:%S%z')
  echo "[${ts}] done rc=${rc}"
  exit $rc
} >>"$LOG_FILE" 2>&1
