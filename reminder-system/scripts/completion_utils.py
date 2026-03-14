#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"reminders": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="state.", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_daily_event_utc(reference_utc: datetime, *, time_str: str, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)
    local_ref = reference_utc.astimezone(tz)
    hh, mm = [int(part) for part in time_str.split(":", 1)]
    candidate = local_ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= local_ref:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def advance_or_complete(path: str, reminder_id: str) -> Optional[str]:
    state = _load_state(path)
    changed = False
    outcome: Optional[str] = None
    now = datetime.now(timezone.utc)
    for r in state.get("reminders", []):
        if r.get("id") != reminder_id:
            continue
        schedule = r.get("schedule") or {}
        schedule_type = schedule.get("type")
        r["last_run_at"] = _iso(now)
        r["updated_at"] = _iso(now)
        if schedule_type == "daily":
            tz_name = str(schedule.get("timezone") or "Asia/Shanghai")
            time_str = str(schedule.get("time") or "00:00")
            offset_minutes = int(schedule.get("offset_minutes", 0))
            current_event_raw = r.get("event_at") or r.get("next_run_at")
            try:
                current_event = datetime.fromisoformat(str(current_event_raw).replace("Z", "+00:00"))
            except Exception:
                current_event = now
            reference = max(now, current_event)
            next_event = _next_daily_event_utc(reference, time_str=time_str, tz_name=tz_name)
            r["event_at"] = _iso(next_event)
            r["next_run_at"] = _iso(next_event + timedelta(minutes=offset_minutes))
            r["status"] = "active"
            outcome = "advanced"
        else:
            r["status"] = "completed"
            outcome = "completed"
        changed = True
        break
    if changed:
        _atomic_write_json(path, state)
    return outcome


def mark_completed(path: str, reminder_id: str) -> bool:
    outcome = advance_or_complete(path, reminder_id)
    return outcome in ("completed", "advanced")


def get_status(path: str, reminder_id: str) -> Optional[str]:
    state = _load_state(path)
    for r in state.get("reminders", []):
        if r.get("id") == reminder_id:
            return r.get("status")
    return None
