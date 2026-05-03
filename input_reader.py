import json
import os

def load_project(input_file_path):
    """Reads your project JSON file and returns it as a dictionary"""
    with open(input_file_path, "r") as f:
        project = json.load(f)
    print(f"Project loaded: {project['project']['name']}")
    print(f"Total phases: {len(project['phases'])}")
    total_tasks = sum(len(phase['tasks']) for phase in project['phases'])
    print(f"Total tasks: {total_tasks}")
    return project

def get_next_task(project):
    """Finds the next pending task in order"""
    for phase in project['phases']:
        for task in phase['tasks']:
            if task['status'] == 'pending':
                return task, phase
    return None, None

def mark_task_status(project, task_id, status):
    """Updates a task status: pending → in_progress → complete → failed"""
    for phase in project['phases']:
        for task in phase['tasks']:
            if task['task_id'] == task_id:
                task['status'] = status
                return project
    return project

def save_project(project, input_file_path):
    """Saves updated project back to file with new task statuses"""
    with open(input_file_path, "w") as f:
        json.dump(project, f, indent=2)

def load_coder_rules():
    """Loads your CODER_RULES.md file"""
    if os.path.exists("CODER_RULES.md"):
        with open("CODER_RULES.md", "r") as f:
            return f.read()
    return ""

def count_progress(project):
    """Returns (completed, total) task counts"""
    total = 0
    completed = 0
    for phase in project['phases']:
        for task in phase['tasks']:
            total += 1
            if task['status'] in ['complete', 'verified']:
                completed += 1
    return completed, total

