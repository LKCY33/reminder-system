#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List

NOTIFY_EXECUTOR = "/Users/openclaw/.openclaw/workspace/repositories/notify/run/execute-request.py"


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def send_notification_request(req: Dict[str, Any]) -> Dict[str, Any]:
    fd, tmp = tempfile.mkstemp(prefix="notification-request.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=2)
            f.write("\n")

        cmd = [sys.executable, NOTIFY_EXECUTOR, "--request-file", tmp]
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
