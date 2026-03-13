#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List

from completion_utils import mark_completed
from notify_client import send_notification_request
from notify_request_builder import build_notification_request


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"reminders": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _defaults_route(state: Dict[str, Any]) -> Dict[str, str]:
    defaults = state.get("defaults") or {}
    route = defaults.get("route") if isinstance(defaults, dict) else None
    if isinstance(route, dict):
        out: Dict[str, str] = {}
        ch = route.get("channel")
        tgt = route.get("target")
        if ch:
            out["channel"] = ch
        if tgt:
            out["target"] = tgt
        return out
    return {}


def _find_route(state: Dict[str, Any], rid: str) -> Dict[str, str]:
    for r in state.get("reminders", []):
        if r.get("id") == rid:
            route = r.get("route") or {}
            if isinstance(route, dict):
                ch = route.get("channel")
                tgt = route.get("target")
                out: Dict[str, str] = {}
                if ch:
                    out["channel"] = ch
                if tgt:
                    out["target"] = tgt
                return out
    return {}


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.path.join(os.path.dirname(__file__), "..", "data", "state.json"), help="Path to state.json")
    ap.add_argument("--channel", default=None, help="Fallback channel if reminder has no route")
    ap.add_argument("--target", default=None, help="Fallback target if reminder has no route")
    ap.add_argument("--id", dest="rid", default=None, help="Only process a specific reminder id")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    state = _load_state(args.state)
    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "reminder_system.py"), "--state", args.state, "run-due"]
    if args.rid:
        cmd += ["--id", args.rid]
    p = _run(cmd)
    if p.returncode != 0:
        print(p.stderr, file=sys.stderr)
        return p.returncode

    fired: List[Dict[str, Any]] = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj.get("fired"), list):
            fired = obj.get("fired")
            break

    if not fired:
        return 0

    results: List[Dict[str, Any]] = []

    for payload in fired:
        rid = payload.get("id")
        if args.rid and rid != args.rid:
            continue

        route = _find_route(state, rid) if rid else {}
        defaults_route = _defaults_route(state)
        channel = route.get("channel") or args.channel or defaults_route.get("channel")
        target = route.get("target") or args.target or defaults_route.get("target")

        if not channel or not target:
            print(f"missing route for reminder id={rid}", file=sys.stderr)
            return 2

        req = build_notification_request(payload, channel=channel, target=target)

        if args.dry_run:
            print(json.dumps(req, ensure_ascii=False))
            continue

        result = send_notification_request(req)
        results.append(result)
        status = result.get("status")

        if status in ("success", "duplicate"):
            if rid:
                mark_completed(args.state, rid)
            continue
        if status == "invalid_request":
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            return 3
        if status == "delivery_failed":
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            return 1

        print(json.dumps({"status": "delivery_failed", "reason": f"unknown notify status: {status}"}, ensure_ascii=False), file=sys.stderr)
        return 1

    if results:
        print(json.dumps({"results": results, "count": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
