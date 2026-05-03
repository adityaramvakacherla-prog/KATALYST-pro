"""
coder.py — KATALYST Coder Agent
Reads a task context packet and writes complete code.
Uses Cerebras llama-3.3-70b (corrected from invalid qwen-3-235b string).
Blueprint-aware: knows the full app design before writing any single file.
One shot — posts to Reviewer immediately.

FIX 5: Model name in logs updated to reflect actual Cerebras model used.
FIX 6: Blueprint now uses get_blueprint_for_task() which extracts up to
5000 chars with intelligent section prioritisation, instead of a hard
[:2000] slice that cut off the file map and data flow sections.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_bus
import agent_memory
import agent_chat
from api_handler import ask_for_agent, get_blueprint_for_task
from agent_bus import resolve_code  # noqa: F401 — imported so callers can use it


class Coder:

    def __init__(self, task, context_packet, agent_id=1):
        """Sets up Coder with task and its full context."""
        self.task           = task
        self.context_packet = context_packet
        self.agent_id       = agent_id
        self.agent_name     = f"coder-{agent_id}"

    def run(self):
        """Builds prompt, calls AI, posts result to bus for Reviewer."""
        tid = self.task["task_id"]
        agent_chat.log(self.agent_name, f"Starting task {tid}: {self.task.get('file','')}", task_id=tid)

        past_lessons = agent_memory.get_lessons(self.task.get("description", ""))
        prompt = self._build_prompt(past_lessons)

        agent_chat.log(self.agent_name, f"Calling Cerebras llama-3.3-70b for task {tid}...", task_id=tid)
        code = ask_for_agent(prompt, "coder")

        if not code or not code.strip():
            agent_chat.log(
                self.agent_name,
                f"Empty response for task {tid}",
                message_type="error",
                task_id=tid,
            )
            agent_bus.post(
                self.agent_name, "orchestrator", "coder_failed",
                {"task_id": tid, "reason": "AI returned empty response"},
                task_id=tid,
            )
            return

        lines = len(code.strip().splitlines())
        agent_chat.log(self.agent_name, f"Code ready — {lines} lines — sending to Reviewer", task_id=tid)

        agent_bus.post(
            sender       = self.agent_name,
            recipient    = "reviewer",
            message_type = "code_ready",
            content      = {"task_id": tid, "task": self.task, "code": code, "attempt": 1},
            task_id      = tid,
        )

    def _build_prompt(self, past_lessons):
        """Assembles full prompt — includes full blueprint excerpt, deps, lessons, rules."""
        ctx = self.context_packet

        # Dependency file contents
        dep_context = ""
        for filename, content in ctx.get("dependency_files", {}).items():
            dep_context += f"\n\n=== EXISTING FILE: {filename} ===\n{content}\n"

        # Past lessons from permanent lesson store
        lessons_text = ""
        if past_lessons:
            lessons_text = "\nLESSONS FROM PAST MISTAKES — avoid these:\n"
            lessons_text += "\n".join(f"- {l}" for l in past_lessons)

        # Extend note
        extends_note = ""
        if ctx.get("extends"):
            extends_note = (
                f"\nIMPORTANT: This task EXTENDS {ctx['extends']}. "
                f"Add to the existing file — do not rewrite from scratch.\n"
            )

        # Coder rules
        rules_text = ""
        if ctx.get("coder_rules"):
            rules_text = f"\nCODER RULES — follow strictly:\n{ctx['coder_rules']}\n"

        # FIX 6: Use intelligent blueprint extraction instead of raw [:2000] slice.
        # get_blueprint_for_task prioritises the File Architecture section and
        # returns up to 5000 chars so data flow and file map are not cut off.
        blueprint_text = ""
        if ctx.get("app_blueprint"):
            excerpt = get_blueprint_for_task(
                ctx["app_blueprint"],
                ctx.get("description", ""),
                max_chars=5000,
            )
            blueprint_text = f"\nAPP BLUEPRINT CONTEXT (your file is part of this app):\n{excerpt}\n"

        prompt = f"""You are a principal software engineer at a top-tier tech company writing production code for a real shipped product.

PROJECT: {ctx.get('project_name', '')}
DESCRIPTION: {ctx.get('project_desc', '')}
PHASE: {ctx.get('phase_name', '')}
{blueprint_text}
{rules_text}
YOUR TASK:
File to create: {ctx.get('file_to_create', '')}
What to build: {ctx.get('description', '')}
Expected result: {ctx.get('expected_output', '')}
{extends_note}
{dep_context}
{lessons_text}

QUALITY STANDARD — this code will be shown to clients and senior engineers. It must be polished, complete, and professional:

CORRECTNESS (will be rejected if violated):
1. Implement EVERY feature in the task description — read every word, nothing skipped
2. All imports correct — only stdlib or project-local modules unless dependencies listed
3. ZERO placeholders — no `pass`, no `# TODO`, no `raise NotImplementedError`, no `return None` where logic needed
4. Error handling with try/except on ALL I/O, network, file, and parse operations — never swallow silently
5. If dependency files shown: use their EXACT class names, function signatures, and import paths
6. Code runs on first execution with no user modification required

CODE QUALITY (what separates good from great):
7. Functions under 25 lines, each doing exactly one thing — split ruthlessly if needed
8. Descriptive names — never `x`, `data`, `temp`, `val` — name things for what they ARE
9. Every function has a clear docstring/comment
10. Constants at the top of the file in UPPER_SNAKE_CASE
11. Imports grouped: stdlib → third-party → local, blank line between groups

UI POLISH (for any HTML/JS/CSS — make it look stunning):
12. Dark theme: background #0e1117, surface #151b26, text #e2e8f0, accent #7c6af7, success #3dd68c
13. Font: Inter or Segoe UI for UI text, JetBrains Mono for code/numbers
14. Every button has hover state (colour shift + cursor:pointer) and smooth transition (0.15s ease)
15. Consistent spacing — 8px base unit, multiples of 8
16. Cards with border-radius:12px, subtle border (#252f45), no harsh edges
17. Visible loading/error/empty states — never leave the user guessing
18. For games: requestAnimationFrame loop, prominent score display, styled game-over screen with restart

Before writing, think through the full implementation. Picture the finished product.
Write the complete file top to bottom — no shortcuts, no half measures.
PRE-SUBMIT SELF-CHECK — run through this BEFORE returning your code.
For each item, if the answer is NO, fix it first. Do not return code that fails any item.

REQUIRED CHECKS:
[ ] 1. Did I implement EVERY feature in the task description? (Read it again word by word.)
[ ] 2. Does every function have a real body — no pass, no TODO, no return None as a placeholder?
[ ] 3. Did I add try/except around every file read/write, network call, and JSON parse?
[ ] 4. For HTML/JS: does EVERY <button> have an explicit :hover CSS rule with a color change?
[ ] 5. For HTML/JS: is the background #0e1117 (not white, not browser default)?
[ ] 6. For HTML/JS: is there a custom font stack (Inter, Segoe UI, or JetBrains Mono)?
[ ] 7. For games: does the game loop use requestAnimationFrame (not setInterval)?
[ ] 8. For games: is the score visible and styled (not a tiny unstyled number)?
[ ] 9. For games: is there a styled game-over screen with a restart button?
[ ] 10. For games: do keyboard controls work immediately on page load without clicking?
[ ] 11. For Python: do all functions have type hints and a docstring?
[ ] 12. For Python: is there an if __name__ == "__main__": guard on executable scripts?
[ ] 13. Would a senior engineer be proud to ship this? Or does it look unfinished?

If any answer is NO — fix it before returning.

Return code ONLY. No explanations. No markdown fences. Start with imports or <!DOCTYPE html>.
"""
        return prompt
