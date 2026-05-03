import subprocess
import sys


def run_and_check(code_string, expected_output_description):
    # Writes code to a temp file and runs it to check if it works
    with open("temp_test.py", "w") as f:
        f.write(code_string)

    # Skip running code that needs user input — just check syntax instead
    interactive_keywords = ["input(", "input ("]
    is_interactive = any(kw in code_string for kw in interactive_keywords)

    if is_interactive:
        try:
            compile(code_string, "temp_test.py", "exec")
            return True, "Interactive code — syntax check passed"
        except SyntaxError as syntax_error:
            return False, f"SyntaxError: {str(syntax_error)}"

    try:
        result = subprocess.run(
            [sys.executable, "temp_test.py"],
            capture_output=True,
            text=True,
            timeout=30
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr
    except subprocess.TimeoutExpired:
        return False, "CODE TIMED OUT — probably infinite loop"
    except Exception as error:
        return False, str(error)


def classify_error(error_message):
    # Classifies the type of error to decide how to retry
    error_lower = error_message.lower()
    if "syntaxerror" in error_lower or "indentationerror" in error_lower:
        return "syntax"
    elif "importerror" in error_lower or "modulenotfounderror" in error_lower:
        return "import"
    elif "typeerror" in error_lower or "attributeerror" in error_lower:
        return "logic"
    elif "timeout" in error_lower:
        return "timeout"
    else:
        return "unknown"
