"""
debugger.py — KATALYST Debugger Agent
Fixes only what Reviewer flagged. Never rewrites from scratch.
Uses Cerebras llama-3.3-70b for fix attempts.
Max 3 attempts per task. After 3 failures, escalates to a FALLBACK MODEL
(Groq llama-3.3-70b-versatile) for one final "expert" attempt before giving up.

FIXES:
- After MAX_ATTEMPTS exhausted, tries once more with the Groq fallback model
  using a stronger "expert" prompt before escalating to Orchestrator.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_bus
import agent_memory
import agent_chat
from api_handler import ask_for_agent, ask_groq, GROQ_FALLBACK
from agent_bus import resolve_code

MAX_ATTEMPTS = 3


class Debugger:

    def __init__(self):
        """Sets up debugger."""
        self.agent_name = "debugger"

    def run(self):
        """Reads all pending review_fail messages and fixes each."""
        messages = agent_bus.read("debugger")
        for msg in messages:
            if msg["type"] == "review_fail":
                self._handle_failure(msg)
                agent_bus.acknowledge(msg["message_id"])

    def fix(self, task, code, issues, attempt_number):
        """Fixes code based on reviewer complaints. Returns fixed code or None."""
        tid = task["task_id"]
        agent_chat.log(
            self.agent_name,
            f"Debug attempt {attempt_number}/{MAX_ATTEMPTS} for task {tid} — fixing: {', '.join(issues[:2])}",
            task_id=tid,
        )

        prompt = self._build_fix_prompt(task, code, issues, attempt_number)
        fixed  = ask_for_agent(prompt, "debugger")

        if not fixed or not fixed.strip():
            agent_chat.log(self.agent_name, f"Empty fix response for {tid}", message_type="error", task_id=tid)
            return None

        agent_memory.store_lesson(
            error      = "; ".join(issues[:3]),
            fix        = f"Fixed on attempt {attempt_number}",
            task_type  = task.get("description", "")[:60],
            agent_name = self.agent_name,
        )
        return fixed

    def _fallback_expert_fix(self, task, code, issues, error_history):
        """
        Final attempt using Groq fallback model with a stronger expert prompt.
        Called only after MAX_ATTEMPTS are exhausted and all failed.
        Returns fixed code or None.
        """
        tid = task["task_id"]
        agent_chat.log(
            self.agent_name,
            f"Task {tid} — all {MAX_ATTEMPTS} attempts failed — trying FALLBACK EXPERT MODEL (Groq)",
            message_type="error",
            task_id=tid,
        )

        issues_text = "\n".join(f"- {i}" for i in issues)
        history_text = "\n".join(
            f"Attempt {i+1} issues: {err}" for i, err in enumerate(error_history)
        )

        dep_context = ""
        for filename in task.get("reads", []):
            content = agent_memory.get_file_content(filename)
            if content:
                dep_context += f"\n=== DEPENDENCY: {filename} ===\n{content[:600]}\n"

        expert_prompt = f"""You are a senior engineer fixing code that has failed review {MAX_ATTEMPTS} times.
Previous attempts all failed. You must succeed where others did not.

TASK: {task.get('description', '')}
EXPECTED OUTPUT: {task.get('expected_output', '')}
FILE: {task.get('file', '')}
{dep_context}
FAILURE HISTORY:
{history_text}

CURRENT ISSUES THAT MUST BE FIXED:
{issues_text}

LAST CODE SUBMITTED (that failed):
{code}

EXPERT INSTRUCTIONS:
1. Study every failure carefully — understand WHY each attempt failed
2. Do NOT repeat any approach that already failed
3. Write a completely fresh, clean implementation from scratch
4. Every function must be fully implemented — zero placeholders
5. Every I/O, file, and network operation wrapped in try/except
6. If dependency files shown above — use their EXACT signatures
7. Test your logic mentally before writing

Return COMPLETE corrected code ONLY. No markdown. No explanation. Start with imports.
"""

        fixed = ask_groq(expert_prompt, model=GROQ_FALLBACK)
        if fixed and fixed.strip():
            agent_chat.log(
                self.agent_name,
                f"Task {tid} — Fallback expert model returned a fix — sending to Reviewer",
                task_id=tid,
            )
            agent_memory.store_lesson(
                error      = "; ".join(issues[:3]),
                fix        = "Fixed by fallback expert model after all standard attempts failed",
                task_type  = task.get("description", "")[:60],
                agent_name = self.agent_name,
            )
        return fixed if (fixed and fixed.strip()) else None

    def _handle_failure(self, msg):
        """Processes review_fail — fixes and re-routes to Reviewer or escalates."""
        content        = msg.get("content", {})
        task           = content.get("task", {})
        code           = resolve_code(content)
        issues         = content.get("issues", [])
        attempt_number = content.get("attempt", 1)
        error_history  = content.get("error_history", [])
        tid            = task.get("task_id", "?")

        if attempt_number > MAX_ATTEMPTS:
            # All standard attempts done — try fallback expert model
            fixed = self._fallback_expert_fix(task, code, issues, error_history)

            if fixed:
                # Send fallback result to Reviewer for one final check
                agent_bus.post(
                    sender       = self.agent_name,
                    recipient    = "reviewer",
                    message_type = "debug_ready",
                    content      = {
                        "task_id":       tid,
                        "task":          task,
                        "code":          fixed,
                        "attempt":       attempt_number + 1,
                        "error_history": error_history,
                        "is_fallback":   True,
                    },
                    task_id=tid,
                )
            else:
                # Fallback also failed — truly give up
                agent_chat.log(
                    self.agent_name,
                    f"Task {tid} — fallback model also failed — escalating to Orchestrator",
                    message_type="error", task_id=tid,
                )
                agent_bus.post(
                    sender       = self.agent_name,
                    recipient    = "orchestrator",
                    message_type = "debug_failed",
                    content      = {"task_id": tid, "task": task, "code": code, "issues": issues},
                    task_id      = tid,
                )
            return

        fixed = self.fix(task, code, issues, attempt_number)

        if not fixed:
            # This attempt returned nothing — escalate immediately
            agent_bus.post(
                sender       = self.agent_name,
                recipient    = "orchestrator",
                message_type = "debug_failed",
                content      = {"task_id": tid, "task": task, "code": code, "issues": issues},
                task_id      = tid,
            )
            return

        agent_chat.log(self.agent_name, f"Fix ready for {tid} — sending back to Reviewer", task_id=tid)

        # Carry forward error history so fallback model sees all past failures
        new_history = error_history + ["; ".join(issues[:2])]

        agent_bus.post(
            sender       = self.agent_name,
            recipient    = "reviewer",
            message_type = "debug_ready",
            content      = {
                "task_id":       tid,
                "task":          task,
                "code":          fixed,
                "attempt":       attempt_number + 1,
                "error_history": new_history,
            },
            task_id=tid,
        )

    def _build_fix_prompt(self, task, code, issues, attempt_number):
        """Targeted fix prompt — only fix what reviewer said, keep everything else."""
        issues_text = "\n".join(f"- {issue}" for issue in issues)

        dep_context = ""
        for filename in task.get("reads", []):
            content = agent_memory.get_file_content(filename)
            if content:
                dep_context += f"\n=== DEPENDENCY: {filename} ===\n{content[:600]}\n"

        return f"""You are a senior developer fixing specific code issues.

TASK: {task.get('description', '')}
EXPECTED OUTPUT: {task.get('expected_output', '')}
FILE: {task.get('file', '')}
ATTEMPT: {attempt_number} of {MAX_ATTEMPTS}
{dep_context}
THE REVIEWER FOUND THESE EXACT PROBLEMS:
{issues_text}

CODE THAT FAILED:
{code}

INSTRUCTIONS:
1. Fix ONLY the issues listed above — nothing else
2. Keep all working parts exactly as they are
3. If a dependency file is shown above, make sure your fix integrates correctly with it
4. No placeholders, no TODO, no pass where logic is needed
5. Return the COMPLETE fixed file — not just the changed parts

Return complete corrected code only. No markdown. No explanation.
"""
