#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = SKILL_ROOT.parents[2]


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _candidate_executors() -> List[Path]:
    candidates: List[Path] = []

    explicit_executor = os.environ.get("NOTIFY_EXECUTOR")
    if explicit_executor:
        candidates.append(Path(explicit_executor).expanduser())

    explicit_root = os.environ.get("NOTIFY_ROOT")
    if explicit_root:
        candidates.append(Path(explicit_root).expanduser() / "run" / "execute-request.py")

    candidates.extend(
        [
            SKILL_ROOT.parent / "notify" / "run" / "execute-request.py",
            WORKSPACE_ROOT / "skills" / "notify" / "run" / "execute-request.py",
            WORKSPACE_ROOT / "repositories" / "notify" / "run" / "execute-request.py",
        ]
    )
    return candidates


def resolve_notify_executor() -> Path:
    seen = set()
    for candidate in _candidate_executors():
        try:
            resolved = candidate.expanduser().resolve()
        except FileNotFoundError:
            resolved = candidate.expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if resolved.exists() and resolved.is_file():
            return resolved
    searched = [str(p) for p in _candidate_executors()]
    raise FileNotFoundError(f"notify executor not found; searched: {searched}")


def explain_notify_resolution() -> Dict[str, Any]:
    resolved: Optional[str] = None
    error: Optional[str] = None
    try:
        resolved = str(resolve_notify_executor())
    except Exception as e:
        error = str(e)
    return {
        "resolved": resolved,
        "searched": [str(p) for p in _candidate_executors()],
        "env": {
            "NOTIFY_EXECUTOR": os.environ.get("NOTIFY_EXECUTOR"),
            "NOTIFY_ROOT": os.environ.get("NOTIFY_ROOT"),
        },
        "error": error,
    }


def _maybe_inject_failure(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    target_id = os.environ.get("REMINDER_NOTIFY_INJECT_FAILURE_FOR_ID")
    target_dedupe = os.environ.get("REMINDER_NOTIFY_INJECT_FAILURE_FOR_DEDUPE_KEY")
    status = os.environ.get("REMINDER_NOTIFY_INJECT_FAILURE_STATUS")
    reason = os.environ.get("REMINDER_NOTIFY_INJECT_FAILURE_REASON") or "validation-only injected failure"

    if not status:
        return None

    req_dedupe = str(req.get("dedupe_key") or "")
    reminder_id = None
    metadata = req.get("metadata")
    if isinstance(metadata, dict):
        reminder_id = metadata.get("reminder_id")

    matched = False
    if target_id and reminder_id and str(reminder_id) == target_id:
        matched = True
    if target_dedupe and req_dedupe == target_dedupe:
        matched = True

    if not matched:
        return None

    if status not in ("invalid_request", "delivery_failed"):
        status = "delivery_failed"

    return {
        "status": status,
        "event_id": req.get("event_id"),
        "dedupe_key": req.get("dedupe_key"),
        "reason": reason,
    }


def send_notification_request(req: Dict[str, Any]) -> Dict[str, Any]:
    injected = _maybe_inject_failure(req)
    if injected is not None:
        return injected

    fd, tmp = tempfile.mkstemp(prefix="notification-request.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=2)
            f.write("\n")

        try:
            notify_executor = resolve_notify_executor()
        except Exception as e:
            return {
                "status": "delivery_failed",
                "event_id": req.get("event_id"),
                "dedupe_key": req.get("dedupe_key"),
                "reason": f"notify-executor-resolution-failed:{e}",
            }

        cmd = [sys.executable, str(notify_executor), "--request-file", tmp]
        p = _run(cmd)
        raw = (p.stdout or "").strip()
        if not raw:
            return {
                "status": "delivery_failed",
                "event_id": req.get("event_id"),
                "dedupe_key": req.get("dedupe_key"),
                "reason": "notify returned empty output",
            }
        try:
            return json.loads(raw)
        except Exception:
            return {
                "status": "delivery_failed",
                "event_id": req.get("event_id"),
                "dedupe_key": req.get("dedupe_key"),
                "reason": f"notify returned invalid json: {raw}",
            }
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
