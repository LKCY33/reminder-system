#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional


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


def mark_completed(path: str, reminder_id: str) -> bool:
    state = _load_state(path)
    changed = False
    for r in state.get("reminders", []):
        if r.get("id") != reminder_id:
            continue
        r["status"] = "completed"
        r["last_run_at"] = _iso_now()
        r["updated_at"] = _iso_now()
        changed = True
        break
    if changed:
        _atomic_write_json(path, state)
    return changed


def get_status(path: str, reminder_id: str) -> Optional[str]:
    state = _load_state(path)
    for r in state.get("reminders", []):
        if r.get("id") == reminder_id:
            return r.get("status")
    return None
