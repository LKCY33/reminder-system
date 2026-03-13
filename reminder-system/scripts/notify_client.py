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


def send_notification_request(req: Dict[str, Any]) -> Dict[str, Any]:
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
