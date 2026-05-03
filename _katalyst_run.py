import sys, os
sys.path.insert(0, '/home/aditya/codespace/KATALYST')
os.chdir('/home/aditya/codespace/KATALYST')
from task_runner import run_project
run_project('current_project.json')
