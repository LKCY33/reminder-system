#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Dict, List


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _openclaw_message_send(text: str, channel: str, target: str) -> bool:
    cmd = [
        "openclaw",
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        target,
        "--message",
        text,
    ]
    p = _run(cmd)
    return p.returncode == 0


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"reminders": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def _format_payload(payload: Dict[str, Any]) -> str:
    title = payload.get("title") or "(no title)"
    rid = payload.get("id")
    due_local = payload.get("due_local") or payload.get("due")
    now_local = payload.get("now_local") or payload.get("now")
    tz = payload.get("timezone")
    notes = payload.get("notes") or ""

    lines = [
        "提醒到期",
        f"- 标题: {title}",
        f"- 到期: {due_local}" + (f" ({tz})" if tz else ""),
        f"- 现在: {now_local}" + (f" ({tz})" if tz else ""),
    ]
    if rid:
        lines.append(f"- id: {rid}")
    if notes.strip():
        lines.append("- 备注:")
        lines.append(notes.strip())
    return "\n".join(lines)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--state",
        default=os.path.join(os.path.dirname(__file__), "..", "data", "state.json"),
        help="Path to state.json",
    )
    ap.add_argument("--channel", default="feishu", help="Fallback channel if reminder has no route")
    ap.add_argument(
        "--target",
        default="user:ou_4da26eb40cfb44caee9ad41074668bba",
        help="Fallback target if reminder has no route",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    state = _load_state(args.state)

    cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "reminder_system.py"), "--state", args.state, "run-due"]
    p = _run(cmd)
    if p.returncode != 0:
        print(p.stderr, file=sys.stderr)
        return p.returncode

    fired: List[Dict[str, Any]] = []

    # reminder_system prints optional per-reminder lines + a final JSON summary.
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "reminder_due":
            fired.append(obj.get("payload") or {})

    if not fired:
        return 0

    for payload in fired:
        msg = _format_payload(payload)

        rid = payload.get("id")
        route = _find_route(state, rid) if rid else {}
        channel = route.get("channel") or args.channel
        target = route.get("target") or args.target

        if args.dry_run:
            print(json.dumps({"channel": channel, "target": target, "message": msg}, ensure_ascii=False))
            continue

        ok = _openclaw_message_send(msg, channel, target)
        if not ok:
            print("failed to send message", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
