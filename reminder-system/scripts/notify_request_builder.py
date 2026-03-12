#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


def _canonical_due(due_value: str) -> str:
    try:
        dt = datetime.fromisoformat(due_value.replace("Z", "+00:00")).astimezone(timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except Exception:
        return due_value


def build_notification_request(payload: Dict[str, Any], *, channel: str, target: str) -> Dict[str, Any]:
    reminder_id = str(payload.get("id") or "unknown")
    due_at = _canonical_due(str(payload.get("due") or "unknown"))
    kind = "reminder_due"
    event_id = f"evt:{kind}:{reminder_id}:{due_at}"
    dedupe_key = f"{kind}:{reminder_id}:{due_at}"

    title = str(payload.get("title") or "(no title)")
    due_local = str(payload.get("due_local") or payload.get("due") or "")
    notes = str(payload.get("notes") or "").strip()
    body_lines = [
        f"时间：{due_local}",
        f"对象：{target}",
        f"内容：{notes}",
        "来源：reminder-system",
    ]

    return {
        "source": "reminder-system",
        "kind": kind,
        "event_id": event_id,
        "dedupe_key": dedupe_key,
        "channel": channel,
        "target": target,
        "title": title,
        "body": "\n".join(body_lines),
        "metadata": {
            "reminder_id": reminder_id,
            "due_at": due_at,
            "timezone": payload.get("timezone"),
        },
    }
