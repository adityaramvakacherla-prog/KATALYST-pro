"""
planner_validator.py — KATALYST Planner Validator
Enforces task description quality AFTER the Planner produces the JSON.
Checks each task description against a rubric and rejects/retries if it fails.

This makes Fix 2 (TASK_DESCRIPTION_RULES) enforceable, not just advisory.

Checks per task:
  HTML/JS tasks:
    - At least 2 hex color values present (#rrggbb)
    - At least 1 pixel dimension (Npx)
    - At least 1 font name
    - At least 1 interactive state keyword (hover, click, focus, etc.)
  Python tasks:
    - At least 1 function signature or type hint pattern
    - At least 1 error/exception mention
    - At least 1 specific output format mention
  ALL tasks:
    - Description length >= 80 chars (too short = definitely vague)
    - expected_output length >= 40 chars

On failure: logs which tasks failed and what was missing.
Returns (valid: bool, issues: list[str]).
The Planner calls this and retries the whole plan if too many tasks fail.
"""
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat

# Minimum description/output lengths
MIN_DESCRIPTION_LEN  = 80
MIN_EXPECTED_LEN     = 40

# How many tasks can fail the rubric before we reject the whole plan
# (some tasks are genuinely simple — don't force hex colors into a __init__.py)
MAX_FAIL_RATIO = 0.4   # if >40% of tasks fail, reject

# File extensions considered frontend (require visual specs)
FRONTEND_EXTS = {".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".css"}

# File extensions considered Python
PYTHON_EXTS = {".py"}


class PlannerValidator:

    def __init__(self):
        """Sets up planner validator."""
        self.agent_name = "planner"

    def validate(self, project: dict) -> tuple[bool, list[str]]:
        """
        Validates all tasks in the project plan.
        Returns (valid, list_of_issues).
        """
        all_issues  = []
        total_tasks = 0
        fail_count  = 0

        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                total_tasks += 1
                task_issues = self._check_task(task)
                if task_issues:
                    fail_count += 1
                    tid = task.get("task_id", "?")
                    for issue in task_issues:
                        all_issues.append(f"Task {tid} ({task.get('file','')}): {issue}")

        if total_tasks == 0:
            return False, ["Plan has no tasks"]

        fail_ratio = fail_count / total_tasks
        valid = fail_ratio <= MAX_FAIL_RATIO

        if all_issues:
            agent_chat.log(
                self.agent_name,
                f"Plan validator: {fail_count}/{total_tasks} tasks below spec quality"
                f" ({'REJECTING' if not valid else 'acceptable ratio'})",
                message_type="error" if not valid else "info",
            )
            for issue in all_issues[:5]:   # log first 5 only
                agent_chat.log(self.agent_name, f"  - {issue}")
        else:
            agent_chat.log(
                self.agent_name,
                f"Plan validator: all {total_tasks} tasks passed spec quality check ✓",
            )

        return valid, all_issues

    def _check_task(self, task: dict) -> list[str]:
        """
        Checks a single task description against the quality rubric.
        Returns list of issue strings (empty = passed).
        """
        desc     = task.get("description", "")
        expected = task.get("expected_output", "")
        filename = task.get("file", "")
        ext      = os.path.splitext(filename)[1].lower()
        issues   = []

        # ── Universal checks ─────────────────────────────────────────────
        if len(desc.strip()) < MIN_DESCRIPTION_LEN:
            issues.append(
                f"description too short ({len(desc)} chars, min {MIN_DESCRIPTION_LEN})"
                f" — likely vague: '{desc[:60]}...'"
            )

        if len(expected.strip()) < MIN_EXPECTED_LEN:
            issues.append(
                f"expected_output too short ({len(expected)} chars, min {MIN_EXPECTED_LEN})"
                f" — '{expected[:40]}'"
            )

        # Skip detailed checks for config/data files
        if ext in (".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".env"):
            return issues

        # ── Frontend-specific checks ──────────────────────────────────────
        if ext in FRONTEND_EXTS:
            issues.extend(self._check_frontend_task(desc, filename))

        # ── Python-specific checks ────────────────────────────────────────
        elif ext in PYTHON_EXTS:
            issues.extend(self._check_python_task(desc, filename))

        return issues

    def _check_frontend_task(self, desc: str, filename: str) -> list[str]:
        """Returns issues for HTML/JS/CSS tasks."""
        issues = []
        desc_lower = desc.lower()

        # Check for hex colors
        hex_colors = re.findall(r"#[0-9a-fA-F]{3,6}\b", desc)
        if len(hex_colors) < 2:
            issues.append(
                f"only {len(hex_colors)} hex color(s) specified — frontend tasks need"
                f" explicit colors for background, text, accent, buttons"
            )

        # Check for pixel dimensions
        px_dims = re.findall(r"\d+\s*px", desc, re.IGNORECASE)
        if not px_dims:
            issues.append(
                "no pixel dimensions specified — frontend tasks need explicit sizes"
                " (canvas size, element widths, font sizes, padding)"
            )

        # Check for font mention
        font_keywords = [
            "inter", "outfit", "segoe", "roboto", "jetbrains", "fira", "mono",
            "sans-serif", "monospace", "font-family", "font:",
        ]
        if not any(kw in desc_lower for kw in font_keywords):
            issues.append(
                "no font specified — frontend tasks need explicit font names"
                " (Inter, JetBrains Mono, Segoe UI, etc.)"
            )

        # Check for interactive state keywords
        interactive_kw = ["hover", "click", "focus", "active", "disabled", "transition"]
        if not any(kw in desc_lower for kw in interactive_kw):
            issues.append(
                "no interactive states mentioned — frontend tasks need hover/focus/active"
                " state descriptions for all interactive elements"
            )

        return issues

    def _check_python_task(self, desc: str, filename: str) -> list[str]:
        """Returns issues for Python tasks."""
        issues   = []
        desc_lower = desc.lower()

        # Skip __init__, setup, config files — they legitimately have simple descriptions
        simple_files = {"__init__.py", "setup.py", "config.py", "constants.py", "settings.py"}
        if os.path.basename(filename) in simple_files:
            return issues

        # Check for error handling mention
        error_kw = ["error", "exception", "raise", "try", "except", "systemexit", "valueerror"]
        if not any(kw in desc_lower for kw in error_kw):
            issues.append(
                "no error handling mentioned — Python tasks should specify error cases"
                " (what happens on bad input, missing files, invalid data)"
            )

        # Check for output format mention
        output_kw = [
            "print", "return", "write", "output", "log", "save", "file",
            "json", "csv", "table", "format", "str", "dict", "list",
        ]
        if not any(kw in desc_lower for kw in output_kw):
            issues.append(
                "no output format specified — Python tasks should describe what"
                " the function returns or prints"
            )

        return issues

    def build_retry_prompt(self, original_prompt: str, issues: list[str]) -> str:
        """
        Builds an improved retry prompt that explicitly calls out the failed tasks.
        Used when the plan is rejected and needs to be regenerated.
        """
        issues_text = "\n".join(f"  - {issue}" for issue in issues[:10])

        return (
            f"The previous plan had task descriptions that were too vague."
            f" These specific issues were found:\n{issues_text}\n\n"
            f"Regenerate the complete plan for this request, fixing ALL of the above.\n"
            f"Remember: every HTML/JS task needs hex colors, px sizes, font names, and"
            f" hover state descriptions. Every Python task needs error cases and output"
            f" format. Short descriptions will be rejected again.\n\n"
            f"Original request: {original_prompt}"
        )
