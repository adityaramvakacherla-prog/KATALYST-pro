"""
orchestrator.py — KATALYST Orchestrator Agent
Traffic controller. Assigns tasks, monitors bus, saves files, handles failures.
Includes Validator + Tester steps before saving — catches bad code before /output.
Uses Cerebras llama-3.3-70b for routing decisions.

FIXES APPLIED (cumulative):
- Bug 7: health_monitor.watch_agent() called for every coder thread
- Bug 8: Planner enrichment fallback for JSON uploads + plan_ready bus message handled
- Bug 10: Architect blueprint generated from project description on JSON uploads
- Fix 8 (Tester): Tester agent wired in after Validator
- Fix 9: TASK_TIMEOUT raised 180→300s so 3 full Debugger attempts always fit
- Fix 9: clear_old() called at project start — stale bus messages purged
- Fix 9: resolve_code() used everywhere so code is read from file cache not bus JSON
- Fix 10: If reviewer score < 8 but still PASS, Orchestrator sends "recode" signal
           to Coder to write the file again from scratch before Debugger is tried
- Fix 11: Validator and Tester always wired in — never skipped
- Fix 12: Old output files are NOT cleared here (server.py handles that on startup)
           to avoid wiping output mid-project on bus reconnect
"""
import os
import sys
import json
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_bus
import agent_memory
import agent_chat
from coder          import Coder
from reviewer       import Reviewer
from debugger       import Debugger
from validator      import Validator
from tester         import Tester
from visual_tester   import VisualTester
from health_monitor import monitor as health_monitor
from agent_bus      import resolve_code

MAX_PARALLEL_CODERS = 3
POLL_INTERVAL       = 2
TASK_TIMEOUT        = 300   # allows 3 full Debugger attempts + fallback model

# Score below this even on PASS → trigger recode from scratch
RECODE_THRESHOLD = 8


class Orchestrator:

    def __init__(self):
        """Sets up semaphore, loads agents, starts health monitor."""
        self.max_parallel    = self._load_parallel_setting()
        self.coder_semaphore = threading.Semaphore(self.max_parallel)
        self.reviewer        = Reviewer()
        self.debugger        = Debugger()
        self.validator       = Validator()
        self.tester          = Tester()
        self.visual_tester   = VisualTester()
        self.active_tasks    = {}
        self.recode_counts   = {}   # task_id → number of recode attempts
        self.agent_name      = "orchestrator"
        health_monitor.start()

    def _load_parallel_setting(self):
        """Reads max_parallel_coders from settings.json."""
        settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        try:
            with open(settings_path) as f:
                return int(json.load(f).get("max_parallel_coders", MAX_PARALLEL_CODERS))
        except Exception:
            return MAX_PARALLEL_CODERS

    def run(self, project_file):
        """Main loop — loads project, recovers in_progress, runs pipeline."""
        project = self._load_project(project_file)
        if not project:
            agent_chat.log(self.agent_name, "Failed to load project", message_type="error")
            return None

        if "dependency_order" not in project:
            agent_chat.log(
                self.agent_name,
                "No dependency_order found — running Planner enrichment for JSON upload"
            )
            from planner import Planner
            planner = Planner()
            project = planner._enrich(project)
            self._save_project(project, project_file)

        if not agent_memory.get("app_blueprint"):
            project_desc = project.get("project", {}).get("description", "")
            if project_desc:
                agent_chat.log(
                    self.agent_name,
                    "JSON upload — generating app blueprint from project description"
                )
                try:
                    from architect import Architect
                    arch = Architect()
                    arch.design(project_desc)
                except Exception as e:
                    agent_chat.log(
                        self.agent_name,
                        f"Blueprint generation skipped: {e}",
                        message_type="error"
                    )

        project = self._recover_in_progress(project, project_file)
        agent_memory.set_project_context(project)

        # Clear stale bus messages from previous runs before starting
        agent_bus.clear_old(hours=1)

        total = sum(len(p.get("tasks", [])) for p in project.get("phases", []))
        agent_chat.log(self.agent_name, f"Project loaded — {total} tasks — pipeline starting")

        try:
            from api_handler import reset_rate_limits
            reset_rate_limits()
        except Exception:
            pass

        while True:
            if not self._check_control():
                break

            self._process_bus(project, project_file)
            project = self._check_timeouts(project, project_file)
            project = self._assign_ready_tasks(project, project_file)

            completed, total, failed = self._count(project)
            if completed + failed >= total and not self._has_pending(project):
                agent_chat.log(
                    self.agent_name,
                    f"ALL DONE — {completed}/{total} complete, {failed} failed",
                )
                break

            time.sleep(POLL_INTERVAL)

        return project

    def _recover_in_progress(self, project, project_file):
        """Resets in_progress tasks to pending on restart."""
        recovered = 0
        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                if task.get("status") == "in_progress":
                    task["status"] = "pending"
                    recovered += 1
        if recovered:
            agent_chat.log(self.agent_name, f"Recovery: reset {recovered} stuck tasks to pending")
            self._save_project(project, project_file)
        return project

    def _check_timeouts(self, project, project_file):
        """Marks timed-out tasks as failed."""
        now = time.time()
        timed_out = []
        for tid, info in list(self.active_tasks.items()):
            thread  = info.get("thread")
            elapsed = now - info.get("start_time", now)
            if thread and not thread.is_alive():
                timed_out.append(tid)
            elif elapsed > TASK_TIMEOUT:
                agent_chat.log(
                    self.agent_name,
                    f"Task {tid} timed out after {int(elapsed)}s",
                    message_type="error", task_id=tid,
                )
                project = self._mark_status(project, tid, "failed")
                self._save_project(project, project_file)
                timed_out.append(tid)
        for tid in timed_out:
            self.active_tasks.pop(tid, None)
        return project

    def _process_bus(self, project, project_file):
        """Reads bus messages and routes each to the right handler."""
        messages = agent_bus.read("orchestrator")
        for msg in messages:
            mtype   = msg["type"]
            content = msg.get("content", {})

            if mtype == "review_pass":
                project = self._handle_pass(project, project_file, content)
            elif mtype == "debug_failed":
                project = self._handle_debug_failed(project, project_file, content)
            elif mtype == "coder_failed":
                project = self._handle_coder_failed(project, project_file, content)
            elif mtype == "rerun_task":
                tid = content.get("task_id")
                if tid:
                    project = self._mark_status(project, tid, "pending")
                    self._save_project(project, project_file)
            elif mtype == "agent_crashed":
                agent_chat.log(
                    self.agent_name,
                    f"Health alert: {content.get('agent')} crashed",
                    message_type="error"
                )
            elif mtype == "plan_ready":
                updated = agent_memory.get_project_context()
                if updated and updated.get("dependency_order"):
                    project.update(updated)
                    self._save_project(project, project_file)
                    agent_chat.log(
                        self.agent_name,
                        "Received plan_ready from Planner — dependency graph loaded"
                    )

            agent_bus.acknowledge(msg["message_id"])

        self.reviewer.run()
        self.debugger.run()

    def _handle_pass(self, project, project_file, content):
        """
        Reviewer passed. Check score — if below RECODE_THRESHOLD and this task
        hasn't been recoded yet, send back to Coder for a full rewrite.
        Then run Validator and Tester before saving.
        """
        tid   = content.get("task_id")
        task  = content.get("task", {})
        code  = resolve_code(content)
        score = content.get("score", 10)

        # Recode gate: score passed but below quality threshold → full rewrite
        recode_count = self.recode_counts.get(tid, 0)
        if score < RECODE_THRESHOLD and recode_count < 1:
            self.recode_counts[tid] = recode_count + 1
            agent_chat.log(
                self.agent_name,
                f"Task {tid} passed Reviewer but score {score}/10 is below {RECODE_THRESHOLD} — "
                f"requesting full recode (attempt {recode_count + 1})",
                task_id=tid,
            )
            context_packet = agent_memory.get(f"context:{tid}") or {}
            context_packet["previous_low_score_code"] = code
            context_packet["previous_score"]          = score
            context_packet["recode_instruction"]      = (
                f"The previous attempt scored {score}/10 — below the required {RECODE_THRESHOLD}. "
                "Rewrite the file completely from scratch with higher quality. "
                "Focus on: complete implementation, proper error handling, clean structure, "
                "full feature coverage as described."
            )
            agent_memory.store(f"context:{tid}", context_packet, agent_name=self.agent_name)

            # Reset task to pending so Orchestrator reassigns it to Coder
            project = self._mark_status(project, tid, "pending")
            self._save_project(project, project_file)
            self.active_tasks.pop(tid, None)
            return project

        # Gate 1: Validator — syntax + structure + AI sanity
        valid, reason = self.validator.validate(task, code)
        if not valid:
            agent_chat.log(
                self.agent_name,
                f"Task {tid} VALIDATOR REJECTED — {reason} — sending to Debugger",
                message_type="error", task_id=tid,
            )
            agent_bus.post(
                sender       = self.agent_name,
                recipient    = "debugger",
                message_type = "review_fail",
                content      = {
                    "task_id": tid, "task": task, "code": code,
                    "issues":  [f"Validator: {reason}"], "attempt": 1,
                },
                task_id=tid,
            )
            return project

        # Gate 2: Tester — comprehensive checks
        test_passed, test_reason, _ = self.tester.test(task, code)
        if not test_passed:
            agent_chat.log(
                self.agent_name,
                f"Task {tid} TESTER REJECTED — {test_reason} — sending to Debugger",
                message_type="error", task_id=tid,
            )
            agent_bus.post(
                sender       = self.agent_name,
                recipient    = "debugger",
                message_type = "review_fail",
                content      = {
                    "task_id": tid, "task": task, "code": code,
                    "issues":  [f"Tester: {test_reason}"], "attempt": 1,
                },
                task_id=tid,
            )
            return project

        # Gate 3: Visual Tester — headless browser screenshot + vision model judge
        # Only runs for HTML/JS files. Fallback-safe: passes through if browser unavailable.
        filename = task.get("file", "")
        is_frontend = any(filename.endswith(ext) for ext in (".html", ".htm", ".js", ".jsx"))
        if is_frontend:
            visual_passed, visual_reason, _ = self.visual_tester.test_html(task, code)
            if not visual_passed:
                agent_chat.log(
                    self.agent_name,
                    f"Task {tid} VISUAL TESTER REJECTED — {visual_reason} — sending to Debugger",
                    message_type="error", task_id=tid,
                )
                agent_bus.post(
                    sender       = self.agent_name,
                    recipient    = "debugger",
                    message_type = "review_fail",
                    content      = {
                        "task_id": tid, "task": task, "code": code,
                        "issues":  [f"Visual: {visual_reason}"], "attempt": 1,
                    },
                    task_id=tid,
                )
                return project

        # All gates passed — save
        self._save_file(task, code)
        agent_memory.store_file_content(task.get("file", ""), code, task_id=tid)
        project = self._mark_status(project, tid, "complete")
        self._save_project(project, project_file)
        self.active_tasks.pop(tid, None)
        self.recode_counts.pop(tid, None)

        visual_note = " + visual" if is_frontend else ""
        agent_chat.log(
            self.agent_name,
            f"Task {tid} COMPLETE ✓ (score {score}/10, validated, tested{visual_note}) — {task.get('file','')}",
            task_id=tid,
        )
        agent_memory.save_decision(self.agent_name, tid, "complete", f"Score {score}, all gates passed")
        return project

    def _handle_debug_failed(self, project, project_file, content):
        """All debug attempts exhausted — mark failed."""
        tid = content.get("task_id")
        agent_chat.log(
            self.agent_name,
            f"Task {tid} FAILED — all attempts exhausted",
            message_type="error", task_id=tid
        )
        project = self._mark_status(project, tid, "failed")
        self._save_project(project, project_file)
        self.active_tasks.pop(tid, None)
        return project

    def _handle_coder_failed(self, project, project_file, content):
        """Coder returned nothing — mark failed."""
        tid = content.get("task_id")
        agent_chat.log(
            self.agent_name,
            f"Task {tid} FAILED — coder returned empty",
            message_type="error", task_id=tid
        )
        project = self._mark_status(project, tid, "failed")
        self._save_project(project, project_file)
        self.active_tasks.pop(tid, None)
        return project

    def _assign_ready_tasks(self, project, project_file):
        """Finds tasks with met dependencies and spawns Coders for them."""
        order    = project.get("dependency_order", [])
        complete = self._get_complete_task_ids(project)
        in_prog  = self._get_inprogress_task_ids(project)

        for tid in order:
            task = self._find_task(project, tid)
            if not task or task.get("status") != "pending":
                continue
            if not all(n in complete for n in task.get("needs", [])):
                continue
            if len(in_prog) >= self.max_parallel:
                break

            project = self._mark_status(project, tid, "in_progress")
            self._save_project(project, project_file)
            in_prog.append(tid)

            context_packet = agent_memory.get(f"context:{tid}") or {}
            agent_id       = len(in_prog)

            t = threading.Thread(
                target=self._run_coder,
                args=(task, context_packet, agent_id),
                daemon=True,
            )
            self.active_tasks[tid] = {"thread": t, "start_time": time.time()}
            t.start()
            health_monitor.watch_agent(f"coder-{agent_id}", t)
            agent_chat.log(self.agent_name, f"Assigned task {tid} → Coder-{agent_id}", task_id=tid)

        return project

    def _run_coder(self, task, context_packet, agent_id):
        """Runs a single Coder thread — respects parallel semaphore."""
        with self.coder_semaphore:
            coder = Coder(task, context_packet, agent_id=agent_id)
            coder.run()

    def _save_file(self, task, code):
        """Writes generated code to /output directory."""
        base_dir   = os.path.dirname(os.path.abspath(__file__))
        output_dir = os.path.join(base_dir, "output")
        file_rel   = task.get("file", "output.py")
        file_path  = os.path.join(output_dir, file_rel)
        os.makedirs(os.path.dirname(file_path) if os.path.dirname(file_path) else output_dir, exist_ok=True)
        with open(file_path, "w") as f:
            f.write(code)
        agent_chat.log(self.agent_name, f"Saved: output/{file_rel}", task_id=task.get("task_id"))

    def _mark_status(self, project, task_id, status):
        """Updates a task's status field in the project dict."""
        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                if task["task_id"] == task_id:
                    task["status"] = status
        return project

    def _find_task(self, project, task_id):
        """Returns the task dict for a given task_id, or None."""
        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                if task["task_id"] == task_id:
                    return task
        return None

    def _get_complete_task_ids(self, project):
        """Returns list of task_ids that are complete or verified."""
        return [
            t["task_id"] for p in project.get("phases", [])
            for t in p.get("tasks", [])
            if t.get("status") in ("complete", "verified")
        ]

    def _get_inprogress_task_ids(self, project):
        """Returns list of task_ids currently in_progress."""
        return [
            t["task_id"] for p in project.get("phases", [])
            for t in p.get("tasks", [])
            if t.get("status") == "in_progress"
        ]

    def _has_pending(self, project):
        """Returns True if any task is still pending or in_progress."""
        return any(
            t.get("status") in ("pending", "in_progress")
            for p in project.get("phases", [])
            for t in p.get("tasks", [])
        )

    def _count(self, project):
        """Returns (completed, total, failed) task counts."""
        completed = failed = total = 0
        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                total += 1
                s = task.get("status")
                if s in ("complete", "verified"): completed += 1
                elif s == "failed":               failed += 1
        return completed, total, failed

    def _load_project(self, project_file):
        """Loads project JSON from disk."""
        try:
            with open(project_file) as f:
                return json.load(f)
        except Exception as e:
            agent_chat.log(self.agent_name, f"Load failed: {e}", message_type="error")
            return None

    def _save_project(self, project, project_file):
        """Saves project JSON back to disk."""
        try:
            with open(project_file, "w") as f:
                json.dump(project, f, indent=2)
        except Exception as e:
            agent_chat.log(self.agent_name, f"Save failed: {e}", message_type="error")

    def _check_control(self):
        """Reads control.txt for pause/stop signals."""
        control_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs", "control.txt"
        )
        if not os.path.exists(control_path):
            return True
        with open(control_path) as f:
            state = f.read().strip()
        if state == "stop":
            agent_chat.log(self.agent_name, "STOP received", message_type="error")
            return False
        if state == "paused":
            agent_chat.log(self.agent_name, "PAUSED — waiting...")
            while True:
                time.sleep(3)
                with open(control_path) as f:
                    state = f.read().strip()
                if state == "running":
                    agent_chat.log(self.agent_name, "Resumed")
                    return True
                if state == "stop":
                    return False
        return True
