#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _atomic_write(path: str, obj: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"version": 1, "reminders": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ensure_job_index(state: Dict[str, Any]) -> Dict[str, Any]:
    idx = state.get("scheduled_jobs")
    if isinstance(idx, dict):
        return idx
    idx = {}
    state["scheduled_jobs"] = idx
    return idx


def _add_one_shot(job_name: str, at_iso: str, rid: str, state_path: str) -> Optional[str]:
    # Use an agentTurn payload so execution does not depend on hooks/systemEvent routing.
    # The BOOT.md contract makes __CRON_EXEC__ execute via exec.
    msg = (
        "__CRON_EXEC__ /usr/bin/python3 "
        + os.path.join(os.path.dirname(__file__), "run_due_and_notify.py")
        + " --state "
        + state_path
        + " --id "
        + rid
    )

    cmd = [
        "openclaw",
        "cron",
        "add",
        "--name",
        job_name,
        "--at",
        at_iso,
        "--session",
        "main",
        "--wake",
        "now",
        "--delete-after-run",
        "--light-context",
        "--expect-final",
        "--timeout-seconds",
        "120",
        "--message",
        msg,
        "--no-deliver",
        "--json",
    ]
    p = _run(cmd)
    if p.returncode != 0:
        print(p.stderr, file=sys.stderr)
        return None
    try:
        j = json.loads(p.stdout)
        return j.get("jobId") or j.get("id")
    except Exception:
        # Even if JSON parsing fails, the job may still have been created.
        return "created"


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--state",
        default=os.path.join(os.path.dirname(__file__), "..", "data", "state.json"),
    )
    ap.add_argument(
        "--lookahead-minutes",
        type=int,
        default=None,
        help="Minutes to look ahead for upcoming reminders. If omitted, uses state defaults.lookahead_minutes.",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    state = _load(args.state)
    idx = _ensure_job_index(state)

    defaults = state.get("defaults") or {}
    default_lookahead = defaults.get("lookahead_minutes", 60)
    lookahead_minutes = args.lookahead_minutes if args.lookahead_minutes is not None else int(default_lookahead)

    now = _utc_now()
    window_end = now + timedelta(minutes=lookahead_minutes)

    planned: List[Dict[str, Any]] = []

    for r in state.get("reminders", []):
        if r.get("status") != "active":
            continue
        next_run_at = r.get("next_run_at")
        if not next_run_at:
            continue

        try:
            fire_at = _parse_iso(next_run_at)
        except Exception:
            continue

        if fire_at < now or fire_at > window_end:
            continue

        rid = r.get("id")
        if not rid:
            continue

        # de-dupe: if already scheduled for this exact time, skip
        existing = idx.get(rid)
        if isinstance(existing, dict) and existing.get("at") == next_run_at and existing.get("status") == "scheduled":
            continue

        job_name = f"reminder-fire-{rid[:8]}"
        planned.append({"id": rid, "at": next_run_at, "job_name": job_name})

        if not args.dry_run:
            job_id = _add_one_shot(job_name, next_run_at, rid, args.state)
            idx[rid] = {"at": next_run_at, "job_id": job_id, "status": "scheduled", "created_at": now.isoformat()}

    if not args.dry_run:
        _atomic_write(args.state, state)

    print(json.dumps({"planned": planned, "count": len(planned)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
