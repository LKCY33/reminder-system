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
  - Chat notification via OpenClaw messaging as the current pragmatic delivery path
- Scheduler model: a periodic job calls `scripts/scheduler_lookahead.py` to pre-schedule one-shot jobs, and the one-shot jobs trigger notifications.
- Cron execution contract: agent cron jobs use a message prefix `__CRON_EXEC__ <cmd>`, defined in workspace `BOOT.md`, which instructs the agent to execute `<cmd>` locally via `exec`.

## Operational Behavior

### Path Behavior

- By default, runtime paths resolve relative to the skill root.
- The same relative layout is intended to work in both repository and installed-skill locations.
- Most normal usage should not need `--state`.
- Use `--state` when you explicitly want to point the skill at a different state file.
- Wrapper scripts may also honor environment overrides such as `REMINDER_SYSTEM_ROOT` and `REMINDER_SYSTEM_STATE`.

### Current Delivery Behavior

- The reminder system currently hands due-notification execution to `notify` through a small client boundary.
- The runtime resolution strategy is:
  - honor `NOTIFY_EXECUTOR` if set
  - else honor `NOTIFY_ROOT` if set
  - else prefer a colocated installed-skill path under `skills/notify`
  - else fall back to the repository development copy under `repositories/notify`
- Long-term direction remains the same: reminder orchestration stays here, while generic notification delivery concerns live in `notify`.

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

- Stored at `data/state.json` by default.
- Written atomically (temp + rename).
- Minimal fields:
  - `id`, `title`, `notes`, `status`, `schedule`, `next_run_at`, `channels`, `backend_refs`

## Dependencies

Current expected runtime dependencies include:

- `/usr/bin/python3`
- `openclaw`
- `remindctl` (only when Apple Reminders mirroring is used)

## Validation

Repository-side install validation is now expected to run through:

- `scripts/install_validation.py`

Current validation layering:

- `self-check` — structural sanity check
- `preinstall` — repository-mode behavior validation with isolated state
- `install-copy-check` — copied install-artifact validation
- `skills-install-check` — real `skills/` target validation

Validation run semantics:

- stage-local modes are for local or incremental validation only
- `full` is the canonical non-live readiness-review run
- `live-e2e` remains a later explicit real scheduling / delivery confidence validation layer

## Notes / Boundaries

- Apple Reminders is treated as a mirror only; complex recurrence lives in `state.json`.
- This skill owns reminder orchestration concerns, not generic notification-delivery governance.
- Quiet hours and retry policies can be added later without changing the public CLI surface.
