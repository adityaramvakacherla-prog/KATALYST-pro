import json
import os
from datetime import datetime

LOG_FILE = "logs/activity.log"
MEMORY_FILE = "memory/knowledge.json"

def log(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{level}] {message}"
    print(log_entry)
    os.makedirs("logs", exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(log_entry + "\n")

def save_to_memory(error, fix, task_type):
    os.makedirs("memory", exist_ok=True)
    memory = []
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            memory = json.load(f)
    memory.append({
        "date": datetime.now().isoformat(),
        "task_type": task_type,
        "error": error,
        "fix": fix
    })
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def get_relevant_memory(task_description):
    if not os.path.exists(MEMORY_FILE):
        return ""
    with open(MEMORY_FILE, "r") as f:
        memory = json.load(f)
    relevant = []
    for lesson in memory[-20:]:
        if any(word in task_description.lower()
               for word in lesson["task_type"].lower().split()):
            relevant.append(f"Past lesson: Error was '{lesson['error'][:100]}' — Fixed by: '{lesson['fix'][:100]}'")
    if relevant:
        return "\n".join(relevant[:3])
    return ""
