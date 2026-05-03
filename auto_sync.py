"""
KATALYST Auto-Sync
Pushes live data to GitHub every 10 seconds so phone dashboard stays updated
"""
import subprocess
import time
import os
import threading
from logger import log

SYNC_INTERVAL = 10  # seconds

def git_push():
    """Pushes latest logs and progress to GitHub"""
    try:
        # Stage only the safe files — never keys
        subprocess.run(
            ["git", "add",
             "logs/activity.log",
             "logs/live_feed.txt",
             "logs/control.txt",
             "memory/knowledge.json",
             "current_project.json"],
            capture_output=True,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )

        # Check if there's anything new to push
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )

        if not status.stdout.strip():
            return  # Nothing changed, skip push

        # Commit and push
        subprocess.run(
            ["git", "commit", "-m", "katalyst live sync"],
            capture_output=True,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )

        result = subprocess.run(
            ["git", "push", "origin", "main"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )

        if result.returncode == 0:
            print(f"[SYNC] Pushed to GitHub")
        else:
            print(f"[SYNC] Push failed: {result.stderr[:100]}")

    except Exception as e:
        print(f"[SYNC] Error: {e}")

def start_sync():
    """Runs sync loop in background thread"""
    def loop():
        print("[SYNC] Auto-sync started — pushing to GitHub every 10 seconds")
        while True:
            git_push()
            time.sleep(SYNC_INTERVAL)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
