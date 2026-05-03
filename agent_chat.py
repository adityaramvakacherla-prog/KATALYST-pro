"""
agent_chat.py — Visible conversation log for all KATALYST agents.
Every agent logs here. Dashboard reads this to show the live feed.
Stored in memory/agent_chat.log (human readable) and agent_chat.json (structured).
"""
import json
import os
from datetime import datetime

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LOG_FILE   = os.path.join(BASE_DIR, "memory", "agent_chat.log")
JSON_FILE  = os.path.join(BASE_DIR, "memory", "agent_chat.json")

# Color codes per agent — used by dashboard frontend
AGENT_COLORS = {
    "orchestrator": "#9b8dff",   # purple
    "planner":      "#4b9eff",   # blue
    "coder":        "#3dd68c",   # green
    "reviewer":     "#f5a623",   # orange
    "debugger":     "#f05252",   # red
    "validator":    "#e879f9",   # pink-purple
    "tester":       "#22d3ee",   # cyan
    "packager":     "#7ab8ff",   # light blue
    "architect":    "#fb923c",   # amber
    "system":       "#8892a4",   # grey
}


def _ensure_dirs():
    """Makes sure the memory folder exists before writing."""
    os.makedirs(os.path.join(BASE_DIR, "memory"), exist_ok=True)


def log(agent_name, message, message_type="info", task_id=None):
    """Appends a message to both the human-readable log and the structured JSON log."""
    _ensure_dirs()
    timestamp = datetime.now().strftime("%H:%M:%S")
    agent_upper = agent_name.upper()

    # Write to human-readable .log file
    log_line = f"[{timestamp}] [{agent_upper}] {message}"
    if task_id:
        log_line = f"[{timestamp}] [{agent_upper}] [task:{task_id}] {message}"
    with open(LOG_FILE, "a") as f:
        f.write(log_line + "\n")

    # Write structured entry to JSON file
    entry = {
        "timestamp":  timestamp,
        "agent":      agent_name.lower(),
        "agent_upper": agent_upper,
        "message":    message,
        "type":       message_type,
        "task_id":    task_id,
        "color":      AGENT_COLORS.get(agent_name.lower(), "#8892a4"),
    }
    existing = _read_json()
    existing.append(entry)
    # Keep only last 500 entries to avoid file bloat
    existing = existing[-500:]
    with open(JSON_FILE, "w") as f:
        json.dump(existing, f, indent=2)


def _read_json():
    """Reads the structured JSON log file."""
    if not os.path.exists(JSON_FILE):
        return []
    try:
        with open(JSON_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def get_recent(limit=50):
    """Returns the last N structured log entries for the dashboard."""
    entries = _read_json()
    return entries[-limit:]


def get_by_agent(agent_name):
    """Returns all log entries from a specific agent."""
    entries = _read_json()
    return [e for e in entries if e["agent"] == agent_name.lower()]


def get_by_task(task_id):
    """Returns all log entries related to a specific task."""
    entries = _read_json()
    return [e for e in entries if e.get("task_id") == task_id]


def get_all_formatted():
    """Returns the full human-readable log as a single string."""
    if not os.path.exists(LOG_FILE):
        return "No agent activity yet."
    with open(LOG_FILE, "r") as f:
        return f.read()


def clear_log():
    """Wipes both log files — called from danger zone in settings."""
    _ensure_dirs()
    with open(LOG_FILE, "w") as f:
        f.write("")
    with open(JSON_FILE, "w") as f:
        json.dump([], f)
