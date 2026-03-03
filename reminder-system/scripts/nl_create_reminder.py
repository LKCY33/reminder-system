#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, Optional


def _run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _local_now() -> datetime:
    # Use local time for natural language parsing; reminder_system.py expects local timestamps in --when.
    return datetime.now()


def _parse_relative_minutes(text: str) -> Optional[int]:
    # Match patterns like: "10分钟后", "1小时后", "2h后", "30min后"
    t = text.strip().lower()

    m = re.search(r"(\d{1,4})\s*(分钟|min|m)\s*后", t)
    if m:
        return int(m.group(1))

    h = re.search(r"(\d{1,4})\s*(小时|h)\s*后", t)
    if h:
        return int(h.group(1)) * 60

    return None


def _parse_absolute_when(text: str) -> Optional[str]:
    # Match: YYYY-MM-DD HH:MM (24h)
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", text.strip())
    if not m:
        return None
    return f"{m.group(1)} {m.group(2)}"


def _make_title(raw: str) -> str:
    s = raw.strip()
    # Remove common leading phrases
    s = re.sub(r"^(请)?(帮我)?(提醒我|提醒一下|记得)[:：\s]*", "", s)
    s = re.sub(r"^(remind me)[:：\s]*", "", s, flags=re.IGNORECASE)
    s = s.strip()
    if not s:
        return "Reminder"
    if len(s) <= 24:
        return s
    return s[:24].rstrip() + "..."


def _extract_content(raw: str) -> str:
    return raw.strip()


def _notes_with_meta(content: str, meta: Dict[str, Any]) -> str:
    # Option 4: content + separator + short meta.
    lines = [content, "---"]
    for k in ("channel", "target", "created_at", "source", "parsed_when"):
        v = meta.get(k)
        if v:
            lines.append(f"{k}: {v}")
    return "\n".join(lines).strip() + "\n"


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="Natural language reminder request")
    ap.add_argument("--channel", required=True, help="feishu|telegram|...")
    ap.add_argument("--target", required=True, help="feishu user:/chat:... or telegram chat id")
    ap.add_argument(
        "--state",
        default=os.path.join(os.path.dirname(__file__), "..", "data", "state.json"),
        help="Path to state.json",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    raw = args.text

    when_abs = _parse_absolute_when(raw)
    rel_min = _parse_relative_minutes(raw) if not when_abs else None

    if not when_abs and rel_min is None:
        out = {
            "ok": False,
            "needs": ["time"],
            "hint": "请告诉我提醒时间：例如‘10分钟后提醒我…’或‘在 2026-03-05 10:00 提醒我…’",
        }
        print(json.dumps(out, ensure_ascii=False))
        return 2

    if rel_min is not None:
        when_dt = _local_now() + timedelta(minutes=rel_min)
        when_abs = when_dt.strftime("%Y-%m-%d %H:%M")

    title = _make_title(raw)

    meta = {
        "channel": args.channel,
        "target": args.target,
        "created_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "source": "natural_language",
        "parsed_when": when_abs,
    }
    notes = _notes_with_meta(_extract_content(raw), meta)

    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "reminder_system.py"),
        "--state",
        args.state,
        "create",
        "--title",
        title,
        "--when",
        when_abs,
        "--offset-minutes",
        "0",
        "--notes",
        notes,
        "--notify",
        "none",
        "--route-channel",
        args.channel,
        "--route-target",
        args.target,
    ]

    if args.dry_run:
        print(json.dumps({"ok": True, "title": title, "when": when_abs, "cmd": cmd}, ensure_ascii=False))
        return 0

    p = _run(cmd)
    if p.returncode != 0:
        print(json.dumps({"ok": False, "error": p.stderr.strip() or "create failed"}, ensure_ascii=False))
        return p.returncode

    # reminder_system.py prints: {"id": ..., "next_run_at": ...}
    try:
        created = json.loads(p.stdout.strip())
    except Exception:
        created = {"raw": p.stdout.strip()}

    created["ok"] = True
    created["route"] = {"channel": args.channel, "target": args.target}
    print(json.dumps(created, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
