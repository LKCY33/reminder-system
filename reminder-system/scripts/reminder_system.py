#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


STATE_VERSION = 1
DEFAULT_TZ = "America/Los_Angeles"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _local_tz_name() -> str:
    # On macOS, /etc/localtime points at the current system timezone.
    # time.tzname is not a stable IANA tz id, so we prefer TZ env or fall back.
    return os.environ.get("TZ") or DEFAULT_TZ


def _parse_when_local_to_utc(s: str, tz: ZoneInfo) -> datetime:
    """Parse a local timestamp string and convert it to UTC.

    Supported formats:
    - YYYY-MM-DD HH:MM
    - YYYY-MM-DD (interpreted as 00:00)

    Ambiguous DST times are handled by Python's default fold=0 behavior.
    """

    s = s.strip()
    parsed: Optional[datetime] = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError(f"Unsupported --when format: {s!r}")

    local_dt = parsed.replace(tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "version": STATE_VERSION,
            "timezone": DEFAULT_TZ,
            "reminders": [],
        }
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


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _mirror_apple(title: str, notes: str) -> Optional[str]:
    """Best-effort mirror into Apple Reminders.

    Returns a backend id if we can extract one; otherwise None.
    """

    cmd = ["remindctl", "add", "--title", title]
    if notes:
        cmd += ["--notes", notes]

    p = _run(cmd)
    if p.returncode != 0:
        return None

    # remindctl prints a human line; treat id extraction as optional.
    return None


@dataclass
class Reminder:
    id: str
    title: str
    notes: str
    status: str
    schedule: Dict[str, Any]
    next_run_at: str
    channels: List[str]
    backend_refs: Dict[str, Any]


def _find(state: Dict[str, Any], rid: str) -> Optional[Dict[str, Any]]:
    for r in state.get("reminders", []):
        if r.get("id") == rid:
            return r
    return None


def cmd_create(args: argparse.Namespace) -> int:
    state_path = args.state
    state = _load_state(state_path)

    rid = str(uuid.uuid4())
    tz_name = _local_tz_name()
    tz = ZoneInfo(tz_name)
    when = _parse_when_local_to_utc(args.when, tz)

    reminder: Dict[str, Any] = {
        "id": rid,
        "title": args.title,
        "notes": args.notes or "",
        "status": "active",
        "schedule": {"type": "once", "value": args.when, "timezone": tz_name},
        "next_run_at": _iso(when),
        "channels": [],
        "backend_refs": {},
        "created_at": _iso(_utc_now()),
        "updated_at": _iso(_utc_now()),
    }

    if args.mirror == "apple":
        reminder["channels"].append("apple_reminders")
        apple_id = _mirror_apple(reminder["title"], reminder["notes"])
        reminder["backend_refs"]["apple_reminders"] = {"id": apple_id}

    if args.notify == "stdout":
        reminder["channels"].append("chat_stdout")

    state["reminders"].append(reminder)
    _atomic_write_json(state_path, state)

    print(json.dumps({"id": rid, "next_run_at": reminder["next_run_at"]}, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    reminders = state.get("reminders", [])
    if args.status:
        reminders = [r for r in reminders if r.get("status") == args.status]
    print(json.dumps(reminders, ensure_ascii=False, indent=2))
    return 0


def cmd_snooze(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    r = _find(state, args.id)
    if not r:
        print(f"not found: {args.id}", file=sys.stderr)
        return 2

    try:
        cur = datetime.fromisoformat(r["next_run_at"].replace("Z", "+00:00"))
    except Exception:
        cur = _utc_now()

    dt = cur + timedelta(minutes=args.minutes)
    r["next_run_at"] = _iso(dt)
    r["updated_at"] = _iso(_utc_now())
    _atomic_write_json(args.state, state)

    print(json.dumps({"id": r["id"], "next_run_at": r["next_run_at"]}, ensure_ascii=False))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    r = _find(state, args.id)
    if not r:
        print(f"not found: {args.id}", file=sys.stderr)
        return 2
    r["status"] = "cancelled"
    r["updated_at"] = _iso(_utc_now())
    _atomic_write_json(args.state, state)
    print(json.dumps({"id": r["id"], "status": r["status"]}, ensure_ascii=False))
    return 0


def cmd_run_due(args: argparse.Namespace) -> int:
    state = _load_state(args.state)
    now = _utc_now()

    fired: List[Dict[str, Any]] = []
    for r in state.get("reminders", []):
        if r.get("status") != "active":
            continue
        try:
            due = datetime.fromisoformat(r["next_run_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if due > now:
            continue

        payload = {
            "id": r.get("id"),
            "title": r.get("title"),
            "notes": r.get("notes"),
            "due": r.get("next_run_at"),
        }

        if "chat_stdout" in (r.get("channels") or []):
            print(json.dumps({"type": "reminder_due", "payload": payload}, ensure_ascii=False))

        # one-shot completion
        r["status"] = "completed"
        r["last_run_at"] = _iso(now)
        r["updated_at"] = _iso(now)
        fired.append(payload)

    _atomic_write_json(args.state, state)
    print(json.dumps({"fired": fired, "count": len(fired)}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reminder-system")
    p.add_argument(
        "--state",
        default=os.path.join(os.path.dirname(__file__), "..", "data", "state.json"),
        help="Path to state.json (source of truth)",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Create a reminder")
    c.add_argument("--title", required=True)
    c.add_argument("--when", required=True, help="YYYY-MM-DD or YYYY-MM-DD HH:MM")
    c.add_argument("--notes", default="")
    c.add_argument("--mirror", choices=["none", "apple"], default="none")
    c.add_argument("--notify", choices=["none", "stdout"], default="stdout")
    c.set_defaults(fn=cmd_create)

    l = sub.add_parser("list", help="List reminders")
    l.add_argument("--status", choices=["active", "completed", "cancelled"])
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("snooze", help="Snooze a reminder")
    s.add_argument("--id", required=True)
    s.add_argument("--minutes", type=int, required=True)
    s.set_defaults(fn=cmd_snooze)

    x = sub.add_parser("cancel", help="Cancel a reminder")
    x.add_argument("--id", required=True)
    x.set_defaults(fn=cmd_cancel)

    r = sub.add_parser("run-due", help="Fire due reminders")
    r.set_defaults(fn=cmd_run_due)

    return p


def main(argv: List[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
