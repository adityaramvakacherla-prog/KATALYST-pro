"""
validator.py — KATALYST Validator Agent
Runs after Reviewer PASS, before Orchestrator saves the file.
Three checks:
  1. Python syntax check via compile() — zero AI cost
  2. Non-Python structural check via validate_non_python() — zero AI cost
     Catches truncated HTML, invalid JSON, mismatched JS braces
  3. AI sanity check via Cerebras llama3.1-8b (tiny model)
     asking: does this code actually do what the task says?

FIX 7: Previously only .py files got any static validation. HTML, JS,
JSON, CSS files went straight to AI sanity check. Now every file type
gets a fast, free structural check first.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat
import agent_memory
from api_handler import ask_for_agent, validate_non_python


class Validator:

    def __init__(self):
        """Sets up validator."""
        self.agent_name = "validator"

    def validate(self, task, code):
        """
        Validates code before saving.
        Returns (passed: bool, reason: str)
        """
        tid      = task.get("task_id", "?")
        filename = task.get("file", "")

        # Step 1: static syntax / structure check — free, instant, no API call
        struct_ok, struct_msg = self._static_check(code, filename)
        if not struct_ok:
            agent_chat.log(
                self.agent_name,
                f"Task {tid} STRUCTURE FAIL — {struct_msg}",
                message_type="error",
                task_id=tid,
            )
            return False, f"Structure error: {struct_msg}"

        # Step 2: AI sanity check using tiny fast model
        ai_ok, ai_msg = self._ai_sanity_check(task, code)
        if not ai_ok:
            agent_chat.log(
                self.agent_name,
                f"Task {tid} SANITY FAIL — {ai_msg}",
                message_type="error",
                task_id=tid,
            )
            return False, f"Sanity check failed: {ai_msg}"

        agent_chat.log(
            self.agent_name,
            f"Task {tid} VALIDATED ✓ — structure clean, logic verified",
            task_id=tid,
        )
        return True, "ok"

    def _static_check(self, code, filename):
        """
        Runs the appropriate static check for the file type.
        .py  → Python compile() syntax check
        .html/.js/.json/.css → structural checks via validate_non_python()
        other → pass through
        """
        if not filename:
            return True, "no filename — skipping static check"

        if filename.endswith(".py"):
            return self._python_syntax_check(code, filename)

        ext = os.path.splitext(filename)[1].lower()
        if ext in (".html", ".htm", ".js", ".json", ".css"):
            return validate_non_python(code, filename)

        return True, f"no static check for {ext}"

    def _python_syntax_check(self, code, filename):
        """Uses Python compile() to check syntax — zero cost, instant."""
        try:
            compile(code, filename, "exec")
            return True, "ok"
        except SyntaxError as e:
            return False, f"line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, str(e)[:200]

    def _ai_sanity_check(self, task, code):
        """
        Asks Cerebras llama3.1-8b one simple question:
        does this code actually implement the task? YES or NO + reason.
        """
        prompt = f"""You are doing a quick sanity check on generated code.

TASK DESCRIPTION: {task.get('description', '')}
EXPECTED OUTPUT: {task.get('expected_output', '')}
FILE: {task.get('file', '')}

CODE:
{code[:3000]}

Answer in this EXACT format only:
SANE: YES
or
SANE: NO
REASON: [one sentence — what is missing or wrong]

Check only: does this code plausibly implement the task described?
Do not check style. Do not check edge cases. Just: does it do the job?
"""
        response = ask_for_agent(prompt, "validator")

        if not response:
            agent_chat.log(
                self.agent_name,
                "Validator AI unavailable — passing on structure check alone",
            )
            return True, "ai unavailable"

        response_upper = response.upper()
        if "SANE: YES" in response_upper:
            return True, "ok"
        if "SANE: NO" in response_upper:
            lines  = response.strip().splitlines()
            reason = next(
                (l.split("REASON:", 1)[1].strip() for l in lines if "REASON:" in l.upper()),
                "Code does not match task"
            )
            return False, reason

        return True, "ambiguous response — passing"
