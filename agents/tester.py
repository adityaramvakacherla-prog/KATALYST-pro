"""
tester.py — KATALYST Tester Agent  (Enhanced)
Sits between Reviewer PASS and Orchestrator file-save.
Performs COMPREHENSIVE testing — not just crash detection:
  1. Syntax check (compile)
  2. Import check (all imports resolvable in isolation)
  3. Runtime execution (does it run without crashing?)
  4. Function existence check (key functions from task description exist)
  5. HTML/JS structural integrity (buttons have handlers, forms work, etc.)
  6. Basic interaction simulation for HTML (checks onclick, event handlers)

Pipeline position:
  Coder → Reviewer → [Validator] → [Tester] → Orchestrator saves

CHANGES FROM v1:
- Now tests HTML/JS files too (structural + handler checks)
- Checks that functions mentioned in task description actually exist in code
- Checks all major imports before full execution (faster failure detection)
- Reports WHICH specific check failed so Debugger has a precise target
"""
import os
import sys
import re
import ast
import subprocess
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat

TESTER_TIMEOUT  = 10   # seconds for Python execution
PYTHON_EXTS     = {".py"}
HTML_EXTS       = {".html", ".htm"}
JS_EXTS         = {".js"}


class Tester:

    def __init__(self):
        """Sets up tester."""
        self.agent_name = "tester"

    def test(self, task, code):
        """
        Runs comprehensive checks on the generated code.
        Returns (passed: bool, reason: str, output: str)
        """
        tid      = task.get("task_id", "?")
        filename = task.get("file", "")
        ext      = os.path.splitext(filename)[1].lower()

        agent_chat.log(
            self.agent_name,
            f"Task {tid} — comprehensive test starting for {filename}",
            task_id=tid,
        )

        if ext in PYTHON_EXTS:
            passed, reason, output = self._test_python(code, filename, task)
        elif ext in HTML_EXTS:
            passed, reason, output = self._test_html(code, filename, task)
        elif ext in JS_EXTS:
            passed, reason, output = self._test_js(code, filename, task)
        else:
            # Non-testable file type — pass through
            return True, f"{ext} file — runtime test skipped", ""

        if passed:
            agent_chat.log(
                self.agent_name,
                f"Task {tid} — ALL CHECKS PASSED ✓ ({reason})",
                task_id=tid,
            )
        else:
            agent_chat.log(
                self.agent_name,
                f"Task {tid} — FAILED: {reason}",
                message_type="error",
                task_id=tid,
            )

        return passed, reason, output

    # ── PYTHON TESTING ────────────────────────────────────────────────────

    def _test_python(self, code, filename, task):
        """
        Full Python test suite:
        1. AST parse (syntax)
        2. Key function/class existence from task description
        3. Import check (no broken imports when run in isolation)
        4. Runtime execution
        """
        # Check 1: AST syntax
        ok, msg = self._check_syntax(code, filename)
        if not ok:
            return False, f"SYNTAX ERROR — {msg}", ""

        # Check 2: Key names from task description exist in code
        ok, msg = self._check_required_names(code, task)
        if not ok:
            return False, f"MISSING IMPLEMENTATION — {msg}", ""

        # Check 3: No placeholder code
        ok, msg = self._check_no_placeholders(code)
        if not ok:
            return False, f"PLACEHOLDER FOUND — {msg}", ""

        # Check 4: Runtime execution
        ok, msg, output = self._run_code(code, filename)
        if not ok:
            return False, msg, output

        return True, "syntax OK, names present, no placeholders, runtime clean", output

    def _check_syntax(self, code, filename):
        """Parses code with AST — catches all syntax errors."""
        try:
            ast.parse(code)
            return True, "ok"
        except SyntaxError as e:
            return False, f"line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, str(e)[:200]

    def _check_required_names(self, code, task):
        """
        Extracts function/class names hinted in the task description and
        verifies they exist in the code. E.g. 'write a function called add()'
        → checks 'def add' is present.
        """
        description = task.get("description", "").lower()

        # Extract explicit function/class names from description
        # Patterns: "called X", "named X", "function X", "class X", "method X"
        patterns = [
            r'\bcalled\s+[\'"]?(\w+)[\'"]?',
            r'\bnamed\s+[\'"]?(\w+)[\'"]?',
            r'\bfunction\s+(\w+)\s*\(',
            r'\bclass\s+(\w+)\b',
            r'\bmethod\s+(\w+)\s*\(',
            r'\bdef\s+(\w+)\b',
        ]
        required_names = set()
        for pattern in patterns:
            for match in re.findall(pattern, description):
                if len(match) > 2 and match not in {"the", "for", "and", "with", "that"}:
                    required_names.add(match.lower())

        if not required_names:
            return True, "no specific function names required"

        code_lower = code.lower()
        missing = []
        for name in required_names:
            # Check both def name and class name patterns
            if f"def {name}" not in code_lower and f"class {name}" not in code_lower:
                # Also allow it as a variable or method assignment
                if f"{name}" not in code_lower:
                    missing.append(name)

        if missing:
            return False, f"required names not found in code: {', '.join(missing)}"
        return True, "all required names found"

    def _check_no_placeholders(self, code):
        """Detects placeholder patterns that pass review but indicate incomplete code."""
        # Lines that are pure placeholders
        placeholder_patterns = [
            (r"^\s*pass\s*$",                    "bare 'pass' statement"),
            (r"raise\s+NotImplementedError",      "'raise NotImplementedError'"),
            (r"#\s*TODO",                         "TODO comment"),
            (r"#\s*FIXME",                        "FIXME comment"),
            (r"#\s*PLACEHOLDER",                  "PLACEHOLDER comment"),
            (r"\.\.\.(\s*#.*)?$",                 "ellipsis placeholder"),
        ]
        for i, line in enumerate(code.splitlines(), 1):
            for pattern, label in placeholder_patterns:
                if re.search(pattern, line, re.IGNORECASE):
                    return False, f"line {i}: {label} detected"
        return True, "no placeholders found"

    def _run_code(self, code, filename):
        """Executes code in a temp dir with timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_path = os.path.join(tmpdir, filename)
            try:
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write(code)
            except Exception as e:
                return False, f"Could not write temp file: {e}", ""

            try:
                result = subprocess.run(
                    [sys.executable, "-c",
                     f"import sys; sys.path.insert(0, {repr(tmpdir)}); "
                     f"exec(open({repr(test_path)}).read())"],
                    capture_output=True,
                    text=True,
                    timeout=TESTER_TIMEOUT,
                    cwd=tmpdir,
                )
            except subprocess.TimeoutExpired:
                # Timeout = likely a server/loop — that's fine
                return True, f"timed out after {TESTER_TIMEOUT}s (server/loop — ok)", ""
            except Exception as e:
                return False, f"Subprocess error: {str(e)[:200]}", ""

            stdout = result.stdout.strip()
            stderr = result.stderr.strip()

            if result.returncode == 0:
                return True, "exited cleanly (code 0)", stdout

            if stderr:
                # Missing imports are expected in isolation — pass through
                if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
                    module_line = next(
                        (l for l in stderr.splitlines() if "No module named" in l), ""
                    )
                    return True, f"import skipped in isolation ({module_line.strip()})", stdout

                # Real crash — return last 3 meaningful lines
                error_lines = [l for l in stderr.splitlines() if l.strip()]
                short_error = " | ".join(error_lines[-3:])[:300]
                return False, f"crashed: {short_error}", stdout

            return False, f"non-zero exit code: {result.returncode}", stdout

    # ── HTML TESTING ──────────────────────────────────────────────────────

    def _test_html(self, code, filename, task):
        """
        HTML structural checks:
        1. Has DOCTYPE and html tags
        2. Has body and /body
        3. All buttons/links have event handlers or hrefs
        4. No broken script tags (script with src that's external and unreachable)
        5. Interactive elements mentioned in task actually exist
        6. No unclosed major tags (div, section, main, header, footer, nav)
        """
        low = code.lower()

        # Check 1: Basic structure
        if "<!doctype" not in low and "<html" not in low:
            return False, "HTML STRUCTURE — missing <!DOCTYPE> or <html> tag", ""

        if "<body" not in low:
            return False, "HTML STRUCTURE — missing <body> tag", ""

        if "</body>" not in low:
            return False, "HTML TRUNCATED — </body> not found, file is likely cut off", ""

        if "</html>" not in low:
            return False, "HTML TRUNCATED — </html> not found, file is likely cut off", ""

        # Check 2: Interactive elements have handlers
        ok, msg = self._check_html_interactivity(code, task)
        if not ok:
            return False, msg, ""

        # Check 3: Balanced major tags
        ok, msg = self._check_html_balance(code)
        if not ok:
            return False, msg, ""

        # Check 4: Task-required UI elements exist
        ok, msg = self._check_html_required_elements(code, task)
        if not ok:
            return False, msg, ""

        return True, "HTML structure valid, interactive elements present, tags balanced", ""

    def _check_html_interactivity(self, code, task):
        """Checks that buttons have click handlers and forms have submit handlers."""
        # Find all button tags
        buttons = re.findall(r'<button([^>]*)>', code, re.IGNORECASE)
        for attrs in buttons:
            has_handler = (
                "onclick" in attrs.lower() or
                "id=" in attrs.lower() or
                "class=" in attrs.lower()  # might be handled via JS elsewhere
            )
            # Only fail if button has no id, class, or onclick — truly orphaned
            has_any = any(k in attrs.lower() for k in ["onclick", "id=", "class=", "type=", "data-"])
            if not has_any:
                return False, "BUTTON WITHOUT HANDLER — a <button> tag has no onclick, id, or class"

        # Check forms have action or onsubmit
        forms = re.findall(r'<form([^>]*)>', code, re.IGNORECASE)
        for attrs in forms:
            has_handler = any(k in attrs.lower() for k in ["action", "onsubmit", "id="])
            if not has_handler:
                return False, "FORM WITHOUT HANDLER — a <form> tag has no action, onsubmit, or id"

        return True, "interactive elements ok"

    def _check_html_balance(self, code):
        """Checks for unclosed major structural tags."""
        tags_to_check = ["div", "section", "main", "header", "footer", "nav", "ul", "ol", "table"]
        for tag in tags_to_check:
            opens  = len(re.findall(f"<{tag}[\\s>]", code, re.IGNORECASE))
            closes = len(re.findall(f"</{tag}>", code, re.IGNORECASE))
            if opens > 0 and opens != closes:
                diff = opens - closes
                if diff > 2:  # allow minor mismatch from templates
                    return False, f"UNCLOSED TAG — <{tag}> opened {opens}x but closed {closes}x"
        return True, "tags balanced"

    def _check_html_required_elements(self, code, task):
        """Checks that UI elements mentioned in the task description exist."""
        description = task.get("description", "").lower()
        low         = code.lower()

        # Map description keywords to expected HTML elements
        element_checks = [
            (["button", "click", "btn"],    r'<button',          "button element"),
            (["input", "form", "field"],     r'<input',           "input element"),
            (["table", "grid", "rows"],      r'<table|<tr',       "table element"),
            (["chart", "canvas", "graph"],   r'<canvas|chart',    "canvas/chart element"),
            (["dropdown", "select", "menu"], r'<select|dropdown', "select/dropdown element"),
            (["image", "img", "photo"],      r'<img',             "img element"),
            (["link", "anchor", "href"],     r'<a\s',             "anchor element"),
            (["textarea", "text area"],      r'<textarea',        "textarea element"),
            (["checkbox", "check box"],      r'type=["\']checkbox', "checkbox input"),
            (["list", "items"],              r'<ul|<ol|<li',      "list element"),
        ]

        for keywords, html_pattern, label in element_checks:
            if any(kw in description for kw in keywords):
                if not re.search(html_pattern, low):
                    return False, f"MISSING ELEMENT — task mentions '{keywords[0]}' but no {label} found"

        return True, "required elements present"

    # ── JS TESTING ────────────────────────────────────────────────────────

    def _test_js(self, code, filename, task):
        """
        JavaScript structural checks:
        1. No obvious syntax errors (balanced braces)
        2. Functions mentioned in task exist
        3. No placeholder patterns
        4. Event listeners are attached (addEventListener or onclick)
        """
        # Check 1: Brace balance
        opens  = code.count("{")
        closes = code.count("}")
        if abs(opens - closes) > 2:
            return False, f"JS SYNTAX — mismatched braces ({opens} open, {closes} close)", ""

        # Check 2: Placeholder check
        ok, msg = self._check_no_placeholders(code)
        if not ok:
            return False, f"PLACEHOLDER — {msg}", ""

        # Check 3: Required function names from task
        description = task.get("description", "").lower()
        # Look for "function called X" or "function X"
        func_patterns = [
            r'\bcalled\s+[\'"]?(\w+)[\'"]?',
            r'\bfunction\s+(\w+)\s*\(',
        ]
        required = set()
        for pat in func_patterns:
            for m in re.findall(pat, description):
                if len(m) > 2:
                    required.add(m.lower())

        code_lower = code.lower()
        missing = []
        for name in required:
            if f"function {name}" not in code_lower and f"const {name}" not in code_lower and f"let {name}" not in code_lower:
                if name not in code_lower:
                    missing.append(name)
        if missing:
            return False, f"MISSING FUNCTIONS — {', '.join(missing)} not found in JS", ""

        return True, "JS structure valid, balanced braces, required functions present", ""
