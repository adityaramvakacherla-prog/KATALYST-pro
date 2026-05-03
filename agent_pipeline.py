"""
agent_pipeline.py — KATALYST Full Agent Pipeline
Used when user hits "Generate Code" in Labs or Input page.
Now runs ALL 8 agents synchronously:
  Architect → Planner → Coder → Reviewer → Debugger → Validator → Tester

Every quick-generate goes through the complete QA chain just like a
full project build. Returns the final reviewed+fixed code to the caller.
"""
import os
import sys
import ast
import re
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat
import agent_memory
from api_handler import ask_for_agent


MAX_DEBUG_ATTEMPTS      = 3
REVIEWER_PASS_THRESHOLD = 8
TESTER_TIMEOUT          = 10   # seconds for Python execution


# ── LOGGING HELPER ────────────────────────────────────────────────────────────

def _log(msg, agent="pipeline"):
    agent_chat.log(agent, msg)


# ── ARCHITECT ─────────────────────────────────────────────────────────────────

def _run_architect(prompt, language):
    """
    Architect designs a mini blueprint for the requested app.
    Gives Coder a clear picture of what to build before writing a line.
    """
    _log(f"Designing blueprint for: {prompt[:70]}...", "architect")

    architect_prompt = f"""You are a senior software architect. A developer is about to write code for this request:

REQUEST: {prompt}
LANGUAGE: {language}

Produce a concise technical blueprint covering:
1. WHAT TO BUILD — exact features, UI elements, interactions
2. FILE STRUCTURE — since this is a single file, list all functions/classes needed
3. DATA FLOW — how data moves through the app
4. UI THEME — dark theme, specific colours (#0e1117 background, #e2e8f0 text, #7c6af7 accent)
5. TECHNICAL NOTES — any tricky parts, libraries to use, things to avoid

Be specific. Name actual functions. Describe actual UI elements.
This blueprint goes directly to the Coder — vagueness causes bugs.
Keep it under 400 words.
"""
    blueprint = ask_for_agent(architect_prompt, "architect")
    if blueprint:
        agent_memory.store("quickgen_blueprint", blueprint, agent_name="architect")
        _log(f"Blueprint ready — {len(blueprint)} chars", "architect")
    else:
        _log("Blueprint generation failed — Coder will proceed without it", "architect")
    return blueprint or ""


# ── PLANNER ───────────────────────────────────────────────────────────────────

def _run_planner(prompt, language, blueprint):
    """
    Planner breaks the request into a clear implementation checklist.
    Gives Coder an ordered list of exactly what to implement.
    """
    _log(f"Planning implementation for: {prompt[:70]}...", "planner")

    planner_prompt = f"""You are a software project planner. A Coder is about to write a single {language} file.

USER REQUEST: {prompt}
ARCHITECT BLUEPRINT:
{blueprint if blueprint else "(not available)"}

Create a concise implementation checklist — an ordered list of exactly what the Coder must implement.
Format:
1. [specific thing to implement]
2. [specific thing to implement]
...

Rules:
- Be specific — name actual functions, UI elements, game mechanics, etc.
- Order matters — list foundational things first
- Maximum 15 items
- No vague items like "make it work" or "add styling"

Return ONLY the numbered checklist. No intro, no explanation.
"""
    plan = ask_for_agent(planner_prompt, "planner")
    if plan:
        agent_memory.store("quickgen_plan", plan, agent_name="planner")
        _log(f"Implementation plan ready — {len(plan.splitlines())} items", "planner")
    else:
        _log("Planner returned nothing — Coder will proceed without checklist", "planner")
    return plan or ""


# ── CODER ─────────────────────────────────────────────────────────────────────

def _build_coder_prompt(prompt, language, blueprint, plan, coder_rules, past_lessons):
    """Builds the full coder prompt with strict polish and correctness requirements."""
    blueprint_section = f"\nARCHITECT BLUEPRINT (follow this design exactly):\n{blueprint}\n" if blueprint else ""
    plan_section      = f"\nIMPLEMENTATION CHECKLIST (every item is mandatory):\n{plan}\n" if plan else ""
    lessons_section   = ("\nLESSONS FROM PAST MISTAKES \u2014 do not repeat these:\n"
                         + "\n".join(f"- {l}" for l in past_lessons)) if past_lessons else ""
    rules_section     = f"\nCODER RULES \u2014 non-negotiable:\n{coder_rules}" if coder_rules else ""

    lang_lower = language.lower()
    is_html    = "html" in lang_lower
    is_python  = "python" in lang_lower

    if is_html:
        polish_block = (
            "\nUI POLISH \u2014 the Reviewer checks every one of these:\n"
            "- Body background: #0e1117. Surface/cards: #151b26. Borders: #252f45.\n"
            "- Primary text: #e2e8f0. Secondary: #8892a4. Accent: #7c6af7. Hover: #9b8dff.\n"
            "- Success: #3dd68c. Error: #f05252. Warning: #f5a623.\n"
            "- Font: 'Inter', 'Segoe UI', system-ui for UI. 'JetBrains Mono' for code/numbers.\n"
            "- EVERY button: hover bg shift + cursor:pointer + transition:all 0.15s ease.\n"
            "- EVERY button: padding min 10px 20px. No tiny targets.\n"
            "- EVERY interactive element has visible focus state.\n"
            "- Disabled elements: opacity:0.45 + cursor:not-allowed.\n"
            "- Cards: border-radius:12px, border:1px solid #252f45.\n"
            "- Spacing: 8px base unit. Use 8,16,24,32,48px only.\n"
            "- Body text line-height:1.6. Uppercase labels letter-spacing:1.5px.\n"
            "- Loading states for async ops. Styled empty states, no blank boxes.\n"
            "- FOR GAMES: canvas centered with glow border. Score large + always visible.\n"
            "  Game-over overlay: styled, final score shown, restart button present.\n"
            "  Game loop: requestAnimationFrame ONLY, never setInterval for animation.\n"
            "  Keyboard controls active immediately on page load, no click-to-focus.\n"
        )
    elif is_python:
        polish_block = (
            "\nPYTHON QUALITY \u2014 the Reviewer checks every one of these:\n"
            "- f-strings for ALL formatting \u2014 never .format() or %.\n"
            "- Type hints on every function signature.\n"
            "- pathlib.Path for all file paths \u2014 no string concatenation.\n"
            "- if __name__ == '__main__': guard on all executable scripts.\n"
            "- logging module for status output \u2014 not bare print().\n"
            "- Context managers (with) for all file and resource handling.\n"
            "- Constants in UPPER_SNAKE_CASE at the top of the file.\n"
            "- Imports: stdlib \u2192 third-party \u2192 local, blank line between groups.\n"
            "- Every function: one docstring, under 25 lines, one responsibility.\n"
            "- Names describe what the variable IS. No: data, result, temp, val.\n"
        )
    else:
        polish_block = (
            "\nCODE QUALITY:\n"
            "- Descriptive names. Every function documented. Constants at top.\n"
        )

    issues_hint = ("\nREVIEWER WILL SPECIFICALLY CHECK:\n"
                   "- Every UI element in the checklist has a working implementation\n"
                   "- No function returns undefined/None where a real value is expected\n"
                   "- Game/app is interactive and responds to user input immediately\n")

    return (
        f"You are a principal software engineer at a world-class tech company.\n"
        f"You write production code shipped to real users, reviewed by senior engineers.\n"
        f"Your bar: every line should be something you are genuinely proud of.\n\n"
        f"TASK: {prompt}\n"
        f"LANGUAGE: {language}\n"
        f"{blueprint_section}{plan_section}{rules_section}{lessons_section}\n"
        f"{polish_block}"
        f"{issues_hint}\n"
        f"CORRECTNESS \u2014 automatic rejection if any fail:\n"
        f"1. Implement EVERY feature in the task \u2014 read every word, nothing skipped.\n"
        f"2. All imports correct \u2014 only stdlib or real well-known packages. No invented names.\n"
        f"3. ZERO placeholders \u2014 no pass, no TODO, no raise NotImplementedError.\n"
        f"4. try/except on ALL I/O, network, file, and parse operations.\n"
        f"5. Every function: one responsibility, under 25 lines.\n"
        f"6. Code runs successfully on first execution \u2014 zero user changes needed.\n"
        f"7. For games: controls work, loop at 60fps via rAF, score visible and live.\n\n"
        f"Before writing: picture the finished product. Plan functions. Verify checklist.\n\n"
        f"PRE-SUBMIT SELF-CHECK — run through this BEFORE returning your code.\n"
        f"If any answer is NO, fix it first. Do not return code that fails any item.\n\n"
        f"[ ] 1. Did I implement EVERY feature in the request? (Read it again word by word.)\n"
        f"[ ] 2. Every function has a real body — no pass, no TODO, no return None as placeholder?\n"
        f"[ ] 3. try/except on every file read/write, network call, and JSON parse?\n"
        f"[ ] 4. For HTML/JS: EVERY button has explicit :hover CSS with color change?\n"
        f"[ ] 5. For HTML/JS: background is #0e1117, NOT white or browser default?\n"
        f"[ ] 6. For HTML/JS: custom font stack specified (Inter, Segoe UI, JetBrains Mono)?\n"
        f"[ ] 7. For games: game loop uses requestAnimationFrame ONLY, never setInterval?\n"
        f"[ ] 8. For games: score is visible and styled prominently?\n"
        f"[ ] 9. For games: styled game-over screen with restart button exists?\n"
        f"[ ] 10. For games: keyboard controls work immediately on page load, no click needed?\n"
        f"[ ] 11. Would a senior engineer be proud to ship this? Or does it look unfinished?\n\n"
        f"If any answer is NO — fix it before returning.\n\n"
        f"Return COMPLETE, RUNNABLE code ONLY.\n"
        f"No markdown fences. No explanations. No preamble.\n"
        f"Start with imports (Python) or <!DOCTYPE html> (HTML/JS).\n"
    )

# ── REVIEWER ──────────────────────────────────────────────────────────────────

def _build_reviewer_prompt(prompt, language, code, coder_rules=""):
    """Adversarial checklist-based reviewer prompt with Visual QA for frontend files."""
    lang_lower   = language.lower()
    is_frontend  = any(x in lang_lower for x in ("html", "js", "javascript", "css"))

    visual_qa = ""
    if is_frontend:
        visual_qa = """
VISUAL QA — FRONTEND SPECIFIC (weighted equally to functional items):
[ ] 9.  Open this file in Chrome mentally. One sentence: what does the user see?
        Does it look like a professional product or a student demo?
[ ] 10. Clear visual hierarchy? Title visible? Content structured logically?
[ ] 11. Dark theme correctly applied? body background ≈ #0e1117, NOT white or browser default.
        Surface/card backgrounds ≈ #151b26. Text ≈ #e2e8f0. Accent ≈ #7c6af7.
        If any of these are wrong or missing — FAIL.
[ ] 12. ALL buttons have explicit CSS :hover rule with color/bg change + cursor:pointer +
        transition. Minimum padding 10px 20px. If ANY button lacks hover styles — FAIL.
[ ] 13. Custom font specified? (Inter, Segoe UI, JetBrains Mono, or similar). NOT browser default.
        Line-height set on body text? Letter-spacing on uppercase labels?
[ ] 14. Consistent spacing? padding/margin on all containers? Nothing touching edges?
[ ] 15. For GAMES:
          - Score prominent and visible (large, styled, not a tiny corner number)?
          - Game-over screen styled (overlay, final score, restart button)?
          - Game loop uses requestAnimationFrame ONLY, never setInterval?
          - Keyboard controls work immediately on page load, no canvas click required?
[ ] 16. Empty/loading/error states styled? No blank white boxes anywhere?

AUTOMATIC FAIL if visual description includes: "basic", "simple", "minimal",
"plain", "default", "standard" — these mean polish was skipped.
"""

    rules_block = ("CODER RULES:\n" + coder_rules) if coder_rules else ""
    return (
        "You are a senior adversarial code reviewer. Assume this code has at least one bug — find it.\n\n"
        f"ORIGINAL REQUEST: {prompt}\n"
        f"LANGUAGE: {language}\n"
        + (rules_block + "\n" if rules_block else "")
        + f"\nCODE TO REVIEW:\n{code}\n\n"
        "Work through EVERY item — actively try to break each one mentally:\n\n"
        "FUNCTIONAL CHECKLIST:\n"
        "[ ] 1. Implements EXACTLY what was requested — check every word of the request\n"
        "[ ] 2. All imports correct — no typos, no missing packages\n"
        "[ ] 3. No placeholder functions (no pass, no TODO, no return None where logic needed)\n"
        "[ ] 4. Error handling exists where failures are possible\n"
        "[ ] 5. Code will run without crashing on first execution\n"
        "[ ] 6. For HTML/JS: dark theme applied, all buttons have handlers, game loop runs\n"
        "[ ] 7. For games: controls work, collision detection correct, score updates\n"
        "[ ] 8. No hardcoded test data, no debug prints left in\n"
        + visual_qa
        + "\nRespond in this EXACT format:\n"
        "SCORE: [1-10]\n"
        "VERDICT: [PASS or FAIL]\n"
        "REASON: [one sentence — the single most important finding]\n"
        "ISSUES:\n"
        "- [specific issue with line reference if possible]\n"
        '- [another issue, or \"none\" if genuinely none]\n\n'
        f"VERDICT is PASS only if SCORE >= {REVIEWER_PASS_THRESHOLD} AND every item passes.\n"
        "One failed item = FAIL regardless of score.\n"
        "For frontend files: failing any visual item (#9-16) = FAIL, same as failing a functional item.\n"
    )


def _parse_verdict(response):
    """Parses SCORE/VERDICT/REASON/ISSUES from reviewer response."""
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
    if result["score"] < REVIEWER_PASS_THRESHOLD:
        result["verdict"] = "FAIL"
        if not result["issues"]:
            result["issues"] = [f"Score {result['score']}/10 below minimum {REVIEWER_PASS_THRESHOLD}"]
    return result


# ── DEBUGGER ──────────────────────────────────────────────────────────────────

def _build_debugger_prompt(prompt, language, code, issues, attempt, max_attempts):
    """Targeted fix prompt — fix only what Reviewer said."""
    issues_text = "\n".join(f"- {i}" for i in issues)
    return f"""You are a senior developer fixing specific code issues. Attempt {attempt}/{max_attempts}.

ORIGINAL REQUEST: {prompt}
LANGUAGE: {language}

THE REVIEWER FOUND THESE EXACT PROBLEMS:
{issues_text}

CODE THAT FAILED REVIEW:
{code}

INSTRUCTIONS:
1. Fix ONLY the issues listed above — keep everything else exactly as is
2. Every fix must be complete — no TODO, no pass
3. If a game mechanic is broken, fix the logic not just the syntax
4. Return the COMPLETE fixed file — not just the changed parts

Return complete corrected code ONLY. No markdown. No explanation.
"""


# ── VALIDATOR ─────────────────────────────────────────────────────────────────

def _run_validator(prompt, language, code):
    """
    Validator runs two checks:
    1. Static syntax/structure check (free, instant)
    2. AI sanity check (does code match request?)
    Returns (passed, reason)
    """
    _log(f"Validating {language} code...", "validator")

    # Static check
    if language.lower() in ("python", "py"):
        try:
            ast.parse(code)
        except SyntaxError as e:
            reason = f"Syntax error line {e.lineno}: {e.msg}"
            _log(f"VALIDATOR FAIL — {reason}", "validator")
            return False, reason
    elif language.lower() in ("html", "html/js"):
        low = code.lower()
        if "</body>" not in low:
            _log("VALIDATOR FAIL — HTML truncated, no </body>", "validator")
            return False, "HTML truncated — </body> missing"
        if "</html>" not in low:
            _log("VALIDATOR FAIL — HTML truncated, no </html>", "validator")
            return False, "HTML truncated — </html> missing"

    # AI sanity check
    sanity_prompt = f"""Quick sanity check on generated code.

REQUEST: {prompt}
LANGUAGE: {language}

CODE (first 2000 chars):
{code[:2000]}

Does this code plausibly implement the request? Check only: does it do the job?
Do not check style or edge cases.

Answer EXACTLY:
SANE: YES
or
SANE: NO
REASON: [one sentence what is missing]
"""
    response = ask_for_agent(sanity_prompt, "validator")
    if not response:
        _log("Validator AI unavailable — passing on static check alone", "validator")
        return True, "ai unavailable"

    if "SANE: YES" in response.upper():
        _log("Validator PASSED ✓", "validator")
        return True, "ok"
    if "SANE: NO" in response.upper():
        lines  = response.strip().splitlines()
        reason = next(
            (l.split("REASON:", 1)[1].strip() for l in lines if "REASON:" in l.upper()),
            "Code does not match request"
        )
        _log(f"Validator FAIL — {reason}", "validator")
        return False, reason

    _log("Validator ambiguous — passing", "validator")
    return True, "ambiguous"


# ── TESTER ────────────────────────────────────────────────────────────────────

def _run_tester(prompt, language, code):
    """
    Tester runs structural checks:
    - Python: AST parse + placeholder check + no obvious crashes
    - HTML: structure + interactive element checks
    - JS: brace balance
    Returns (passed, reason)
    """
    _log(f"Testing {language} code...", "tester")

    lang = language.lower()

    if lang in ("python", "py"):
        # AST parse
        try:
            ast.parse(code)
        except SyntaxError as e:
            reason = f"Syntax error line {e.lineno}: {e.msg}"
            _log(f"TESTER FAIL — {reason}", "tester")
            return False, reason

        # Placeholder check
        placeholder_patterns = [
            (r"^\s*pass\s*$",               "bare 'pass' statement"),
            (r"raise\s+NotImplementedError", "raise NotImplementedError"),
            (r"#\s*TODO",                    "TODO comment"),
            (r"#\s*FIXME",                   "FIXME comment"),
            (r"\.\.\.(\s*#.*)?$",            "ellipsis placeholder"),
        ]
        for i, line in enumerate(code.splitlines(), 1):
            for pattern, label in placeholder_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    reason = f"line {i}: {label} detected"
                    _log(f"TESTER FAIL — {reason}", "tester")
                    return False, reason

    elif lang in ("html", "html/js"):
        low = code.lower()
        if "<body" not in low:
            return False, "missing <body> tag"
        if "</body>" not in low:
            return False, "HTML truncated — </body> missing"
        if "</html>" not in low:
            return False, "HTML truncated — </html> missing"

        # Check buttons have handlers
        buttons = re.findall(r'<button([^>]*)>', code, re.IGNORECASE)
        for attrs in buttons:
            has_any = any(k in attrs.lower() for k in ["onclick", "id=", "class=", "type=", "data-"])
            if not has_any:
                return False, "button tag has no onclick, id, or class"

    elif lang in ("javascript", "js"):
        opens  = code.count("{")
        closes = code.count("}")
        if abs(opens - closes) > 2:
            return False, f"mismatched braces ({opens} open, {closes} close)"

    _log("Tester PASSED ✓", "tester")
    return True, "ok"


# ── ORCHESTRATOR LOGGER ───────────────────────────────────────────────────────

def _orchestrate_log(msg):
    """Logs a message as Orchestrator so it shows in the Agents tab."""
    _log(msg, "orchestrator")


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def run_single_file_pipeline(prompt, language="Python"):
    """
    Runs the FULL 8-agent pipeline for a single file:
    Architect → Planner → Coder → Reviewer → Debugger → Validator → Tester

    Called by /api/generate_code.

    Returns dict:
      - code:     final code (str)
      - score:    reviewer score (int)
      - attempts: debug attempts made (int)
      - passed:   reviewer gave PASS (bool)
      - log:      list of status messages
    """
    log_messages = []

    def log(msg, agent="pipeline"):
        agent_chat.log(agent, msg)
        log_messages.append(msg)

    # Load coder rules
    coder_rules = ""
    rules_path  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CODER_RULES.md")
    if os.path.exists(rules_path):
        with open(rules_path) as f:
            coder_rules = f.read()

    # Past lessons from memory
    past_lessons = agent_memory.get_lessons(prompt)

    _orchestrate_log(f"Quick-generate pipeline starting — {language} — {prompt[:60]}...")

    # ── STEP 1: ARCHITECT ─────────────────────────────────────────────────
    blueprint = _run_architect(prompt, language)

    # ── STEP 2: PLANNER ───────────────────────────────────────────────────
    plan = _run_planner(prompt, language, blueprint)

    # ── STEP 3: CODER ─────────────────────────────────────────────────────
    log(f"Coder writing {language} code...", "coder")
    coder_prompt = _build_coder_prompt(prompt, language, blueprint, plan, coder_rules, past_lessons)
    code = ask_for_agent(coder_prompt, "coder")

    if not code or not code.strip():
        log("Coder returned empty — cannot proceed", "coder")
        _orchestrate_log("Pipeline FAILED — Coder returned nothing")
        return {"code": "", "score": 0, "attempts": 0, "passed": False, "log": log_messages}

    log(f"Coder complete — {len(code.splitlines())} lines generated", "coder")
    _orchestrate_log(f"Coder done — {len(code.splitlines())} lines — sending to Reviewer")

    # ── STEP 4: REVIEWER ──────────────────────────────────────────────────
    log(f"Reviewer checking {language} code...", "reviewer")
    reviewer_prompt  = _build_reviewer_prompt(prompt, language, code, coder_rules)
    review_response  = ask_for_agent(reviewer_prompt, "reviewer")

    if not review_response:
        log("Reviewer unavailable — skipping to Validator", "reviewer")
        verdict = {"verdict": "PASS", "score": 0, "issues": [], "reason": "reviewer unavailable"}
    else:
        verdict = _parse_verdict(review_response)
        log(f"Reviewer: {verdict['verdict']} ({verdict['score']}/10) — {verdict.get('reason','')}", "reviewer")
        _orchestrate_log(f"Reviewer verdict: {verdict['verdict']} score {verdict['score']}/10")

    # ── STEP 5: DEBUGGER (if Reviewer failed) ─────────────────────────────
    current_code   = code
    current_issues = verdict["issues"]
    debug_attempts = 0

    if verdict["verdict"] == "FAIL":
        for attempt in range(1, MAX_DEBUG_ATTEMPTS + 1):
            debug_attempts = attempt
            issues_summary = ', '.join(current_issues[:2]) if current_issues else 'unspecified issues'
            log(f"Debugger attempt {attempt}/{MAX_DEBUG_ATTEMPTS} — fixing: {issues_summary}", "debugger")
            _orchestrate_log(f"Debugger attempt {attempt} — {issues_summary}")

            fix_prompt = _build_debugger_prompt(prompt, language, current_code, current_issues, attempt, MAX_DEBUG_ATTEMPTS)
            fixed      = ask_for_agent(fix_prompt, "debugger")

            if not fixed or not fixed.strip():
                log(f"Debugger attempt {attempt} returned empty", "debugger")
                break

            current_code = fixed
            log(f"Debugger fixed — re-sending to Reviewer", "debugger")

            re_review = ask_for_agent(
                _build_reviewer_prompt(prompt, language, current_code, coder_rules),
                "reviewer"
            )
            if not re_review:
                log("Reviewer unavailable on re-check — keeping fixed code", "reviewer")
                verdict["verdict"] = "PASS"
                break

            verdict = _parse_verdict(re_review)
            log(f"Reviewer re-check: {verdict['verdict']} ({verdict['score']}/10)", "reviewer")
            _orchestrate_log(f"Re-review: {verdict['verdict']} ({verdict['score']}/10)")

            if verdict["verdict"] == "PASS":
                log(f"Code passed review after {attempt} debug attempt(s) ✓", "reviewer")
                agent_memory.store_lesson(
                    error="; ".join(current_issues[:3]),
                    fix=f"Fixed on debug attempt {attempt}",
                    task_type=prompt[:60],
                    agent_name="debugger",
                )
                break

            current_issues = verdict["issues"]

    # ── STEP 6: VALIDATOR ─────────────────────────────────────────────────
    _orchestrate_log("Sending to Validator...")
    valid, valid_reason = _run_validator(prompt, language, current_code)

    if not valid:
        # Validator failed — one more Debugger pass targeting the structural issue
        log(f"Validator rejected — {valid_reason} — one more Debugger pass", "validator")
        _orchestrate_log(f"Validator FAIL — {valid_reason} — final Debugger pass")
        fix_prompt   = _build_debugger_prompt(prompt, language, current_code, [f"Validator: {valid_reason}"], debug_attempts + 1, MAX_DEBUG_ATTEMPTS + 1)
        fixed        = ask_for_agent(fix_prompt, "debugger")
        if fixed and fixed.strip():
            current_code = fixed
            log("Validator fix applied", "debugger")
            _orchestrate_log("Validator fix applied — proceeding to Tester")

    # ── STEP 7: TESTER ────────────────────────────────────────────────────
    _orchestrate_log("Sending to Tester...")
    test_passed, test_reason = _run_tester(prompt, language, current_code)

    if not test_passed:
        log(f"Tester rejected — {test_reason} — one more Debugger pass", "tester")
        _orchestrate_log(f"Tester FAIL — {test_reason} — final Debugger pass")
        fix_prompt   = _build_debugger_prompt(prompt, language, current_code, [f"Tester: {test_reason}"], debug_attempts + 1, MAX_DEBUG_ATTEMPTS + 1)
        fixed        = ask_for_agent(fix_prompt, "debugger")
        if fixed and fixed.strip():
            current_code = fixed
            log("Tester fix applied", "debugger")

    # ── STEP 8: VISUAL TESTER (HTML/JS only) ───────────────────────────
    visual_passed = True
    if any(x in language.lower() for x in ("html", "js", "javascript")):
        _orchestrate_log("Sending to Visual Tester (headless browser + vision model)...")
        try:
            from visual_tester import VisualTester
            vt    = VisualTester()
            _vtask = {"task_id": "quickgen", "file": "output.html",
                      "description": prompt, "expected_output": prompt}
            visual_passed, visual_reason, _ = vt.test_html(_vtask, current_code)
            if not visual_passed:
                log(f"Visual tester rejected — {visual_reason} — one more Debugger pass", "tester")
                _orchestrate_log(f"Visual FAIL — {visual_reason} — final Debugger pass")
                fix_prompt = _build_debugger_prompt(
                    prompt, language, current_code,
                    [f"Visual QA: {visual_reason}"],
                    debug_attempts + 1, MAX_DEBUG_ATTEMPTS + 1
                )
                fixed = ask_for_agent(fix_prompt, "debugger")
                if fixed and fixed.strip():
                    current_code  = fixed
                    visual_passed = True
                    log("Visual fix applied", "debugger")
        except Exception as e:
            log(f"Visual tester unavailable — {e} — skipping", "tester")

    # ── DONE ──────────────────────────────────────────────────────────────────────────
    final_passed = verdict["verdict"] == "PASS" and valid and test_passed and visual_passed
    _orchestrate_log(
        f"Pipeline complete — {'PASSED ✓' if final_passed else 'best effort'} — "
        f"score {verdict.get('score', 0)}/10 — {debug_attempts} debug attempt(s)"
    )

    return {
        "code":     current_code,
        "score":    verdict.get("score", 0),
        "attempts": debug_attempts,
        "passed":   final_passed,
        "log":      log_messages,
    }
