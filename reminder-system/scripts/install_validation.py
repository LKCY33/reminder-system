#!/usr/bin/env python3

import argparse
import json
import re
import shutil
import subprocess
import sys
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

    def run_cmd(self, cmd: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, cwd=str(cwd) if cwd else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

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

        skills_root = self.skill_root.parents[2] / "skills"
        skills_root.mkdir(parents=True, exist_ok=True)
        target_name = self.args.skills_target_name or f"reminder-system-validation-{self.run_id}"
        skills_target = skills_root / target_name
        if skills_target.exists():
            shutil.rmtree(skills_target)
        shutil.copytree(self.skill_root, skills_target, ignore=shutil.ignore_patterns("logs", "__pycache__", "*.pyc", ".DS_Store"))

        self.summary["artifacts"]["skillsInstallRoot"] = str(skills_target)
        self.summary["artifacts"]["skillsInstallCleanupPlanned"] = bool(self.args.cleanup_skills_install)

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
    p.add_argument("mode", choices=["self-check", "preinstall", "install-copy-check", "skills-install-check", "full"], help="Validation stage to run")
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
