"""
reviewer.py — KATALYST Reviewer Agent
Judges code PASS or FAIL. Never modifies.
Uses Mistral Codestral — purpose-built code review model.
Separate Mistral token pool means Cerebras/Groq budget is freed for coding.
PASS threshold: 8/10. Checklist-based review prompt.

FIX: Reviewer prompt is now adversarial — it is told to ASSUME at least
one bug exists and find it, rather than being asked to confirm the code
looks correct. This prevents a smart coder model from writing code that
passes the checklist structurally while still being logically broken.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_bus
import agent_memory
import agent_chat
from api_handler import ask_for_agent
from agent_bus import resolve_code


class Reviewer:

    def __init__(self):
        """Sets up reviewer."""
        self.agent_name = "reviewer"

    def run(self):
        """Reads all pending code_ready and debug_ready messages and reviews each."""
        messages = agent_bus.read("reviewer")
        for msg in messages:
            if msg["type"] in ("code_ready", "debug_ready"):
                self._review_message(msg)
                agent_bus.acknowledge(msg["message_id"])

    def review(self, task, code, attempt=1):
        """Reviews a single piece of code. Returns verdict dict."""
        tid = task["task_id"]
        agent_chat.log(self.agent_name, f"Reviewing task {tid} (attempt {attempt})", task_id=tid)

        prompt   = self._build_review_prompt(task, code)
        response = ask_for_agent(prompt, "reviewer")

        if not response:
            agent_chat.log(self.agent_name, "No reviewer response — defaulting FAIL", task_id=tid)
            return {"score": 0, "verdict": "FAIL", "issues": ["Reviewer got no AI response"], "reason": ""}

        verdict = self._parse_verdict(response)
        agent_chat.log(
            self.agent_name,
            f"Task {tid} — {verdict['verdict']} ({verdict['score']}/10) — {verdict.get('reason','')}",
            task_id=tid,
        )
        return verdict

    def _review_message(self, msg):
        """Handles a single bus message — reviews code and posts verdict."""
        content = msg.get("content", {})
        task    = content.get("task", {})
        code    = resolve_code(content)
        attempt = content.get("attempt", 1)
        tid     = task.get("task_id", "?")

        verdict = self.review(task, code, attempt=attempt)

        if verdict["verdict"] == "PASS":
            agent_bus.post(
                sender       = self.agent_name,
                recipient    = "orchestrator",
                message_type = "review_pass",
                content      = {"task_id": tid, "task": task, "code": code, "score": verdict["score"]},
                task_id      = tid,
            )
        else:
            agent_bus.post(
                sender       = self.agent_name,
                recipient    = "debugger",
                message_type = "review_fail",
                content      = {
                    "task_id": tid,
                    "task":    task,
                    "code":    code,
                    "issues":  verdict.get("issues", []),
                    "attempt": attempt,
                },
                task_id=tid,
            )

    def _get_dependency_files(self, task):
        """Fetches content of files this task depends on — for integration checking."""
        dep_context = ""
        for filename in task.get("reads", []):
            content = agent_memory.get_file_content(filename)
            if content:
                dep_context += f"\n=== DEPENDENCY: {filename} ===\n{content[:800]}\n"
        return dep_context

    def _build_review_prompt(self, task, code):
        """
        Builds an adversarial checklist-based review prompt.
        Includes a dedicated Visual QA section for HTML/JS/CSS files.
        """
        coder_rules = ""
        rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CODER_RULES.md")
        if os.path.exists(rules_path):
            with open(rules_path) as f:
                coder_rules = f.read()

        dep_context = self._get_dependency_files(task)
        filename = task.get("file", "")
        is_frontend = any(filename.endswith(ext) for ext in (".html", ".htm", ".js", ".jsx", ".css"))

        visual_qa_section = ""
        if is_frontend:
            visual_qa_section = """
VISUAL QA — HTML/JS/CSS SPECIFIC (these items are weighted equally to functional items):
[ ] 9.  Imagine opening this file in Chrome right now. Describe in one sentence what the user
        actually sees. Does it look like a professional app or a student project?
[ ] 10. Is there a clear visual hierarchy? Title/heading visible? Content structured logically?
[ ] 11. Dark theme correctly applied? Check: body background, card/panel backgrounds, text colors,
        button colors. They must match the spec — not browser defaults.
[ ] 12. Buttons: do ALL have explicit hover CSS (background shift)? cursor:pointer? transition?
        Minimum 10px 20px padding? If any button is missing hover styles — FAIL.
[ ] 13. Typography: is a custom font stack specified? Line-height set? Letter-spacing on labels?
        Or is it rendering in browser-default Times New Roman?
[ ] 14. Spacing: is there consistent padding/margin? Or do elements touch the edges?
[ ] 15. For GAMES: is the score visible and prominent (not tiny in a corner)?
        Is there a styled game-over screen with a restart button?
        Does the game loop use requestAnimationFrame (not setInterval)?
        Do controls work without clicking the canvas first?
[ ] 16. Empty/loading/error states: if the app has async ops or data — are these states styled?
        Or does it show a blank white box?

REJECT if the visual description uses words like: "basic", "simple", "minimal", "plain",
"default", "standard" — these indicate the dark theme and polish were not applied.
"""

        return f"""You are a senior adversarial code reviewer. Your job is to FIND bugs, not confirm correctness.

CRITICAL MINDSET: Assume this code has at least one bug. Find it.
A reviewer who passes broken or ugly code is worse than useless. Be harsh.

TASK: {task.get("description", "")}
EXPECTED OUTPUT: {task.get("expected_output", "")}
FILE: {filename}

CODER RULES:
{coder_rules}
{dep_context}
CODE TO REVIEW:
{code}

Work through EVERY checklist item. Actively try to BREAK each one mentally:

FUNCTIONAL CHECKLIST:
[ ] 1. Does it implement EXACTLY what the task description says? Read every word.
[ ] 2. Will every import succeed? Package names correct? Any typos?
[ ] 3. Is every function body fully implemented? Search for pass, TODO, return None, raise NotImplementedError.
[ ] 4. Real error handling present? No bare except, no silent failures, no swallowed exceptions?
[ ] 5. If dependency files shown — correct signatures used? Exact import paths?
[ ] 6. Can you find a realistic input that crashes it on first run?
[ ] 7. Any hardcoded data that should come from parameters or config?
[ ] 8. For HTML/JS: ALL interactive elements have working event handlers?
{visual_qa_section}
Respond in this EXACT format (no extra text):
SCORE: [1-10]
VERDICT: [PASS or FAIL]
REASON: [one sentence — the single most important finding]
ISSUES:
- [specific issue with line reference if possible]
- [another issue, or write "none" if genuinely none]

VERDICT is PASS only if SCORE >= 8 AND every checklist item passes.
One failed item = FAIL regardless of score.
For HTML/JS files: failing any visual item (#9-16) counts the same as failing a functional item.
If you cannot find any bugs after genuinely trying, say so in REASON and give PASS.
"""


    def _parse_verdict(self, response):
        """Parses score, verdict, reason, issues from reviewer response."""
        result = {"score": 5, "verdict": "FAIL", "reason": "", "issues": []}
        lines  = response.strip().splitlines()
        issues_started = False
        issues = []

        for line in lines:
            line = line.strip()
            if line.startswith("SCORE:"):
                try:
                    result["score"] = int(line.split(":", 1)[1].strip().split()[0])
                except Exception:
                    pass
            elif line.startswith("VERDICT:"):
                v = line.split(":", 1)[1].strip().upper()
                result["verdict"] = "PASS" if "PASS" in v else "FAIL"
            elif line.startswith("REASON:"):
                result["reason"] = line.split(":", 1)[1].strip()
            elif line.startswith("ISSUES:"):
                issues_started = True
            elif issues_started and line.startswith("-"):
                issue = line.lstrip("- ").strip()
                if issue and issue.lower() not in ("none", ""):
                    issues.append(issue)

        result["issues"] = issues

        if result["score"] < 8:
            result["verdict"] = "FAIL"
            if not result["issues"]:
                result["issues"] = [f"Score {result['score']}/10 is below minimum threshold of 8"]

        return result
