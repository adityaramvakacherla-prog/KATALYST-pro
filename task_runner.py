"""
task_runner.py — KATALYST Task Runner (Phase 3 version)
Thin wrapper — all logic lives in the Orchestrator agent now.
Called by server.py via _katalyst_run.py bootstrap.
"""
import os
import sys
from logger import log

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_project(input_file_path):
    """Loads project and hands off to Orchestrator to run the full agent pipeline."""
    log(f"Task runner started — launching Orchestrator", "START")

    try:
        from orchestrator import Orchestrator
        orchestrator = Orchestrator()
        project = orchestrator.run(input_file_path)
        log("Orchestrator finished", "SUCCESS")
        return project
    except Exception as e:
        log(f"Task runner error: {str(e)[:200]}", "ERROR")
        return None
