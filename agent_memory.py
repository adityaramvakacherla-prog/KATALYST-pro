"""
agent_memory.py — Shared knowledge base for KATALYST agents.
Stores project context, generated file contents, lessons, and decisions.
All agents read and write here.

FIX 1 (disk read on every call): An in-process dict cache means agents
never hit the disk for reads during a build. Only writes flush to disk.
Cache is invalidated on write so it stays consistent across threads.

FIX 2 (lessons wiped between projects): lessons now live in a SEPARATE
permanent file (memory/lessons.json) that is never cleared when a new
project loads. Project context, file registry, and decisions are reset
per-project; lessons accumulate forever (capped at 500 entries).
"""
import json
import os
import fcntl
import threading
from datetime import datetime

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE  = os.path.join(BASE_DIR, "memory", "agent_memory.json")
LESSONS_FILE = os.path.join(BASE_DIR, "memory", "lessons.json")   # permanent — survives project resets

EMPTY_MEMORY = {
    "project_context": {},
    "file_registry":   {},
    "decisions":       [],
    "store":           {},
}

_cache      = None          # in-process dict — avoids disk reads
_cache_lock = threading.Lock()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_memory():
    """Returns cached memory dict. Loads from disk only on first call or after write."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        if not os.path.exists(MEMORY_FILE):
            _cache = {k: v.copy() if isinstance(v, (dict, list)) else v
                      for k, v in EMPTY_MEMORY.items()}
            return _cache
        try:
            with open(MEMORY_FILE, "r") as f:
                _cache = json.load(f)
            # Ensure all required keys exist
            for k, v in EMPTY_MEMORY.items():
                if k not in _cache:
                    _cache[k] = v.copy() if isinstance(v, (dict, list)) else v
        except Exception:
            _cache = {k: v.copy() if isinstance(v, (dict, list)) else v
                      for k, v in EMPTY_MEMORY.items()}
        return _cache


def _write_memory(data):
    """Writes project memory to disk and updates cache atomically."""
    global _cache
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    with _cache_lock:
        with open(MEMORY_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(data, f, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)
        _cache = data


def _read_lessons():
    """Reads permanent lessons file from disk."""
    if not os.path.exists(LESSONS_FILE):
        return []
    try:
        with open(LESSONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _write_lessons(lessons):
    """Writes permanent lessons file to disk."""
    os.makedirs(os.path.dirname(LESSONS_FILE), exist_ok=True)
    with open(LESSONS_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(lessons, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)


# ── Project context ───────────────────────────────────────────────────────────

def set_project_context(project_dict):
    """Saves full project info. Resets file registry and decisions for the new project."""
    memory = _read_memory()
    memory["project_context"] = project_dict
    memory["file_registry"]   = {}   # clear per-project file cache
    memory["decisions"]       = []   # clear per-project decisions
    _write_memory(memory)


def get_project_context():
    """Returns the stored project context dict."""
    return _read_memory().get("project_context", {})


# ── File registry ─────────────────────────────────────────────────────────────

def store_file_content(filename, content, task_id=None):
    """Registers a generated file so other agents can read its content."""
    memory = _read_memory()
    memory["file_registry"][filename] = {
        "content":  content,
        "task_id":  task_id,
        "saved_at": datetime.now().isoformat(),
    }
    _write_memory(memory)


def get_file_content(filename):
    """Returns stored content of a generated file, or None if not found."""
    registry = _read_memory().get("file_registry", {})
    entry = registry.get(filename)
    return entry["content"] if entry else None


def get_files_list():
    """Returns list of all registered filenames."""
    return list(_read_memory().get("file_registry", {}).keys())


# ── Lessons — PERMANENT across projects ───────────────────────────────────────

def store_lesson(error, fix, task_type, agent_name="unknown"):
    """
    Saves an error→fix lesson permanently.
    Lessons survive project resets and accumulate across all builds.
    Capped at 500 entries (oldest removed first).
    """
    lessons = _read_lessons()
    lessons.append({
        "date":      datetime.now().isoformat(),
        "task_type": task_type,
        "error":     error,
        "fix":       fix,
        "agent":     agent_name,
    })
    lessons = lessons[-500:]   # keep newest 500
    _write_lessons(lessons)


def get_lessons(task_description, limit=5):
    """
    Returns relevant past lessons matching words in the task description.
    Searches ALL historical lessons, not just the current project's.
    """
    lessons  = _read_lessons()
    relevant = []
    task_words = set(task_description.lower().split())
    for lesson in reversed(lessons):   # newest first
        lesson_words = set(lesson.get("task_type", "").lower().split())
        if task_words & lesson_words:  # any word overlap
            relevant.append(
                f"Past error: '{lesson['error'][:120]}' — Fixed by: '{lesson['fix'][:120]}'"
            )
        if len(relevant) >= limit:
            break
    return relevant


# ── Decisions ─────────────────────────────────────────────────────────────────

def save_decision(agent_name, task_id, decision, reason):
    """Logs a routing or escalation decision made by the Orchestrator."""
    memory = _read_memory()
    memory["decisions"].append({
        "timestamp": datetime.now().isoformat(),
        "agent":     agent_name,
        "task_id":   task_id,
        "decision":  decision,
        "reason":    reason,
    })
    _write_memory(memory)


def get_decisions(task_id):
    """Returns all decisions logged for a specific task."""
    return [d for d in _read_memory().get("decisions", [])
            if d.get("task_id") == task_id]


# ── Generic key-value store ───────────────────────────────────────────────────

def store(key, value, agent_name="unknown"):
    """Generic key-value store for any agent to save arbitrary data."""
    memory = _read_memory()
    if "store" not in memory:
        memory["store"] = {}
    memory["store"][key] = {
        "value":      value,
        "agent":      agent_name,
        "updated_at": datetime.now().isoformat(),
    }
    _write_memory(memory)


def get(key):
    """Retrieves a value from the generic key-value store."""
    entry = _read_memory().get("store", {}).get(key)
    return entry["value"] if entry else None


def get_relevant(task_description, limit=5):
    """Alias for get_lessons — used by agents that call this name."""
    return get_lessons(task_description, limit=limit)


def invalidate_cache():
    """Forces next read to reload from disk. Call if memory file edited externally."""
    global _cache
    with _cache_lock:
        _cache = None
