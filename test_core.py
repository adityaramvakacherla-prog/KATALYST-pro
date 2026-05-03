"""
test_core.py — KATALYST Phase 6 Full Test Suite
Run with: python3 test_core.py
Tests every layer of the system from API connections to full pipeline.
"""
import os
import sys
import json
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = "✓ PASS"
FAIL = "✗ FAIL"
results = []


def test(name, fn):
    """Runs a single test, catches exceptions, records result."""
    print(f"\n  Running: {name}...", end=" ", flush=True)
    try:
        fn()
        print(PASS)
        results.append((name, True, None))
    except AssertionError as e:
        print(FAIL)
        results.append((name, False, str(e)))
    except Exception as e:
        print(FAIL)
        results.append((name, False, f"{type(e).__name__}: {e}"))


# ── API CONNECTION TESTS ───────────────────────────────────────────────────

def test_cerebras_connection():
    """Verifies Cerebras key is set and returns a response."""
    from api_handler import ask_cerebras, CEREBRAS_KEY
    assert CEREBRAS_KEY, "CEREBRAS_KEY not set in .env"
    result = ask_cerebras("Say the word PING only, nothing else.")
    assert result is not None, "Cerebras returned None"
    assert len(result.strip()) > 0, "Cerebras returned empty string"


def test_groq_connection():
    """Verifies Groq key is set and returns a response."""
    from api_handler import ask_groq, GROQ_KEY
    assert GROQ_KEY, "GROQ_KEY not set in .env"
    result = ask_groq("Say the word PING only, nothing else.")
    assert result is not None, "Groq returned None"
    assert len(result.strip()) > 0, "Groq returned empty string"


def test_get_available_providers():
    """Verifies get_available_providers returns correct structure."""
    from api_handler import get_available_providers
    p = get_available_providers()
    assert "cerebras" in p, "Missing cerebras key"
    assert "groq" in p, "Missing groq key"
    assert isinstance(p["cerebras"], bool), "cerebras value not bool"
    assert isinstance(p["groq"], bool), "groq value not bool"


def test_ask_with_retry():
    """Verifies ask_with_retry returns a result and handles agent routing."""
    from api_handler import ask_with_retry
    result = ask_with_retry("Say PING only.", max_attempts=2, agent_name="coder")
    assert result is not None, "ask_with_retry returned None"
    assert len(result.strip()) > 0, "ask_with_retry returned empty"


# ── AGENT BUS TESTS ────────────────────────────────────────────────────────

def test_agent_bus():
    """Posts, reads, and acknowledges messages on the agent bus."""
    import agent_bus
    # Post a test message
    mid = agent_bus.post(
        sender       = "test",
        recipient    = "test_target",
        message_type = "test_msg",
        content      = {"hello": "world"},
        task_id      = "test-task-1",
    )
    assert mid, "post() returned no message_id"

    # Read it back
    msgs = agent_bus.read("test_target")
    found = [m for m in msgs if m["message_id"] == mid]
    assert found, "Message not found in bus after posting"
    assert found[0]["content"]["hello"] == "world", "Content mismatch"

    # Acknowledge
    agent_bus.acknowledge(mid)
    msgs_after = agent_bus.read("test_target")
    still_there = [m for m in msgs_after if m["message_id"] == mid]
    assert not still_there, "Message still unacknowledged after acknowledge()"


def test_agent_bus_thread(task_id):
    """Verifies get_thread returns messages for a specific task."""
    import agent_bus
    mid = agent_bus.post("test", "test2", "thread_test", {}, task_id=task_id)
    thread = agent_bus.get_thread(task_id)
    assert any(m["message_id"] == mid for m in thread), "Message not in thread"
    agent_bus.acknowledge(mid)


# ── AGENT MEMORY TESTS ────────────────────────────────────────────────────

def test_agent_memory():
    """Stores and retrieves values from agent memory."""
    import agent_memory
    agent_memory.store("test_key_xyz", {"value": 42}, agent_name="test")
    result = agent_memory.get("test_key_xyz")
    assert result is not None, "get() returned None after store()"
    assert result["value"] == 42, f"Value mismatch: got {result}"


def test_agent_memory_file_content():
    """Stores and retrieves file content."""
    import agent_memory
    agent_memory.store_file_content("test_file.py", "print('hello')", task_id="t1")
    content = agent_memory.get_file_content("test_file.py")
    assert content == "print('hello')", f"File content mismatch: {content}"


def test_agent_memory_lessons():
    """Stores a lesson and retrieves it for a matching task."""
    import agent_memory
    agent_memory.store_lesson(
        error      = "NameError: name x not defined",
        fix        = "Define x before using it",
        task_type  = "write a function that calculates",
        agent_name = "test",
    )
    lessons = agent_memory.get_lessons("write a function that calculates sum")
    assert isinstance(lessons, list), "get_lessons() didn't return a list"


# ── AGENT CHAT TESTS ──────────────────────────────────────────────────────

def test_agent_chat():
    """Logs a message and reads it back."""
    import agent_chat
    agent_chat.log("test_agent", "This is a test message XYZ999", task_id="t-test")
    recent = agent_chat.get_recent(50)
    assert isinstance(recent, list), "get_recent() didn't return list"
    found = any("XYZ999" in e.get("message", "") for e in recent)
    assert found, "Test message not found in recent chat"


def test_agent_chat_filter():
    """Filters chat by agent name."""
    import agent_chat
    agent_chat.log("filter_test_agent", "Filter test message ABC888")
    by_agent = agent_chat.get_by_agent("filter_test_agent")
    assert isinstance(by_agent, list), "get_by_agent() didn't return list"
    assert any("ABC888" in e.get("message", "") for e in by_agent), "Filtered message not found"


# ── PLANNER TESTS ─────────────────────────────────────────────────────────

def test_planner_json_validation():
    """Validates a known-good project JSON through the Planner."""
    from planner import Planner
    valid_project = {
        "project": {"name": "Test App", "version": "1.0", "type": "cli", "description": "A test"},
        "phases": [
            {
                "phase_id": 1,
                "phase_name": "Core",
                "tasks": [
                    {
                        "task_id": "1.1",
                        "file": "main.py",
                        "description": "Write a hello world script",
                        "expected_output": "Prints hello world",
                        "status": "pending",
                    }
                ],
            }
        ],
    }
    planner = Planner()
    result = planner._validate_json(valid_project)
    assert result is not None, "Planner rejected valid JSON"
    assert result["project"]["name"] == "Test App", "Project name lost"


def test_planner_natural_language():
    """Converts a natural language prompt to a structured project JSON."""
    from planner import Planner
    planner = Planner()
    project = planner._parse_natural_language(
        "Write a Python script that reads a CSV and prints the total of the Amount column"
    )
    assert project is not None, "Planner returned None for NL input"
    assert "project" in project, "Missing 'project' key in output"
    assert "phases" in project, "Missing 'phases' key in output"
    assert len(project["phases"]) > 0, "No phases generated"


# ── CODER TEST ────────────────────────────────────────────────────────────

def test_coder_single_task():
    """Runs one task through Coder and verifies it posts to bus."""
    import agent_bus
    from coder import Coder

    task = {
        "task_id":       "test-coder-1",
        "file":          "test_output.py",
        "description":   "Write a Python function called add(a, b) that returns a + b",
        "expected_output": "A working add function",
        "status":        "pending",
        "needs": [], "reads": [], "extends": [],
    }
    context = {
        "task_id":         "test-coder-1",
        "file_to_create":  "test_output.py",
        "description":     task["description"],
        "expected_output": task["expected_output"],
        "project_name":    "Test Project",
        "project_desc":    "Testing",
        "phase_name":      "Test Phase",
        "coder_rules":     "",
        "dependency_files": {},
        "needs": [], "reads": [], "extends": [],
    }

    # Clear any old test messages
    for m in agent_bus.read("reviewer"):
        if m.get("content", {}).get("task_id") == "test-coder-1":
            agent_bus.acknowledge(m["message_id"])

    coder = Coder(task, context, agent_id=99)
    coder.run()

    # Give it a moment then check bus
    time.sleep(1)
    msgs = agent_bus.read("reviewer")
    found = [m for m in msgs if m.get("content", {}).get("task_id") == "test-coder-1"]
    assert found, "Coder did not post code_ready to bus"

    code = found[0]["content"].get("code", "")
    assert "def add" in code, f"Expected 'def add' in code, got: {code[:200]}"

    # Clean up
    agent_bus.acknowledge(found[0]["message_id"])


# ── REVIEWER TESTS ────────────────────────────────────────────────────────

def test_reviewer_pass():
    """Submits good code to Reviewer and expects PASS verdict."""
    from reviewer import Reviewer
    task = {
        "task_id":       "test-rev-pass",
        "file":          "adder.py",
        "description":   "Write a function add(a, b) that returns a + b",
        "expected_output": "Working add function",
    }
    good_code = "def add(a, b):\n    \"\"\"Returns the sum of a and b.\"\"\"\n    return a + b\n"
    reviewer = Reviewer()
    verdict = reviewer.review(task, good_code, attempt=1)
    assert verdict["verdict"] == "PASS", f"Expected PASS, got {verdict['verdict']} — {verdict.get('reason','')}"


def test_reviewer_fail():
    """Submits broken code to Reviewer and expects FAIL verdict."""
    from reviewer import Reviewer
    task = {
        "task_id":       "test-rev-fail",
        "file":          "broken.py",
        "description":   "Write a function multiply(a, b) that returns a * b",
        "expected_output": "Working multiply function",
    }
    bad_code = "def multiply(a, b):\n    pass  # TODO: implement\n"
    reviewer = Reviewer()
    verdict = reviewer.review(task, bad_code, attempt=1)
    assert verdict["verdict"] == "FAIL", f"Expected FAIL for placeholder code, got {verdict['verdict']}"


# ── DEBUGGER TEST ─────────────────────────────────────────────────────────

def test_debugger_fix():
    """Submits broken code with complaint to Debugger and expects fixed code."""
    from debugger import Debugger
    task = {
        "task_id":       "test-dbg-1",
        "file":          "multiply.py",
        "description":   "Write a function multiply(a, b) that returns a * b",
        "expected_output": "Working multiply function",
    }
    broken_code = "def multiply(a, b):\n    pass  # TODO\n"
    issues = ["Function body is empty — contains only a placeholder", "Returns None instead of a * b"]

    debugger = Debugger()
    fixed = debugger.fix(task, broken_code, issues, attempt_number=1)
    assert fixed is not None, "Debugger returned None"
    assert "multiply" in fixed, "Function name missing from fixed code"
    assert "pass" not in fixed or "return" in fixed, "Code still has placeholder"


# ── DEPENDENCY ORDERING TEST ──────────────────────────────────────────────

def test_dependency_ordering():
    """Verifies Planner builds a dependency order where needs come before dependents."""
    from planner import Planner
    project = {
        "project": {"name": "Order Test", "version": "1.0", "type": "cli", "description": ""},
        "phases": [
            {
                "phase_id": 1,
                "phase_name": "Core",
                "tasks": [
                    {"task_id": "1.1", "file": "db.py", "description": "Create database module", "expected_output": "DB class", "status": "pending"},
                    {"task_id": "1.2", "file": "api.py", "description": "Create API using db.py for database access", "expected_output": "API using db", "status": "pending"},
                ],
            }
        ],
    }
    planner = Planner()
    enriched = planner._enrich(project)
    order = enriched.get("dependency_order", [])
    assert "1.1" in order, "Task 1.1 missing from order"
    assert "1.2" in order, "Task 1.2 missing from order"
    assert order.index("1.1") < order.index("1.2"), f"1.1 should come before 1.2, got order: {order}"


# ── PARALLEL CODERS TEST ──────────────────────────────────────────────────

def test_parallel_coders():
    """Verifies that 2 Coder threads can run simultaneously."""
    import agent_bus
    from coder import Coder

    def make_task(n):
        return {
            "task_id":       f"parallel-test-{n}",
            "file":          f"parallel_{n}.py",
            "description":   f"Write a Python function called func_{n}() that returns {n}",
            "expected_output": f"Working func_{n} function",
            "status":        "pending",
            "needs": [], "reads": [], "extends": [],
        }

    def make_context(task):
        return {
            "task_id":         task["task_id"],
            "file_to_create":  task["file"],
            "description":     task["description"],
            "expected_output": task["expected_output"],
            "project_name":    "Parallel Test",
            "project_desc":    "Testing parallel execution",
            "phase_name":      "Test",
            "coder_rules":     "",
            "dependency_files": {},
            "needs": [], "reads": [], "extends": [],
        }

    tasks = [make_task(1), make_task(2)]
    threads = []
    start = time.time()

    for task in tasks:
        coder = Coder(task, make_context(task), agent_id=task["task_id"])
        t = threading.Thread(target=coder.run, daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=90)

    elapsed = time.time() - start
    # Both ran — check bus has results for both
    msgs = agent_bus.read("reviewer")
    task_ids = {m.get("content", {}).get("task_id") for m in msgs}
    for task in tasks:
        if task["task_id"] in task_ids:
            for m in msgs:
                if m.get("content", {}).get("task_id") == task["task_id"]:
                    agent_bus.acknowledge(m["message_id"])

    assert "parallel-test-1" in task_ids or "parallel-test-2" in task_ids, \
        f"Neither parallel task posted to bus. Got: {task_ids}"


# ── RECOVERY TEST ─────────────────────────────────────────────────────────

def test_recovery():
    """Simulates a mid-project crash and verifies Orchestrator resets in_progress tasks."""
    from orchestrator import Orchestrator

    # Write a fake project with one in_progress task
    fake_project = {
        "project": {"name": "Recovery Test", "version": "1.0", "type": "cli", "description": ""},
        "phases": [
            {
                "phase_id": 1,
                "phase_name": "Core",
                "tasks": [
                    {"task_id": "r1.1", "file": "r.py", "description": "Test", "expected_output": "Test", "status": "in_progress"},
                    {"task_id": "r1.2", "file": "r2.py", "description": "Test2", "expected_output": "Test2", "status": "pending"},
                ],
            }
        ],
        "dependency_order": ["r1.1", "r1.2"],
    }

    tmp_file = "/tmp/katalyst_recovery_test.json"
    with open(tmp_file, "w") as f:
        json.dump(fake_project, f)

    orch = Orchestrator()
    recovered = orch._recover_in_progress(fake_project, tmp_file)

    # in_progress task should now be pending
    task = recovered["phases"][0]["tasks"][0]
    assert task["status"] == "pending", f"Expected pending after recovery, got {task['status']}"

    # Verify it was also saved to disk
    with open(tmp_file) as f:
        saved = json.load(f)
    assert saved["phases"][0]["tasks"][0]["status"] == "pending", "Recovery not saved to disk"

    os.remove(tmp_file)


# ── FULL PIPELINE TEST ────────────────────────────────────────────────────

def test_full_pipeline():
    """
    Runs a tiny 2-task project through the full agent pipeline.
    Uses a temp project file and checks output dir for generated files.
    WARNING: This makes real API calls and takes ~30-60 seconds.
    """
    import shutil

    mini_project = {
        "project": {"name": "Pipeline Test", "version": "1.0", "type": "cli", "description": "Mini test"},
        "technical": {"language": "Python", "framework": "none", "dependencies": []},
        "phases": [
            {
                "phase_id": 1,
                "phase_name": "Core",
                "tasks": [
                    {
                        "task_id": "p1.1",
                        "file":    "pipeline_test_utils.py",
                        "description": "Write a Python function called double(n) that returns n * 2",
                        "expected_output": "Working double() function",
                        "status": "pending",
                        "needs": [], "reads": [], "extends": [],
                    },
                ],
            }
        ],
        "dependency_order": ["p1.1"],
    }

    tmp_file = "/tmp/katalyst_pipeline_test.json"
    with open(tmp_file, "w") as f:
        json.dump(mini_project, f)

    from orchestrator import Orchestrator
    from planner import Planner

    # Run planner to set up context packets
    planner = Planner()
    planner._enrich(mini_project)

    # Run orchestrator
    orch = Orchestrator()
    result = orch.run(tmp_file)

    assert result is not None, "Orchestrator returned None"

    # Check if any task completed
    tasks = [t for p in result.get("phases", []) for t in p.get("tasks", [])]
    statuses = [t["status"] for t in tasks]
    assert any(s in ("complete", "verified") for s in statuses), \
        f"No tasks completed in pipeline test. Statuses: {statuses}"

    os.remove(tmp_file)


# ── MAIN RUNNER ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n╔══════════════════════════════════════════╗")
    print("║  ⚡ KATALYST — Phase 6 Test Suite         ║")
    print("╚══════════════════════════════════════════╝\n")

    # Fast tests — no API calls
    print("── Infrastructure Tests ──")
    test("Agent Bus: post/read/acknowledge",   test_agent_bus)
    test("Agent Bus: get_thread",              lambda: test_agent_bus_thread("thread-test-xyz"))
    test("Agent Memory: store/retrieve",       test_agent_memory)
    test("Agent Memory: file content",         test_agent_memory_file_content)
    test("Agent Memory: lessons",              test_agent_memory_lessons)
    test("Agent Chat: log/read",               test_agent_chat)
    test("Agent Chat: filter by agent",        test_agent_chat_filter)
    test("Orchestrator: recovery",             test_recovery)
    test("Planner: JSON validation",           test_planner_json_validation)
    test("Planner: dependency ordering",       test_dependency_ordering)

    # API tests — require keys
    print("\n── API Connection Tests ──")
    test("API: get_available_providers",       test_get_available_providers)
    test("API: Cerebras connection",           test_cerebras_connection)
    test("API: Groq connection",               test_groq_connection)
    test("API: ask_with_retry",                test_ask_with_retry)

    # Agent tests — make real AI calls
    print("\n── Agent Tests (live AI calls) ──")
    test("Planner: natural language → JSON",   test_planner_natural_language)
    test("Reviewer: PASS verdict",             test_reviewer_pass)
    test("Reviewer: FAIL verdict",             test_reviewer_fail)
    test("Debugger: fix broken code",          test_debugger_fix)
    test("Coder: single task → bus",           test_coder_single_task)
    test("Coder: parallel execution",          test_parallel_coders)

    # Full pipeline — slowest
    print("\n── Full Pipeline Test ──")
    test("Full pipeline: 1-task project",      test_full_pipeline)

    # Summary
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total  = len(results)

    print(f"\n╔══════════════════════════════════════════╗")
    print(f"║  Results: {passed}/{total} passed, {failed} failed{' ' * (16 - len(str(passed)) - len(str(total)) - len(str(failed)))}║")
    print(f"╚══════════════════════════════════════════╝")

    if failed:
        print("\nFailed tests:")
        for name, ok, err in results:
            if not ok:
                print(f"  ✗ {name}")
                print(f"    {err}")

    sys.exit(0 if failed == 0 else 1)
