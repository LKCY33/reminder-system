#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


REQUIRED_NON_LIVE_STAGES = [
    "self-check",
    "preinstall",
    "install-copy-check",
    "skills-install-check",
]

LIVE_E2E_POLL_SECONDS = 90
LIVE_E2E_POLL_INTERVAL_SECONDS = 3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def run_id_now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S%z")


@dataclass
class CheckResult:
    id: str
    status: str
    message: str
    details: Optional[Dict[str, Any]] = None


@dataclass
class StageResult:
    id: str
    status: str = "pass"
    checks: List[CheckResult] = field(default_factory=list)
    log_file: Optional[str] = None

    def add(self, check_id: str, status: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.checks.append(CheckResult(check_id, status, message, details))
        if status == "fail":
            self.status = "fail"
        elif status == "warn" and self.status != "fail":
            self.status = "warn"


class ValidationRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.script_path = Path(__file__).resolve()
        self.scripts_dir = self.script_path.parent
        self.skill_root = self.scripts_dir.parent
        self.logs_root = Path(args.logs_root).resolve() if args.logs_root else (self.skill_root / "logs" / "install-validation")
        self.run_id = args.run_id or run_id_now()
        self.run_dir = self.logs_root / self.run_id
        self.artifacts_dir = self.run_dir / "artifacts"
        self.validation_state = Path(args.validation_state).resolve() if args.validation_state else (self.artifacts_dir / "validation-state.json")
        self.install_root = Path(args.install_root).resolve() if args.install_root else (self.artifacts_dir / "installed-copy")
        self.py = sys.executable or "/usr/bin/python3"

        self.summary: Dict[str, Any] = {
            "runId": self.run_id,
            "mode": args.mode,
            "startedAt": utc_now().isoformat(),
            "finishedAt": None,
            "skillRoot": str(self.skill_root),
            "runStatus": None,
            "highestCompletedStage": None,
            "recommendedNextStep": None,
            "stages": [],
            "artifacts": {},
            "userReminders": [],
        }

    def ensure_dirs(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def write_stage_log(self, stage_id: str, lines: List[str]) -> str:
        mapping = {
            "self-check": "01-self-check.log",
            "preinstall": "02-preinstall.log",
            "install-copy-check": "03-install-copy-check.log",
            "skills-install-check": "04-skills-install-check.log",
            "live-e2e": "05-live-e2e.log",
        }
        name = mapping.get(stage_id, f"{stage_id}.log")
        path = self.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return str(path)

    def record_stage(self, stage: StageResult) -> None:
        self.summary["stages"].append(
            {
                "id": stage.id,
                "status": stage.status,
                "logFile": stage.log_file,
                "checks": [
                    {
                        "id": c.id,
                        "status": c.status,
                        "message": c.message,
                        **({"details": c.details} if c.details else {}),
                    }
                    for c in stage.checks
                ],
            }
        )

    def run_cmd(self, cmd: List[str], cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _write_seed_state(self, path: Path) -> None:
        state = {
            "version": 1,
            "timezone": "Asia/Shanghai",
            "defaults": {
                "lookahead_minutes": 60,
                "offset_minutes": -10,
                "route": {
                    "channel": self.args.default_channel,
                    "target": self.args.default_target,
                },
            },
            "reminders": [],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _parse_json_output(self, stdout: str) -> Any:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise ValueError("no JSON output found")

    def _copy_skill_root(self, dst: Path) -> None:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(self.skill_root, dst, ignore=shutil.ignore_patterns("logs", "__pycache__", "*.pyc", ".DS_Store"))

    def _workspace_root(self) -> Path:
        return self.skill_root.parents[2]

    def _skills_root(self) -> Path:
        return self._workspace_root() / "skills"

    def _prepare_notify_skill_target(self) -> Path:
        source = self._workspace_root() / "repositories" / "notify"
        target = self._skills_root() / "notify"
        if not source.exists():
            raise FileNotFoundError(f"notify repository not found at {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("logs", "__pycache__", "*.pyc", ".DS_Store"))
        return target

    def _write_notify_test_request(self, path: Path, dedupe_key: str) -> None:
        payload = {
            "source": "reminder-system-install-validation",
            "kind": "handoff_test",
            "event_id": f"evt:{dedupe_key}",
            "dedupe_key": dedupe_key,
            "channel": self.args.default_channel,
            "target": self.args.default_target,
            "title": "handoff validation",
            "body": "installed-to-installed handoff validation",
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _read_reminder(self, state_path: Path, reminder_id: str) -> Optional[Dict[str, Any]]:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        for reminder in state.get("reminders", []):
            if isinstance(reminder, dict) and reminder.get("id") == reminder_id:
                return reminder
        return None

    def stage_self_check(self) -> StageResult:
        stage = StageResult(id="self-check")
        lines: List[str] = []

        required = [
            self.skill_root / "SKILL.md",
            self.skill_root / "scripts" / "reminder_system.py",
            self.skill_root / "scripts" / "run_due_and_notify.py",
            self.skill_root / "scripts" / "scheduler_lookahead.py",
            self.skill_root / "scripts" / "cron_reminder_scheduler.sh",
            self.skill_root / "data",
        ]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            stage.add("required-files", "fail", "required files missing", {"missing": missing})
        else:
            stage.add("required-files", "pass", "required files present")
        lines.append("[required-files]")
        lines.extend(missing or ["all required files present"])

        critical_files = [
            self.skill_root / "scripts" / "reminder_system.py",
            self.skill_root / "scripts" / "run_due_and_notify.py",
            self.skill_root / "scripts" / "scheduler_lookahead.py",
            self.skill_root / "scripts" / "cron_reminder_scheduler.sh",
        ]
        bad_hits: List[str] = []
        suspicious_patterns = [r"/Users/", r"repositories/reminder-system", r"skills/reminder-system"]
        for file_path in critical_files:
            try:
                text = file_path.read_text(encoding="utf-8")
            except Exception as e:
                bad_hits.append(f"{file_path}: read error: {e}")
                continue
            for pattern in suspicious_patterns:
                for m in re.finditer(pattern, text):
                    bad_hits.append(f"{file_path}: matched {pattern} at {m.start()}")
        if bad_hits:
            stage.add("path-coupling-scan", "warn", "found potentially environment-specific path references", {"hits": bad_hits})
        else:
            stage.add("path-coupling-scan", "pass", "no obvious repo/install hardcoded path coupling found in critical chain")
        lines.append("[path-coupling-scan]")
        lines.extend(bad_hits or ["no suspicious matches"])

        cron_wrapper = (self.skill_root / "scripts" / "cron_reminder_scheduler.sh").read_text(encoding="utf-8")
        wrapper_ok = all(token in cron_wrapper for token in ["REMINDER_SYSTEM_ROOT", "REMINDER_SYSTEM_STATE", "REMINDER_SYSTEM_LOG_DIR"])
        if wrapper_ok:
            stage.add("wrapper-overrides", "pass", "cron wrapper supports expected environment overrides")
        else:
            stage.add("wrapper-overrides", "fail", "cron wrapper missing expected environment overrides")
        lines.append("[wrapper-overrides]")
        lines.append("ok" if wrapper_ok else "missing required override tokens")

        deps = {
            "python3": shutil.which("python3") or "/usr/bin/python3",
            "openclaw": shutil.which("openclaw"),
            "remindctl": shutil.which("remindctl"),
        }
        dep_fail: List[str] = []
        dep_warn: List[str] = []
        if not deps["python3"] or not Path(deps["python3"]).exists():
            dep_fail.append("python3")
        if not deps["openclaw"]:
            dep_fail.append("openclaw")
        if not deps["remindctl"]:
            dep_warn.append("remindctl")
        if dep_fail:
            stage.add("dependencies", "fail", "required dependency missing", {"missing": dep_fail, "warn": dep_warn, "resolved": deps})
        elif dep_warn:
            stage.add("dependencies", "warn", "optional dependency missing", {"warn": dep_warn, "resolved": deps})
        else:
            stage.add("dependencies", "pass", "required dependencies available", {"resolved": deps})
        lines.append("[dependencies]")
        lines.append(json.dumps(deps, ensure_ascii=False))

        notify_env = os.environ.copy()
        notify_env["PYTHONPATH"] = str(self.scripts_dir) + (os.pathsep + notify_env["PYTHONPATH"] if notify_env.get("PYTHONPATH") else "")
        notify_probe = self.run_cmd(
            [
                self.py,
                "-c",
                "import json; from notify_client import explain_notify_resolution; print(json.dumps(explain_notify_resolution(), ensure_ascii=False))",
            ],
            cwd=self.scripts_dir,
            env=notify_env,
        )
        lines.append("[notify-resolution]")
        lines.append(notify_probe.stdout)
        lines.append(notify_probe.stderr)
        if notify_probe.returncode != 0:
            stage.add("notify-resolution", "fail", "failed to inspect notify executor resolution")
        else:
            try:
                notify_info = self._parse_json_output(notify_probe.stdout)
                resolved = notify_info.get("resolved")
                if resolved:
                    stage.add("notify-resolution", "pass", "notify executor resolved", {"resolved": resolved, "searched": notify_info.get("searched")})
                else:
                    stage.add("notify-resolution", "fail", "notify executor did not resolve", {"details": notify_info})
            except Exception as e:
                stage.add("notify-resolution", "fail", f"notify resolution output parse failed: {e}")

        stage.log_file = self.write_stage_log(stage.id, lines)
        return stage

    def stage_preinstall(self) -> StageResult:
        stage = StageResult(id="preinstall")
        lines: List[str] = []
        self._write_seed_state(self.validation_state)
        self.summary["artifacts"]["validationState"] = str(self.validation_state)

        reminder_script = self.skill_root / "scripts" / "reminder_system.py"
        run_due_script = self.skill_root / "scripts" / "run_due_and_notify.py"
        scheduler_script = self.skill_root / "scripts" / "scheduler_lookahead.py"

        when = (datetime.now().astimezone() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M")
        create_cmd = [
            self.py, str(reminder_script), "--state", str(self.validation_state), "create",
            "--title", "install-validation-active",
            "--when", when,
            "--offset-minutes", "0",
            "--notes", "validation active reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_cmd)
        lines.append("$ " + " ".join(create_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode != 0:
            stage.add("create-reminder", "fail", "failed to create active reminder")
            stage.log_file = self.write_stage_log(stage.id, lines)
            return stage
        created = self._parse_json_output(p.stdout)
        rid = created.get("id")
        stage.add("create-reminder", "pass", "created active reminder", {"id": rid})

        list_cmd = [self.py, str(reminder_script), "--state", str(self.validation_state), "list"]
        p = self.run_cmd(list_cmd)
        lines.append("$ " + " ".join(list_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("list-reminders", "pass", "list command succeeded")
        else:
            stage.add("list-reminders", "fail", "list command failed")

        snooze_cmd = [self.py, str(reminder_script), "--state", str(self.validation_state), "snooze", "--id", rid, "--minutes", "5"]
        p = self.run_cmd(snooze_cmd)
        lines.append("$ " + " ".join(snooze_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("snooze-reminder", "pass", "snooze command succeeded")
        else:
            stage.add("snooze-reminder", "fail", "snooze command failed")

        cancel_when = (datetime.now().astimezone() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
        create_cancel_cmd = [
            self.py, str(reminder_script), "--state", str(self.validation_state), "create",
            "--title", "install-validation-cancel",
            "--when", cancel_when,
            "--offset-minutes", "0",
            "--notes", "validation cancel reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_cancel_cmd)
        lines.append("$ " + " ".join(create_cancel_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode != 0:
            stage.add("create-cancel-sample", "fail", "failed to create cancel sample reminder")
        else:
            cancel_created = self._parse_json_output(p.stdout)
            cancel_id = cancel_created.get("id")
            cancel_cmd = [self.py, str(reminder_script), "--state", str(self.validation_state), "cancel", "--id", cancel_id]
            p2 = self.run_cmd(cancel_cmd)
            lines.append("$ " + " ".join(cancel_cmd))
            lines.append(p2.stdout)
            lines.append(p2.stderr)
            if p2.returncode == 0:
                stage.add("cancel-reminder", "pass", "cancel command succeeded")
            else:
                stage.add("cancel-reminder", "fail", "cancel command failed")

        due_state = self.artifacts_dir / "due-dry-run-state.json"
        self._write_seed_state(due_state)
        due_when = (datetime.now().astimezone() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        create_due_cmd = [
            self.py, str(reminder_script), "--state", str(due_state), "create",
            "--title", "install-validation-due",
            "--when", due_when,
            "--offset-minutes", "0",
            "--notes", "validation due reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_due_cmd)
        lines.append("$ " + " ".join(create_due_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode != 0:
            stage.add("create-due-sample", "fail", "failed to create due sample reminder")
        else:
            due_created = self._parse_json_output(p.stdout)
            due_id = due_created.get("id")
            dry_run_cmd = [self.py, str(run_due_script), "--state", str(due_state), "--id", due_id, "--dry-run"]
            p2 = self.run_cmd(dry_run_cmd)
            lines.append("$ " + " ".join(dry_run_cmd))
            lines.append(p2.stdout)
            lines.append(p2.stderr)
            if p2.returncode == 0:
                try:
                    payload = self._parse_json_output(p2.stdout)
                    payload_path = self.artifacts_dir / "due-dry-run-payload.json"
                    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    self.summary["artifacts"]["dueDryRunPayload"] = str(payload_path)
                    stage.add("run-due-dry-run", "pass", "due dry-run succeeded", {"payloadPath": str(payload_path)})
                except Exception as e:
                    stage.add("run-due-dry-run", "warn", f"dry-run succeeded but payload parse failed: {e}")
            else:
                stage.add("run-due-dry-run", "fail", "due dry-run failed")

        lookahead_cmd = [self.py, str(scheduler_script), "--state", str(self.validation_state), "--lookahead-minutes", "120", "--dry-run"]
        p = self.run_cmd(lookahead_cmd)
        lines.append("$ " + " ".join(lookahead_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            try:
                planned = self._parse_json_output(p.stdout)
                planned_path = self.artifacts_dir / "preinstall-lookahead-dry-run.json"
                planned_path.write_text(json.dumps(planned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.summary["artifacts"]["preinstallLookaheadDryRun"] = str(planned_path)
                count = int(planned.get("count", 0)) if isinstance(planned, dict) else 0
                if count >= 1:
                    stage.add("lookahead-dry-run", "pass", "lookahead dry-run planned upcoming reminders", {"count": count})
                else:
                    stage.add("lookahead-dry-run", "warn", "lookahead dry-run succeeded but planned no reminders", {"count": count})
            except Exception as e:
                stage.add("lookahead-dry-run", "warn", f"lookahead dry-run succeeded but output parse failed: {e}")
        else:
            stage.add("lookahead-dry-run", "fail", "lookahead dry-run failed")

        daily_state = self.artifacts_dir / "daily-preinstall-state.json"
        self._write_seed_state(daily_state)
        daily_create_cmd = [
            self.py, str(reminder_script), "--state", str(daily_state), "create",
            "--title", "install-validation-daily",
            "--schedule", "daily",
            "--time", "00:00",
            "--offset-minutes", "0",
            "--notes", "validation daily reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(daily_create_cmd)
        lines.append("$ " + " ".join(daily_create_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode != 0:
            stage.add("daily-create", "fail", "failed to create daily reminder")
        else:
            created = self._parse_json_output(p.stdout)
            daily_id = created.get("id")

            def load_daily_item() -> Optional[Dict[str, Any]]:
                daily_list_cmd = [self.py, str(reminder_script), "--state", str(daily_state), "list"]
                out = self.run_cmd(daily_list_cmd)
                lines.append("$ " + " ".join(daily_list_cmd))
                lines.append(out.stdout)
                lines.append(out.stderr)
                items = json.loads(out.stdout)
                return next((item for item in items if isinstance(item, dict) and item.get("id") == daily_id), None)

            before_item = load_daily_item()
            if before_item:
                forced_due = "2026-03-12T16:00:00Z"

                def force_daily_due() -> None:
                    daily_data = json.loads(daily_state.read_text(encoding="utf-8"))
                    for reminder in daily_data.get("reminders", []):
                        if reminder.get("id") == daily_id:
                            reminder["next_run_at"] = forced_due
                            reminder["event_at"] = forced_due
                            reminder["updated_at"] = utc_now().isoformat().replace("+00:00", "Z")
                    daily_state.write_text(json.dumps(daily_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

                def run_daily(status: str, reason: str = "") -> tuple[int, Dict[str, Any], Optional[Dict[str, Any]]]:
                    force_daily_due()
                    env = os.environ.copy()
                    env["NOTIFY_DEDUPE_PATH"] = str(self.artifacts_dir / f"daily-preinstall-{status}-dedupe.txt")
                    if status == "success":
                        env["NOTIFY_TEST_MODE"] = "success"
                    else:
                        env["REMINDER_NOTIFY_INJECT_FAILURE_FOR_ID"] = str(daily_id)
                        env["REMINDER_NOTIFY_INJECT_FAILURE_STATUS"] = status
                        env["REMINDER_NOTIFY_INJECT_FAILURE_REASON"] = reason or f"validation-only injected {status}"
                    cmd = [self.py, str(run_due_script), "--state", str(daily_state), "--id", str(daily_id)]
                    proc = self.run_cmd(cmd, env=env)
                    lines.append("$ " + " ".join(cmd))
                    lines.append(proc.stdout)
                    lines.append(proc.stderr)
                    summary = self._parse_json_output(proc.stdout) if proc.stdout.strip() else {}
                    after_item = load_daily_item()
                    return proc.returncode, summary, after_item

                rc_success, out_success, after_success = run_daily("success")
                if rc_success != 0:
                    stage.add("daily-advance-success", "fail", "daily success path did not return zero")
                else:
                    try:
                        forced_dt = datetime.fromisoformat(forced_due.replace("Z", "+00:00"))
                        after_dt = datetime.fromisoformat(str(after_success.get("next_run_at")).replace("Z", "+00:00")) if after_success else None
                        advanced_by = (after_dt - forced_dt) if after_dt else None
                    except Exception:
                        advanced_by = None
                    if after_success and after_success.get("status") == "active" and after_success.get("next_run_at") != forced_due and advanced_by is not None and advanced_by >= timedelta(hours=23) and out_success.get("any_failure") is False:
                        stage.add("daily-advance-success", "pass", "daily success advanced to next occurrence", {"forcedDue": forced_due, "after": after_success.get("next_run_at"), "advancedBySeconds": int(advanced_by.total_seconds())})
                    else:
                        stage.add("daily-advance-success", "fail", "daily success did not advance correctly", {"summary": out_success, "after": after_success})

                rc_fail, out_fail, after_fail = run_daily("invalid_request", "validation-only injected invalid request")
                if rc_fail != 1:
                    stage.add("daily-no-advance-on-failure", "fail", "daily failure path should return non-zero", {"returncode": rc_fail})
                else:
                    if after_fail and after_fail.get("next_run_at") == forced_due and after_fail.get("status") == "active" and out_fail.get("any_failure") is True:
                        stage.add("daily-no-advance-on-failure", "pass", "daily failure kept current occurrence in place", {"forcedDue": forced_due, "counts": out_fail.get("counts")})
                    else:
                        stage.add("daily-no-advance-on-failure", "fail", "daily failure incorrectly advanced or changed state", {"summary": out_fail, "after": after_fail})
            else:
                stage.add("daily-advance-success", "fail", "daily reminder was not visible in list output")

        stage.log_file = self.write_stage_log(stage.id, lines)
        return stage

    def stage_install_copy_check(self) -> StageResult:
        stage = StageResult(id="install-copy-check")
        lines: List[str] = []
        self._copy_skill_root(self.install_root)
        self.summary["artifacts"]["installRoot"] = str(self.install_root)
        self.summary["artifacts"]["installRootCleanupPlanned"] = bool(self.args.cleanup_install_copy)

        reminder_script = self.install_root / "scripts" / "reminder_system.py"
        run_due_script = self.install_root / "scripts" / "run_due_and_notify.py"
        scheduler_script = self.install_root / "scripts" / "scheduler_lookahead.py"

        install_state = self.artifacts_dir / "installed-copy-state.json"
        self._write_seed_state(install_state)

        when = (datetime.now().astimezone() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
        create_cmd = [
            self.py, str(reminder_script), "--state", str(install_state), "create",
            "--title", "install-copy-active",
            "--when", when,
            "--offset-minutes", "0",
            "--notes", "installed copy active reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_cmd)
        lines.append("$ " + " ".join(create_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("install-copy-create", "pass", "installed copy create succeeded")
        else:
            stage.add("install-copy-create", "fail", "installed copy create failed")
            stage.log_file = self.write_stage_log(stage.id, lines)
            return stage

        list_cmd = [self.py, str(reminder_script), "--state", str(install_state), "list"]
        p = self.run_cmd(list_cmd)
        lines.append("$ " + " ".join(list_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("install-copy-list", "pass", "installed copy list succeeded")
        else:
            stage.add("install-copy-list", "fail", "installed copy list failed")

        due_state = self.artifacts_dir / "installed-copy-due-state.json"
        self._write_seed_state(due_state)
        due_when = (datetime.now().astimezone() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        create_due_cmd = [
            self.py, str(reminder_script), "--state", str(due_state), "create",
            "--title", "install-copy-due",
            "--when", due_when,
            "--offset-minutes", "0",
            "--notes", "installed copy due reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_due_cmd)
        lines.append("$ " + " ".join(create_due_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode != 0:
            stage.add("install-copy-create-due", "fail", "failed to create installed-copy due sample")
        else:
            created = self._parse_json_output(p.stdout)
            due_id = created.get("id")
            dry_run_cmd = [self.py, str(run_due_script), "--state", str(due_state), "--id", due_id, "--dry-run"]
            p2 = self.run_cmd(dry_run_cmd)
            lines.append("$ " + " ".join(dry_run_cmd))
            lines.append(p2.stdout)
            lines.append(p2.stderr)
            if p2.returncode == 0:
                stage.add("install-copy-run-due-dry-run", "pass", "installed copy due dry-run succeeded")
            else:
                stage.add("install-copy-run-due-dry-run", "fail", "installed copy due dry-run failed")

        lookahead_cmd = [self.py, str(scheduler_script), "--state", str(install_state), "--lookahead-minutes", "120", "--dry-run"]
        p = self.run_cmd(lookahead_cmd)
        lines.append("$ " + " ".join(lookahead_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("install-copy-lookahead-dry-run", "pass", "installed copy lookahead dry-run succeeded")
        else:
            stage.add("install-copy-lookahead-dry-run", "fail", "installed copy lookahead dry-run failed")

        installed_scheduler_text = scheduler_script.read_text(encoding="utf-8")
        if str(self.install_root) in installed_scheduler_text:
            stage.add("install-copy-path-embedding", "warn", "installed scheduler script text contains install-root absolute path")
        else:
            stage.add("install-copy-path-embedding", "pass", "installed scheduler script does not embed install-root absolute path text")

        expected_run_due_path = str(self.install_root / "scripts" / "run_due_and_notify.py")
        synthesized_msg = "__CRON_EXEC__ /usr/bin/python3 " + expected_run_due_path + " --state " + str(install_state) + " --id SAMPLE"
        msg_path = self.artifacts_dir / "install-copy-expected-cron-message.txt"
        msg_path.write_text(synthesized_msg + "\n", encoding="utf-8")
        self.summary["artifacts"]["installCopyExpectedCronMessage"] = str(msg_path)
        repo_run_due_path = str(self.skill_root / "scripts" / "run_due_and_notify.py")
        if expected_run_due_path == repo_run_due_path:
            stage.add("install-copy-cron-target", "fail", "installed-copy expected cron target unexpectedly equals repository script path")
        elif repo_run_due_path in synthesized_msg:
            stage.add("install-copy-cron-target", "fail", "synthesized installed-copy cron message still embeds repository script path")
        else:
            stage.add("install-copy-cron-target", "pass", "installed-copy cron message targets installed-copy script path", {"messagePath": str(msg_path)})

        stage.log_file = self.write_stage_log(stage.id, lines)
        return stage

    def stage_skills_install_check(self) -> StageResult:
        stage = StageResult(id="skills-install-check")
        lines: List[str] = []

        skills_root = self._skills_root()
        skills_root.mkdir(parents=True, exist_ok=True)
        target_name = self.args.skills_target_name or f"reminder-system-validation-{self.run_id}"
        skills_target = skills_root / target_name
        if skills_target.exists():
            shutil.rmtree(skills_target)
        shutil.copytree(self.skill_root, skills_target, ignore=shutil.ignore_patterns("logs", "__pycache__", "*.pyc", ".DS_Store"))

        notify_target = self._prepare_notify_skill_target()

        self.summary["artifacts"]["skillsInstallRoot"] = str(skills_target)
        self.summary["artifacts"]["skillsInstallCleanupPlanned"] = bool(self.args.cleanup_skills_install)
        self.summary["artifacts"]["skillsNotifyRoot"] = str(notify_target)

        reminder_script = skills_target / "scripts" / "reminder_system.py"
        run_due_script = skills_target / "scripts" / "run_due_and_notify.py"
        scheduler_script = skills_target / "scripts" / "scheduler_lookahead.py"

        skills_state = self.artifacts_dir / "skills-install-state.json"
        self._write_seed_state(skills_state)

        when = (datetime.now().astimezone() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
        create_cmd = [
            self.py, str(reminder_script), "--state", str(skills_state), "create",
            "--title", "skills-install-active",
            "--when", when,
            "--offset-minutes", "0",
            "--notes", "skills install active reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_cmd)
        lines.append("$ " + " ".join(create_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("skills-install-create", "pass", "real skills-target create succeeded")
        else:
            stage.add("skills-install-create", "fail", "real skills-target create failed")
            stage.log_file = self.write_stage_log(stage.id, lines)
            return stage

        list_cmd = [self.py, str(reminder_script), "--state", str(skills_state), "list"]
        p = self.run_cmd(list_cmd)
        lines.append("$ " + " ".join(list_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("skills-install-list", "pass", "real skills-target list succeeded")
        else:
            stage.add("skills-install-list", "fail", "real skills-target list failed")

        due_state = self.artifacts_dir / "skills-install-due-state.json"
        self._write_seed_state(due_state)
        due_when = (datetime.now().astimezone() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        create_due_cmd = [
            self.py, str(reminder_script), "--state", str(due_state), "create",
            "--title", "skills-install-due",
            "--when", due_when,
            "--offset-minutes", "0",
            "--notes", "skills install due reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p = self.run_cmd(create_due_cmd)
        lines.append("$ " + " ".join(create_due_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        due_id = None
        if p.returncode != 0:
            stage.add("skills-install-create-due", "fail", "failed to create real skills-target due sample")
        else:
            created = self._parse_json_output(p.stdout)
            due_id = created.get("id")
            dry_run_cmd = [self.py, str(run_due_script), "--state", str(due_state), "--id", due_id, "--dry-run"]
            p2 = self.run_cmd(dry_run_cmd)
            lines.append("$ " + " ".join(dry_run_cmd))
            lines.append(p2.stdout)
            lines.append(p2.stderr)
            if p2.returncode == 0:
                stage.add("skills-install-run-due-dry-run", "pass", "real skills-target due dry-run succeeded")
            else:
                stage.add("skills-install-run-due-dry-run", "fail", "real skills-target due dry-run failed")

        lookahead_cmd = [self.py, str(scheduler_script), "--state", str(skills_state), "--lookahead-minutes", "120", "--dry-run"]
        p = self.run_cmd(lookahead_cmd)
        lines.append("$ " + " ".join(lookahead_cmd))
        lines.append(p.stdout)
        lines.append(p.stderr)
        if p.returncode == 0:
            stage.add("skills-install-lookahead-dry-run", "pass", "real skills-target lookahead dry-run succeeded")
        else:
            stage.add("skills-install-lookahead-dry-run", "fail", "real skills-target lookahead dry-run failed")

        expected_run_due_path = str(skills_target / "scripts" / "run_due_and_notify.py")
        repo_run_due_path = str(self.skill_root / "scripts" / "run_due_and_notify.py")
        if expected_run_due_path == repo_run_due_path:
            stage.add("skills-install-cron-target", "fail", "skills-install expected cron target unexpectedly equals repository script path")
        else:
            stage.add("skills-install-cron-target", "pass", "skills-install expected cron target differs from repository script path")

        handoff_env = os.environ.copy()
        handoff_env["PYTHONPATH"] = str(skills_target / "scripts") + (os.pathsep + handoff_env["PYTHONPATH"] if handoff_env.get("PYTHONPATH") else "")
        handoff_env["NOTIFY_TEST_MODE"] = "success"
        handoff_env["NOTIFY_DEDUPE_PATH"] = str(self.artifacts_dir / "handoff-notify-dedupe.txt")

        resolution_probe = self.run_cmd(
            [self.py, "-c", "import json; from notify_client import explain_notify_resolution; print(json.dumps(explain_notify_resolution(), ensure_ascii=False))"],
            cwd=skills_target / "scripts",
            env=handoff_env,
        )
        lines.append("[handoff-resolution]")
        lines.append(resolution_probe.stdout)
        lines.append(resolution_probe.stderr)
        if resolution_probe.returncode != 0:
            stage.add("skills-handoff-resolution", "fail", "failed to inspect installed-to-installed notify resolution")
        else:
            try:
                info = self._parse_json_output(resolution_probe.stdout)
                resolved = info.get("resolved")
                if resolved == str(notify_target / "run" / "execute-request.py"):
                    stage.add("skills-handoff-resolution", "pass", "installed reminder-system resolved installed notify", {"resolved": resolved})
                else:
                    stage.add("skills-handoff-resolution", "fail", "installed reminder-system did not resolve installed notify", {"resolved": resolved, "searched": info.get("searched")})
            except Exception as e:
                stage.add("skills-handoff-resolution", "fail", f"handoff resolution parse failed: {e}")

        if due_id:
            run_cmd = [self.py, str(run_due_script), "--state", str(due_state), "--id", str(due_id)]
            p3 = self.run_cmd(run_cmd, env=handoff_env)
            lines.append("[handoff-success]")
            lines.append("$ " + " ".join(run_cmd))
            lines.append(p3.stdout)
            lines.append(p3.stderr)
            if p3.returncode == 0:
                try:
                    handoff_out = self._parse_json_output(p3.stdout)
                    results = handoff_out.get("results") or []
                    if results and results[0].get("status") == "success" and handoff_out.get("any_failure") is False:
                        stage.add("skills-handoff-success", "pass", "installed-to-installed handoff returned success")
                    else:
                        stage.add("skills-handoff-success", "fail", "handoff run did not report clean success", {"output": handoff_out})
                except Exception as e:
                    stage.add("skills-handoff-success", "fail", f"handoff success output parse failed: {e}")
            else:
                stage.add("skills-handoff-success", "fail", "installed-to-installed handoff command failed")

            status_probe = self.run_cmd(
                [self.py, "-c", f"from completion_utils import get_status; print(get_status(r'{due_state}', r'{due_id}'))"],
                cwd=skills_target / "scripts",
                env=handoff_env,
            )
            lines.append("[handoff-completion]")
            lines.append(status_probe.stdout)
            lines.append(status_probe.stderr)
            if status_probe.returncode == 0 and status_probe.stdout.strip() == "completed":
                stage.add("skills-handoff-completion", "pass", "success handoff marked reminder completed")
            else:
                stage.add("skills-handoff-completion", "fail", "success handoff did not mark reminder completed", {"stdout": status_probe.stdout.strip(), "stderr": status_probe.stderr.strip()})

            self._write_seed_state(due_state)
            recreate_due_cmd = [
                self.py, str(reminder_script), "--state", str(due_state), "create",
                "--title", "skills-install-due-duplicate",
                "--when", due_when,
                "--offset-minutes", "0",
                "--notes", "skills install due duplicate reminder",
                "--notify", "stdout",
                "--route-channel", self.args.default_channel,
                "--route-target", self.args.default_target,
            ]
            p4 = self.run_cmd(recreate_due_cmd)
            lines.append("[handoff-duplicate-create]")
            lines.append("$ " + " ".join(recreate_due_cmd))
            lines.append(p4.stdout)
            lines.append(p4.stderr)
            if p4.returncode == 0:
                recreated = self._parse_json_output(p4.stdout)
                duplicate_due_id = recreated.get("id")
                req_path = self.artifacts_dir / "handoff-duplicate-request.json"
                self._write_notify_test_request(req_path, dedupe_key=f"reminder_due:{duplicate_due_id}:DUPLICATE")
                predup_env = dict(handoff_env)
                predup_env["PYTHONPATH"] = str(notify_target / "run") + (os.pathsep + predup_env["PYTHONPATH"] if predup_env.get("PYTHONPATH") else "")
                notify_exec = notify_target / "run" / "execute-request.py"
                predup = self.run_cmd([self.py, str(notify_exec), "--request-file", str(req_path)], cwd=notify_target, env=predup_env)
                lines.append("[handoff-predup]")
                lines.append(predup.stdout)
                lines.append(predup.stderr)

                real_req = self.run_cmd([self.py, str(run_due_script), "--state", str(due_state), "--id", str(duplicate_due_id), "--dry-run"], env=handoff_env)
                lines.append("[handoff-duplicate-dry-run]")
                lines.append(real_req.stdout)
                lines.append(real_req.stderr)
                if real_req.returncode == 0:
                    try:
                        req_payload = self._parse_json_output(real_req.stdout)
                        dup_req_path = self.artifacts_dir / "handoff-duplicate-real-request.json"
                        dup_req_path.write_text(json.dumps(req_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        predup_real = self.run_cmd([self.py, str(notify_exec), "--request-file", str(dup_req_path)], cwd=notify_target, env=predup_env)
                        lines.append("[handoff-predup-real]")
                        lines.append(predup_real.stdout)
                        lines.append(predup_real.stderr)

                        p5 = self.run_cmd([self.py, str(run_due_script), "--state", str(due_state), "--id", str(duplicate_due_id)], env=handoff_env)
                        lines.append("[handoff-duplicate]")
                        lines.append(p5.stdout)
                        lines.append(p5.stderr)
                        if p5.returncode == 0:
                            try:
                                dup_out = self._parse_json_output(p5.stdout)
                                dup_results = dup_out.get("results") or []
                                if dup_results and dup_results[0].get("status") == "duplicate" and dup_out.get("any_failure") is False:
                                    stage.add("skills-handoff-duplicate", "pass", "installed-to-installed handoff returned duplicate when dedupe key already existed")
                                else:
                                    stage.add("skills-handoff-duplicate", "fail", "handoff duplicate path did not report duplicate", {"output": dup_out})
                            except Exception as e:
                                stage.add("skills-handoff-duplicate", "fail", f"handoff duplicate output parse failed: {e}")
                        else:
                            stage.add("skills-handoff-duplicate", "fail", "installed-to-installed duplicate handoff command failed")
                    except Exception as e:
                        stage.add("skills-handoff-duplicate", "fail", f"failed to prepare real duplicate request: {e}")
                else:
                    stage.add("skills-handoff-duplicate", "fail", "failed to build real duplicate dry-run request")
            else:
                stage.add("skills-handoff-duplicate", "fail", "failed to create duplicate test reminder")

        daily_skills_state = self.artifacts_dir / "skills-install-daily-state.json"
        self._write_seed_state(daily_skills_state)
        daily_skills_create_cmd = [
            self.py, str(reminder_script), "--state", str(daily_skills_state), "create",
            "--title", "skills-install-daily",
            "--schedule", "daily",
            "--time", "00:00",
            "--offset-minutes", "0",
            "--notes", "skills install daily reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p_daily = self.run_cmd(daily_skills_create_cmd)
        lines.append("[skills-daily-create]")
        lines.append("$ " + " ".join(daily_skills_create_cmd))
        lines.append(p_daily.stdout)
        lines.append(p_daily.stderr)
        if p_daily.returncode != 0:
            stage.add("skills-daily-create", "fail", "failed to create daily reminder in skills target")
        else:
            daily_created = self._parse_json_output(p_daily.stdout)
            daily_id = str(daily_created.get("id"))

            def load_daily_skills_item() -> Optional[Dict[str, Any]]:
                cmd = [self.py, str(reminder_script), "--state", str(daily_skills_state), "list"]
                out = self.run_cmd(cmd)
                lines.append("$ " + " ".join(cmd))
                lines.append(out.stdout)
                lines.append(out.stderr)
                items = json.loads(out.stdout)
                return next((item for item in items if isinstance(item, dict) and item.get("id") == daily_id), None)

            forced_due = "2026-03-12T16:00:00Z"
            daily_data = json.loads(daily_skills_state.read_text(encoding="utf-8"))
            for reminder in daily_data.get("reminders", []):
                if reminder.get("id") == daily_id:
                    reminder["next_run_at"] = forced_due
                    reminder["event_at"] = forced_due
                    reminder["updated_at"] = utc_now().isoformat().replace("+00:00", "Z")
            daily_skills_state.write_text(json.dumps(daily_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            daily_success_env = dict(handoff_env)
            daily_success_env["NOTIFY_DEDUPE_PATH"] = str(self.artifacts_dir / "skills-daily-success-dedupe.txt")
            success_cmd = [self.py, str(run_due_script), "--state", str(daily_skills_state), "--id", daily_id]
            p_daily_success = self.run_cmd(success_cmd, env=daily_success_env)
            lines.append("[skills-daily-success]")
            lines.append("$ " + " ".join(success_cmd))
            lines.append(p_daily_success.stdout)
            lines.append(p_daily_success.stderr)
            if p_daily_success.returncode != 0:
                stage.add("skills-daily-success", "fail", "skills daily success path failed")
            else:
                out_success = self._parse_json_output(p_daily_success.stdout)
                after_success = load_daily_skills_item()
                if after_success and after_success.get("status") == "active" and after_success.get("next_run_at") != forced_due and out_success.get("any_failure") is False:
                    stage.add("skills-daily-success", "pass", "skills daily success advanced to next occurrence")
                else:
                    stage.add("skills-daily-success", "fail", "skills daily success did not advance correctly", {"summary": out_success, "after": after_success})

            daily_data = json.loads(daily_skills_state.read_text(encoding="utf-8"))
            for reminder in daily_data.get("reminders", []):
                if reminder.get("id") == daily_id:
                    reminder["next_run_at"] = forced_due
                    reminder["event_at"] = forced_due
                    reminder["updated_at"] = utc_now().isoformat().replace("+00:00", "Z")
            daily_skills_state.write_text(json.dumps(daily_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            daily_fail_env = dict(handoff_env)
            daily_fail_env["REMINDER_NOTIFY_INJECT_FAILURE_FOR_ID"] = daily_id
            daily_fail_env["REMINDER_NOTIFY_INJECT_FAILURE_STATUS"] = "invalid_request"
            daily_fail_env["REMINDER_NOTIFY_INJECT_FAILURE_REASON"] = "validation-only injected invalid request"
            fail_cmd = [self.py, str(run_due_script), "--state", str(daily_skills_state), "--id", daily_id]
            p_daily_fail = self.run_cmd(fail_cmd, env=daily_fail_env)
            lines.append("[skills-daily-failure]")
            lines.append("$ " + " ".join(fail_cmd))
            lines.append(p_daily_fail.stdout)
            lines.append(p_daily_fail.stderr)
            if p_daily_fail.returncode != 1:
                stage.add("skills-daily-failure", "fail", "skills daily failure path should return non-zero", {"returncode": p_daily_fail.returncode})
            else:
                out_fail = self._parse_json_output(p_daily_fail.stdout)
                after_fail = load_daily_skills_item()
                if after_fail and after_fail.get("status") == "active" and after_fail.get("next_run_at") == forced_due and out_fail.get("any_failure") is True:
                    stage.add("skills-daily-failure", "pass", "skills daily failure kept current occurrence in place")
                else:
                    stage.add("skills-daily-failure", "fail", "skills daily failure advanced incorrectly", {"summary": out_fail, "after": after_fail})

        mixed_state = self.artifacts_dir / "skills-install-mixed-state.json"
        self._write_seed_state(mixed_state)
        mixed_when = (datetime.now().astimezone() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        mixed_ids: List[str] = []
        for title, notes in [
            ("skills-install-mixed-success", "mixed batch success reminder"),
            ("skills-install-mixed-invalid-request", "mixed batch invalid request reminder"),
        ]:
            create_mixed_cmd = [
                self.py, str(reminder_script), "--state", str(mixed_state), "create",
                "--title", title,
                "--when", mixed_when,
                "--offset-minutes", "0",
                "--notes", notes,
                "--notify", "stdout",
                "--route-channel", self.args.default_channel,
                "--route-target", self.args.default_target,
            ]
            p_mixed = self.run_cmd(create_mixed_cmd)
            lines.append("[handoff-mixed-create]")
            lines.append("$ " + " ".join(create_mixed_cmd))
            lines.append(p_mixed.stdout)
            lines.append(p_mixed.stderr)
            if p_mixed.returncode != 0:
                stage.add("skills-handoff-mixed", "fail", "failed to create mixed batch reminders")
                stage.log_file = self.write_stage_log(stage.id, lines)
                return stage
            mixed_created = self._parse_json_output(p_mixed.stdout)
            mixed_ids.append(str(mixed_created.get("id")))

        mixed_failure_id = mixed_ids[1]
        mixed_env = dict(handoff_env)
        mixed_env["REMINDER_NOTIFY_INJECT_FAILURE_FOR_ID"] = mixed_failure_id
        mixed_env["REMINDER_NOTIFY_INJECT_FAILURE_STATUS"] = "invalid_request"
        mixed_env["REMINDER_NOTIFY_INJECT_FAILURE_REASON"] = "validation-only injected invalid request"

        mixed_cmd = [self.py, str(run_due_script), "--state", str(mixed_state)]
        p_mixed_run = self.run_cmd(mixed_cmd, env=mixed_env)
        lines.append("[handoff-mixed-run]")
        lines.append("$ " + " ".join(mixed_cmd))
        lines.append(p_mixed_run.stdout)
        lines.append(p_mixed_run.stderr)
        if p_mixed_run.returncode != 1:
            stage.add("skills-handoff-mixed", "fail", "mixed batch should exit non-zero when any item fails", {"returncode": p_mixed_run.returncode})
        else:
            try:
                mixed_out = self._parse_json_output(p_mixed_run.stdout)
                mixed_results = mixed_out.get("results") or []
                statuses = {str(item.get("status")) for item in mixed_results}
                if mixed_out.get("any_failure") is True and "success" in statuses and ("invalid_request" in statuses or "delivery_failed" in statuses or "route_failed" in statuses):
                    stage.add("skills-handoff-mixed", "pass", "mixed batch continued after failure and reported combined summary", {"counts": mixed_out.get("counts")})
                else:
                    stage.add("skills-handoff-mixed", "fail", "mixed batch summary did not preserve both success and failure outcomes", {"output": mixed_out})
            except Exception as e:
                stage.add("skills-handoff-mixed", "fail", f"mixed batch output parse failed: {e}")

        stage.log_file = self.write_stage_log(stage.id, lines)
        return stage

    def stage_live_e2e(self) -> StageResult:
        stage = StageResult(id="live-e2e")
        lines: List[str] = []

        skills_root = self._skills_root()
        skills_root.mkdir(parents=True, exist_ok=True)
        target_name = self.args.skills_target_name or f"reminder-system-validation-{self.run_id}"
        skills_target = skills_root / target_name
        if skills_target.exists():
            shutil.rmtree(skills_target)
        shutil.copytree(self.skill_root, skills_target, ignore=shutil.ignore_patterns("logs", "__pycache__", "*.pyc", ".DS_Store"))
        notify_target = self._prepare_notify_skill_target()

        self.summary["artifacts"]["liveSkillsInstallRoot"] = str(skills_target)
        self.summary["artifacts"]["liveSkillsNotifyRoot"] = str(notify_target)

        reminder_script = skills_target / "scripts" / "reminder_system.py"
        scheduler_script = skills_target / "scripts" / "scheduler_lookahead.py"
        run_due_script = skills_target / "scripts" / "run_due_and_notify.py"

        live_state = self.artifacts_dir / "live-e2e-state.json"
        self._write_seed_state(live_state)
        self.summary["artifacts"]["liveState"] = str(live_state)

        fire_time_local = (datetime.now().astimezone() + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
        create_cmd = [
            self.py, str(reminder_script), "--state", str(live_state), "create",
            "--title", f"live-e2e {self.run_id}",
            "--when", fire_time_local,
            "--offset-minutes", "0",
            "--notes", "live e2e validation reminder",
            "--notify", "stdout",
            "--route-channel", self.args.default_channel,
            "--route-target", self.args.default_target,
        ]
        p_create = self.run_cmd(create_cmd)
        lines.append("$ " + " ".join(create_cmd))
        lines.append(p_create.stdout)
        lines.append(p_create.stderr)
        if p_create.returncode != 0:
            stage.add("live-create", "fail", "failed to create live-e2e reminder")
            stage.log_file = self.write_stage_log(stage.id, lines)
            return stage
        created = self._parse_json_output(p_create.stdout)
        live_id = str(created.get("id"))
        stage.add("live-create", "pass", "created live-e2e reminder", {"id": live_id, "when": fire_time_local})

        list_before = self._read_reminder(live_state, live_id)
        if list_before and list_before.get("next_run_at"):
            stage.add("live-state-before", "pass", "live reminder present before scheduling", {"next_run_at": list_before.get("next_run_at")})
        else:
            stage.add("live-state-before", "fail", "live reminder missing before scheduling")
            stage.log_file = self.write_stage_log(stage.id, lines)
            return stage

        schedule_cmd = [self.py, str(scheduler_script), "--state", str(live_state), "--lookahead-minutes", "10"]
        p_schedule = self.run_cmd(schedule_cmd)
        lines.append("$ " + " ".join(schedule_cmd))
        lines.append(p_schedule.stdout)
        lines.append(p_schedule.stderr)
        if p_schedule.returncode != 0:
            stage.add("live-schedule", "fail", "failed to schedule live-e2e reminder")
            stage.log_file = self.write_stage_log(stage.id, lines)
            return stage
        try:
            scheduled = self._parse_json_output(p_schedule.stdout)
        except Exception:
            scheduled = {}
        planned = scheduled.get("planned") or []
        if any(str(item.get("id")) == live_id for item in planned if isinstance(item, dict)):
            stage.add("live-schedule", "pass", "real scheduling path created a one-shot job", {"planned": planned})
        else:
            stage.add("live-schedule", "warn", "scheduler returned without explicit planned item", {"output": scheduled})

        env = os.environ.copy()
        env["PYTHONPATH"] = str(skills_target / "scripts") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["NOTIFY_DEDUPE_PATH"] = str(self.artifacts_dir / "live-e2e-dedupe.txt")
        manual_due_cmd = [self.py, str(run_due_script), "--state", str(live_state), "--id", live_id]

        deadline = datetime.now(timezone.utc) + timedelta(seconds=LIVE_E2E_POLL_SECONDS)
        delivered_summary: Optional[Dict[str, Any]] = None
        poll_count = 0
        auto_completed = False
        while datetime.now(timezone.utc) < deadline:
            poll_count += 1
            reminder = self._read_reminder(live_state, live_id)
            if reminder and reminder.get("status") == "completed":
                auto_completed = True
                stage.add("live-completion", "pass", "live reminder reached completed state via scheduled wait window", {"polls": poll_count})
                break
            time.sleep(LIVE_E2E_POLL_INTERVAL_SECONDS)

        reminder_before_fallback = self._read_reminder(live_state, live_id)
        if not auto_completed:
            stage.add("live-scheduler-window", "warn", "scheduled wait window did not complete the reminder before timeout", {"timeoutSeconds": LIVE_E2E_POLL_SECONDS})
            p_due = self.run_cmd(manual_due_cmd, env=env)
            lines.append("$ " + " ".join(manual_due_cmd))
            lines.append(p_due.stdout)
            lines.append(p_due.stderr)
            if p_due.stdout.strip():
                try:
                    delivered_summary = self._parse_json_output(p_due.stdout)
                except Exception:
                    delivered_summary = None
            if p_due.returncode not in (0, 1):
                stage.add("live-run-due", "fail", "installed live fallback due execution failed unexpectedly", {"returncode": p_due.returncode})
                stage.log_file = self.write_stage_log(stage.id, lines)
                return stage
            stage.add("live-fallback-run-due", "pass", "installed live due path executed after scheduler wait window", {"returncode": p_due.returncode})

        reminder_after = self._read_reminder(live_state, live_id)
        if delivered_summary and delivered_summary.get("any_failure") is False:
            stage.add("live-delivery", "pass", "live due execution reported success", {"summary": delivered_summary})
        elif auto_completed and reminder_after and reminder_after.get("status") == "completed":
            stage.add("live-delivery", "pass", "live reminder completed during scheduled wait window")
        else:
            stage.add("live-delivery", "warn", "live delivery summary missing or not clean", {"summary": delivered_summary})

        if reminder_after and reminder_after.get("status") == "completed":
            stage.add("live-state-after", "pass", "live reminder stayed completed after delivery", {"status": reminder_after.get("status"), "beforeFallback": reminder_before_fallback})
        else:
            stage.add("live-state-after", "fail", "live reminder did not stay completed", {"reminder": reminder_after})

        stage.log_file = self.write_stage_log(stage.id, lines)
        return stage

    def _finalize_cleanup(self) -> None:
        if self.args.cleanup_install_copy:
            removed = False
            if self.install_root.exists():
                shutil.rmtree(self.install_root)
                removed = True
            self.summary["artifacts"]["installRootCleaned"] = removed
        else:
            self.summary["artifacts"]["installRootCleaned"] = False
            if self.install_root.exists():
                cleanup_cmd = f"rm -rf {str(self.install_root)}"
                self.summary["artifacts"]["installRootCleanupCommand"] = cleanup_cmd
                self.summary["userReminders"].append(
                    f"Copied install candidate retained for review at `{self.install_root}`. After review, you can remove it with: `{cleanup_cmd}`"
                )

        skills_root = Path(self.summary["artifacts"].get("skillsInstallRoot", "")) if self.summary["artifacts"].get("skillsInstallRoot") else None
        if self.args.cleanup_skills_install:
            removed = False
            if skills_root and skills_root.exists():
                shutil.rmtree(skills_root)
                removed = True
            self.summary["artifacts"]["skillsInstallRootCleaned"] = removed
        else:
            self.summary["artifacts"]["skillsInstallRootCleaned"] = False
            if skills_root and skills_root.exists():
                cleanup_cmd = f"rm -rf {str(skills_root)}"
                self.summary["artifacts"]["skillsInstallRootCleanupCommand"] = cleanup_cmd
                self.summary["userReminders"].append(
                    f"Skills-layer install target retained for review at `{skills_root}`. After review, you can remove it with: `{cleanup_cmd}`"
                )

    def _finalize_summary(self) -> None:
        self.summary["finishedAt"] = utc_now().isoformat()
        stages = self.summary.get("stages", [])
        any_fail = any(stage["status"] == "fail" for stage in stages)
        any_warn = any(stage["status"] == "warn" for stage in stages)
        completed_ids = [stage["id"] for stage in stages]
        self.summary["highestCompletedStage"] = completed_ids[-1] if completed_ids else None

        if any_fail:
            self.summary["runStatus"] = "fail"
        elif any_warn:
            self.summary["runStatus"] = "pass-with-followups"
        else:
            self.summary["runStatus"] = "pass"

        if self.args.mode == "full":
            passed = all(any(stage["id"] == req and stage["status"] == "pass" for stage in stages) for req in REQUIRED_NON_LIVE_STAGES)
            self.summary["requiredNonLiveGatesPassed"] = passed
            if passed:
                self.summary["recommendedNextStep"] = "consider live-e2e as the final deployment-confidence check"
            else:
                self.summary["recommendedNextStep"] = "investigate the failing non-live gate and re-run full validation"
        else:
            current = self.args.mode
            next_step = {
                "self-check": "run preinstall for repository-mode behavior validation",
                "preinstall": "run install-copy-check for install-artifact validation",
                "install-copy-check": "run skills-install-check for real skills-layer validation",
                "skills-install-check": "run full for a complete non-live readiness evaluation",
            }.get(current, "review this stage result and decide on the next validation step")
            self.summary["recommendedNextStep"] = next_step

    def write_summary(self) -> None:
        (self.run_dir / "summary.json").write_text(json.dumps(self.summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        md_lines = [
            "# Reminder-System Install Validation Summary",
            "",
            f"- Run ID: `{self.summary['runId']}`",
            f"- Mode: `{self.summary['mode']}`",
            f"- Started: `{self.summary['startedAt']}`",
            f"- Finished: `{self.summary['finishedAt']}`",
            f"- Skill Root: `{self.summary['skillRoot']}`",
            f"- Run Status: **{(self.summary.get('runStatus') or 'unknown').upper()}**",
            f"- Highest Completed Stage: `{self.summary.get('highestCompletedStage')}`",
            f"- Recommended Next Step: `{self.summary.get('recommendedNextStep')}`",
        ]
        if self.args.mode == "full":
            md_lines.append(f"- Required Non-Live Gates Passed: `{self.summary.get('requiredNonLiveGatesPassed')}`")
        md_lines.extend(["", "## Stages", ""])

        for stage in self.summary["stages"]:
            md_lines.append(f"### {stage['id']} — {stage['status'].upper()}")
            md_lines.append("")
            if stage.get("logFile"):
                md_lines.append(f"- Log: `{stage['logFile']}`")
            for check in stage.get("checks", []):
                md_lines.append(f"- `{check['status']}` `{check['id']}` — {check['message']}")
            md_lines.append("")

        if self.summary.get("userReminders"):
            md_lines.extend(["## User Reminders", ""])
            for item in self.summary["userReminders"]:
                md_lines.append(f"- {item}")
            md_lines.append("")

        (self.run_dir / "summary.md").write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")

    def run(self) -> int:
        self.ensure_dirs()
        if self.args.mode == "full":
            stages_to_run = REQUIRED_NON_LIVE_STAGES
        else:
            stages_to_run = [self.args.mode]

        for stage_id in stages_to_run:
            if stage_id == "self-check":
                stage = self.stage_self_check()
            elif stage_id == "preinstall":
                stage = self.stage_preinstall()
            elif stage_id == "install-copy-check":
                stage = self.stage_install_copy_check()
            elif stage_id == "skills-install-check":
                stage = self.stage_skills_install_check()
            elif stage_id == "live-e2e":
                stage = self.stage_live_e2e()
            else:
                raise ValueError(f"unsupported mode: {stage_id}")
            self.record_stage(stage)
            if stage.status == "fail" and self.args.fail_fast:
                break

        self._finalize_cleanup()
        self._finalize_summary()
        self.write_summary()
        for item in self.summary.get("userReminders") or []:
            print(item)
        return 0 if self.summary.get("runStatus") != "fail" else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Reminder-system install validation runner")
    p.add_argument("mode", choices=["self-check", "preinstall", "install-copy-check", "skills-install-check", "live-e2e", "full"], help="Validation stage to run")
    p.add_argument("--run-id", default=None, help="Optional run id override")
    p.add_argument("--logs-root", default=None, help="Root directory for validation logs")
    p.add_argument("--validation-state", default=None, help="Path to isolated validation state.json")
    p.add_argument("--install-root", default=None, help="Path to a copied install candidate root")
    p.add_argument("--skills-target-name", default=None, help="Target directory name to use under the real skills layer for skills-install-check")
    p.add_argument("--default-channel", default="feishu", help="Default validation delivery channel")
    p.add_argument("--default-target", default="user:ou_4da26eb40cfb44caee9ad41074668bba", help="Default validation delivery target")
    p.add_argument("--cleanup-install-copy", action="store_true", help="Remove the copied install candidate after validation while keeping logs and other artifacts")
    p.add_argument("--cleanup-skills-install", action="store_true", help="Remove the real skills-layer install target after validation while keeping logs and other artifacts")
    p.add_argument("--fail-fast", action="store_true", help="Stop after first failing stage")
    return p


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runner = ValidationRunner(args)
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
