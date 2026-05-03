"""
agent_bus.py — Message queue for KATALYST agent communication.
All agents post and read messages here. No direct agent-to-agent calls.
Stored in memory/agent_bus.json.

FIXES (cumulative):
- Race condition eliminated — a single threading.Lock wraps the entire
  read-modify-write cycle so two threads can never overwrite each other.
- Code stored by reference not inline: Coder/Debugger/Reviewer pass code
  as a key into a file cache instead of embedding the full string in JSON.
  This keeps agent_bus.json tiny across long projects (was growing to
  100K+ on a 20-task build causing slow dashboard polls).
  The post() function automatically offloads any content "code" field
  >500 chars and replaces it with "code_key". Consumers call
  resolve_code(content) to get the string back before using it.
- clear_old() now called by Orchestrator at project start so acknowledged
  messages don't pile up across runs. Also cleans orphaned cache files.
"""
import json
import os
import uuid
import fcntl
import threading
from datetime import datetime, timezone

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
BUS_FILE  = os.path.join(BASE_DIR, "memory", "agent_bus.json")
CODE_DIR  = os.path.join(BASE_DIR, "memory", "code_cache")
_BUS_LOCK = threading.Lock()   # process-level lock — covers full read-modify-write


# ── Internal file I/O ─────────────────────────────────────────────────────────

def _read_raw():
    """Reads bus file — caller must hold _BUS_LOCK."""
    if not os.path.exists(BUS_FILE):
        return []
    try:
        with open(BUS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _write_raw(messages):
    """Writes bus file — caller must hold _BUS_LOCK."""
    os.makedirs(os.path.dirname(BUS_FILE), exist_ok=True)
    with open(BUS_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump(messages, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)


# ── Code cache — keeps large strings off the bus JSON ────────────────────────

def _store_code(code_str, task_id):
    """
    Writes code string to memory/code_cache/ and returns (key, path).
    Key format: code_cache:<task_id>:<8-char hex>
    """
    os.makedirs(CODE_DIR, exist_ok=True)
    key  = f"code_cache:{task_id}:{uuid.uuid4().hex[:8]}"
    path = os.path.join(CODE_DIR, key.replace(":", "_") + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code_str)
    return key, path


def _load_code(code_key):
    """Reads code from cache file. Returns empty string if file missing."""
    filename = code_key.replace(":", "_") + ".txt"
    path     = os.path.join(CODE_DIR, filename)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def resolve_code(content):
    """
    Given a bus message content dict, returns the code string.
    If content has "code_key", loads from file cache (new path).
    If content has "code" inline (legacy messages), returns it directly.
    Always safe to call — returns "" if neither key is present.
    """
    if "code_key" in content:
        return _load_code(content["code_key"])
    return content.get("code", "")


# ── Public API ────────────────────────────────────────────────────────────────

def post(sender, recipient, message_type, content, task_id=None):
    """
    Posts a new message to the bus atomically. Returns the generated message_id.

    Code offloading: if content["code"] is longer than 500 chars it is written
    to memory/code_cache/ and replaced with a "code_key" reference string.
    This keeps bus JSON small — important for dashboard poll performance.
    """
    content = dict(content)   # don't mutate the caller's dict

    # Offload large code strings to file cache
    if "code" in content and isinstance(content["code"], str) and len(content["code"]) > 500:
        tid_for_key         = task_id or content.get("task_id", "unknown")
        key, _path          = _store_code(content["code"], tid_for_key)
        del content["code"]
        content["code_key"] = key

    message = {
        "message_id":   str(uuid.uuid4()),
        "from_agent":   sender,
        "to_agent":     recipient,
        "task_id":      task_id,
        "type":         message_type,
        "content":      content,
        "status":       "pending",
        "acknowledged": False,
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }
    with _BUS_LOCK:
        messages = _read_raw()
        messages.append(message)
        _write_raw(messages)
    return message["message_id"]


def read(recipient):
    """Returns all unacknowledged messages addressed to this agent."""
    with _BUS_LOCK:
        messages = _read_raw()
    return [m for m in messages if m["to_agent"] == recipient and not m["acknowledged"]]


def acknowledge(message_id):
    """Marks a message as acknowledged atomically so it won't be re-read."""
    with _BUS_LOCK:
        messages = _read_raw()
        for m in messages:
            if m["message_id"] == message_id:
                m["acknowledged"] = True
                m["status"] = "done"
        _write_raw(messages)


def read_all():
    """Returns the full bus contents — used by dashboard to display state."""
    with _BUS_LOCK:
        return _read_raw()


def get_thread(task_id):
    """Returns all messages for a specific task in chronological order."""
    with _BUS_LOCK:
        messages = _read_raw()
    return [m for m in messages if m.get("task_id") == task_id]


def clear_old(hours=24):
    """
    Removes acknowledged messages older than the given number of hours.
    Also deletes orphaned code cache files for removed messages.
    Called by Orchestrator.run() at the start of every project run.
    """
    cutoff     = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    orphan_keys = []

    with _BUS_LOCK:
        messages = _read_raw()
        kept = []
        for m in messages:
            if not m["acknowledged"]:
                kept.append(m)
                continue
            try:
                ts = datetime.fromisoformat(m["timestamp"]).timestamp()
                if ts > cutoff:
                    kept.append(m)
                else:
                    ck = m.get("content", {}).get("code_key")
                    if ck:
                        orphan_keys.append(ck)
            except Exception:
                kept.append(m)
        _write_raw(kept)

    # Clean up orphaned code cache files outside the lock
    for key in orphan_keys:
        try:
            path = os.path.join(CODE_DIR, key.replace(":", "_") + ".txt")
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def get_bus_status():
    """Returns summary counts per agent and status — for dashboard display."""
    with _BUS_LOCK:
        messages = _read_raw()
    summary = {}
    for m in messages:
        agent  = m["to_agent"]
        status = "pending" if not m["acknowledged"] else "done"
        if agent not in summary:
            summary[agent] = {"pending": 0, "done": 0}
        summary[agent][status] += 1
    return summary
