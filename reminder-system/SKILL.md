---
name: reminder-system
description: Personal reminder system with a JSON source-of-truth, Apple Reminders mirroring (remindctl), and scheduled chat notifications. Use when you need to create/update/snooze/cancel reminders, run due reminders, or build an automation-friendly reminder workflow that can later be exposed via MCP tools.
---

# Reminder System

A lightweight reminder orchestrator designed to be machine-friendly.

It keeps reminders in a local JSON file (source of truth), optionally mirrors them into Apple Reminders for visibility, and can emit chat notifications when reminders are due.

## Core Concepts

- Source of truth: `data/state.json`
- Backends:
  - Apple Reminders (via `remindctl`) as a mirror
  - Chat notification (initially: print payload; integrate with OpenClaw messaging at the call site)
- Scheduler model: a periodic job calls `scripts/scheduler_lookahead.py` to pre-schedule one-shot jobs, and the one-shot jobs trigger notifications.

## Quick Start

Create a one-time reminder:

```bash
python3 scripts/reminder_system.py create \
  --title "推进 RAG PoC" \
  --when "2026-03-05 10:00" \
  --mirror apple
```

List reminders:

```bash
python3 scripts/reminder_system.py list
```

Schedule one-shot jobs for reminders in the lookahead window:

```bash
python3 scripts/scheduler_lookahead.py --lookahead-minutes 60
```

Run due reminders (debug):

```bash
python3 scripts/reminder_system.py run-due
```

Snooze a reminder:

```bash
python3 scripts/reminder_system.py snooze --id <id> --minutes 120
```

Cancel a reminder:

```bash
python3 scripts/reminder_system.py cancel --id <id>
```

## Data Model (state.json)

- Stored at `data/state.json`.
- Written atomically (temp + rename).
- Minimal fields:
  - `id`, `title`, `notes`, `status`, `schedule`, `next_run_at`, `channels`, `backend_refs`

## Notes / Boundaries

- Apple Reminders is treated as a mirror only; complex recurrence lives in `state.json`.
- Quiet hours and retry policies can be added later without changing the public CLI surface.
