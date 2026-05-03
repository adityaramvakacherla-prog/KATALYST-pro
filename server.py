"""
KATALYST Dashboard Backend v3.1
Run with: python3 server.py
Handles dashboard + spawns task runner directly.
AI providers: Cerebras (primary) + Groq (backup). Gemini removed.
"""
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import psutil, os, json, glob, subprocess, sys, threading
from datetime import datetime

app = Flask(__name__, static_folder=".")
CORS(app)

from labs_runner import labs
app.register_blueprint(labs)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
LOG_FILE      = os.path.join(BASE_DIR, "logs", "activity.log")
LIVE_FEED     = os.path.join(BASE_DIR, "logs", "live_feed.txt")
CONTROL_FILE  = os.path.join(BASE_DIR, "logs", "control.txt")
PROJECT_FILE  = os.path.join(BASE_DIR, "current_project.json")
MEMORY_FILE   = os.path.join(BASE_DIR, "memory", "knowledge.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
OUTPUT_DIR    = os.path.join(BASE_DIR, "output")
ENV_FILE      = os.path.join(BASE_DIR, ".env")

# ── Load .env so keys work without manual export in terminal ──────────────
def _load_env():
    """Reads .env file and sets environment variables."""
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

SYSTEM_CEREBRAS_KEY = os.environ.get("CEREBRAS_KEY", "")
SYSTEM_GROQ_KEY     = os.environ.get("GROQ_KEY", "")
SYSTEM_MISTRAL_KEY  = os.environ.get("MISTRAL_KEY", "")

DEFAULT_SETTINGS = {
    "cerebras_key": "", "groq_key": "", "mistral_key": "",
    "use_global_keys": True,
    "primary_model":  "llama-3.1-8b",
    "backup_model":   "llama-3.3-70b-versatile",
    "max_retries": 3, "poll_interval": 2,
    "mobile_mode": False, "log_level": "INFO",
    "auto_sync": True, "coder_rules": True, "notifications": True, "max_parallel_coders": 3,
}

# Track the subprocess running task_runner
_runner_proc = None
_runner_lock = threading.Lock()


# ── Helpers ────────────────────────────────────────────────────────────────

def read_control():
    """Returns current control state: running, paused, or stop."""
    if not os.path.exists(CONTROL_FILE): return "running"
    with open(CONTROL_FILE) as f: return f.read().strip()

def write_control(state):
    """Writes a control state to disk so task runner can read it."""
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    with open(CONTROL_FILE, "w") as f: f.write(state)

def read_project():
    """Loads the current project JSON from disk."""
    if not os.path.exists(PROJECT_FILE): return None
    try:
        with open(PROJECT_FILE) as f: return json.load(f)
    except: return None

def count_progress(project):
    """Counts completed, total, failed, and active tasks in the project."""
    if not project: return 0, 0, 0, 0
    total = completed = failed = active = 0
    for phase in project.get("phases", []):
        for task in phase.get("tasks", []):
            total += 1
            s = task.get("status", "pending")
            if s in ("complete", "verified"): completed += 1
            elif s == "failed":  failed += 1
            elif s == "in_progress": active += 1
    return completed, total, failed, active

def read_log_lines(n=80):
    """Reads the last n lines from the activity log, parsed into dicts."""
    if not os.path.exists(LOG_FILE): return []
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    result = []
    for line in lines[-n:]:
        line = line.strip()
        if not line: continue
        level = "INFO"
        if "[SUCCESS]" in line: level = "SUCCESS"
        elif "[WARNING]" in line: level = "WARNING"
        elif "[START]"   in line: level = "START"
        elif "[ERROR]"   in line: level = "ERROR"
        try:    ts = line[1:20]; msg = line[line.index("]", 21) + 2:]
        except: ts = "";         msg = line
        result.append({"time": ts[11:], "msg": msg, "level": level})
    return result

def read_live_feed():
    """Reads the live AI output feed file."""
    if not os.path.exists(LIVE_FEED): return ""
    with open(LIVE_FEED, encoding="utf-8", errors="replace") as f: return f.read()

def get_output_files():
    """Returns list of generated files in the output directory."""
    if not os.path.exists(OUTPUT_DIR): return []
    files = []; seen = set()
    for pat in ["**/*.py","**/*.js","**/*.html","**/*.ts","**/*.json","**/*.css","**/*.txt","**/*.md"]:
        for path in glob.glob(os.path.join(OUTPUT_DIR, pat), recursive=True):
            if path in seen: continue
            seen.add(path)
            stat = os.stat(path)
            files.append({
                "name":     os.path.relpath(path, OUTPUT_DIR),
                "size":     f"{stat.st_size / 1024:.1f} KB",
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S"),
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files[:30]

def get_memory_count():
    """Returns how many lessons are stored in AI memory."""
    if not os.path.exists(MEMORY_FILE): return 0
    try:
        with open(MEMORY_FILE) as f: return len(json.load(f))
    except: return 0

def read_settings():
    """Loads settings from disk, merging with defaults for any missing keys."""
    if not os.path.exists(SETTINGS_FILE): return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE) as f: saved = json.load(f)
        merged = DEFAULT_SETTINGS.copy(); merged.update(saved); return merged
    except: return DEFAULT_SETTINGS.copy()

def resolve_keys(settings):
    """Returns (cerebras_key, groq_key) based on global vs user key preference."""
    if settings.get("use_global_keys", True):
        return SYSTEM_CEREBRAS_KEY, SYSTEM_GROQ_KEY
    cerebras = settings.get("cerebras_key", "") or SYSTEM_CEREBRAS_KEY
    groq     = settings.get("groq_key",     "") or SYSTEM_GROQ_KEY
    return cerebras, groq

def call_ai(cerebras_key, groq_key, system_prompt, user_msg, settings):
    """Tries Cerebras first, then Groq. Returns response string. Never raises."""
    full_prompt = f"{system_prompt}\n\nUser: {user_msg}"

    if cerebras_key:
        try:
            from cerebras.cloud.sdk import Cerebras
            client = Cerebras(api_key=cerebras_key)
            resp = client.chat.completions.create(
                model=settings.get("primary_model", "llama-3.1-8b"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=800,
            )
            return resp.choices[0].message.content
        except Exception as e:
            if not groq_key:
                return f"Cerebras error: {e}"

    if groq_key:
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            comp = client.chat.completions.create(
                model=settings.get("backup_model", "llama-3.3-70b-versatile"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=800,
            )
            return comp.choices[0].message.content
        except Exception as e:
            return f"Groq error: {e}"

    return (
        "No API key configured.\n\n"
        "Create a .env file in the KATALYST folder with:\n"
        "  CEREBRAS_KEY=your_key_here\n"
        "  GROQ_KEY=your_key_here\n"
        "Then restart server.py.\n\n"
        "Get free Cerebras key at: cerebras.ai\n"
        "Get free Groq key at: console.groq.com"
    )

def build_chat_context(project):
    """Builds a context string about the current project for the chat assistant."""
    if not project: return "No project loaded."
    completed, total, failed, active = count_progress(project)
    logs = read_log_lines(20)
    log_text = "\n".join(f"  {l['time']} [{l['level']}] {l['msg']}" for l in logs[-8:])
    return (
        f"Project: {project['project']['name']}\n"
        f"Desc: {project['project'].get('description','')}\n"
        f"Progress: {completed}/{total} tasks complete, {failed} failed, {active} active\n"
        f"Recent logs:\n{log_text}"
    )


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "dashboard_home.html")

@app.route("/api/status")
def api_status():
    """Main polling endpoint — returns everything the dashboard needs."""
    global _runner_proc
    project = read_project()
    completed, total, failed, active = count_progress(project)
    pct = round((completed / total * 100) if total else 0)
    settings = read_settings()
    tasks = []
    if project:
        for phase in project.get("phases", []):
            tasks.append({"type": "phase", "name": phase["phase_name"]})
            for t in phase.get("tasks", []):
                tasks.append({
                    "type": "task", "id": t["task_id"],
                    "desc": t["description"], "file": t.get("file", ""),
                    "status": t.get("status", "pending"),
                })
    runner_alive = False
    with _runner_lock:
        if _runner_proc and _runner_proc.poll() is None:
            runner_alive = True
    cerebras_key, groq_key = resolve_keys(settings)
    return jsonify({
        "control": read_control(),
        "cpu": round(psutil.cpu_percent(interval=None)),
        "ram": round(psutil.virtual_memory().percent),
        "completed": completed, "total": total, "failed": failed, "active": active,
        "pct": pct,
        "project": project["project"]["name"] if project else "No project loaded",
        "logs": read_log_lines(60),
        "live_feed": read_live_feed(),
        "output": get_output_files(),
        "memory": get_memory_count(),
        "tasks": tasks,
        "mobile_mode": settings.get("mobile_mode", False),
        "poll_interval": settings.get("poll_interval", 2),
        "runner_alive": runner_alive,
        "keys_ok": bool(cerebras_key or groq_key),
    })

@app.route("/api/key_check")
def api_key_check():
    """Lets the frontend verify which keys are actually available."""
    settings = read_settings()
    cerebras_key, groq_key = resolve_keys(settings)
    return jsonify({
        "cerebras":       bool(cerebras_key),
        "groq":           bool(groq_key),
        "mistral":        bool(SYSTEM_MISTRAL_KEY),
        "use_global":     settings.get("use_global_keys", True),
        "system_cerebras": bool(SYSTEM_CEREBRAS_KEY),
        "system_groq":     bool(SYSTEM_GROQ_KEY),
    })

@app.route("/api/pause", methods=["POST"])
def api_pause():
    """Toggles between paused and running state."""
    state = read_control()
    new_state = "running" if state == "paused" else "paused"
    write_control(new_state)
    return jsonify({"state": new_state})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Emergency stop — kills the runner process immediately."""
    global _runner_proc
    write_control("stop")
    with _runner_lock:
        if _runner_proc and _runner_proc.poll() is None:
            _runner_proc.terminate()
    return jsonify({"state": "stop"})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Resets control state back to running."""
    write_control("running")
    return jsonify({"state": "running"})

@app.route("/api/start", methods=["POST"])
def api_start():
    """Spawns task_runner.py as a child process to build the loaded project.
    Phase 6: If project has in_progress tasks, resumes from where it left off.
    """
    global _runner_proc
    if not os.path.exists(PROJECT_FILE):
        return jsonify({"error": "No project loaded — upload a JSON first"}), 400

    # Phase 6: Detect resume vs fresh start
    resume_mode = False
    try:
        with open(PROJECT_FILE) as f:
            proj = json.load(f)
        in_progress = [
            t for p in proj.get("phases", [])
            for t in p.get("tasks", [])
            if t.get("status") == "in_progress"
        ]
        completed = [
            t for p in proj.get("phases", [])
            for t in p.get("tasks", [])
            if t.get("status") in ("complete", "verified")
        ]
        if in_progress or completed:
            resume_mode = True
    except Exception:
        pass

    with _runner_lock:
        if _runner_proc and _runner_proc.poll() is None:
            _runner_proc.terminate()

        write_control("running")

        bootstrap = os.path.join(BASE_DIR, "_katalyst_run.py")
        with open(bootstrap, "w") as f:
            f.write(
                f"import sys, os\n"
                f"sys.path.insert(0, {repr(BASE_DIR)})\n"
                f"os.chdir({repr(BASE_DIR)})\n"
                f"from task_runner import run_project\n"
                f"run_project('current_project.json')\n"
            )
        _runner_proc = subprocess.Popen(
            [sys.executable, bootstrap],
            cwd=BASE_DIR
        )

    return jsonify({"ok": True, "resume": resume_mode})

@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Accepts a project JSON file upload and saves it as current_project.json."""
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    try:
        data = json.load(f)
        if "project" not in data or "phases" not in data:
            return jsonify({"error": "Invalid format — needs 'project' and 'phases' keys"}), 400
        with open(PROJECT_FILE, "w") as out:
            json.dump(data, out, indent=2)
        total = sum(len(p.get("tasks", [])) for p in data["phases"])
        return jsonify({
            "ok": True, "name": data["project"]["name"],
            "phases": len(data["phases"]), "tasks": total,
        })
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Invalid JSON: {e}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/download/<path:filename>")
def api_download(filename):
    """Downloads a file from the output directory."""
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

@app.route("/api/file_content/<path:filename>")
def api_file_content(filename):
    """Returns the text content of an output file for the in-dashboard viewer."""
    safe = os.path.realpath(os.path.join(OUTPUT_DIR, filename))
    if not safe.startswith(os.path.realpath(OUTPUT_DIR)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(safe):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(safe, encoding="utf-8", errors="replace") as f:
            return jsonify({"content": f.read(), "name": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Returns current settings, masking key values for security."""
    s = read_settings(); safe = s.copy()
    for key in ("cerebras_key", "groq_key"):
        val = safe.get(key, "")
        safe[key] = (val[:4] + "…" + val[-3:]) if len(val) > 7 else ("set" if val else "")
    safe["system_cerebras_available"] = bool(SYSTEM_CEREBRAS_KEY)
    safe["system_groq_available"]     = bool(SYSTEM_GROQ_KEY)
    return jsonify(safe)

@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    """Saves updated settings to disk."""
    data = request.get_json(force=True)
    if not data: return jsonify({"error": "No data"}), 400
    current = read_settings()
    for key, value in data.items():
        if key in DEFAULT_SETTINGS:
            if key in ("cerebras_key", "groq_key") and not value: continue
            current[key] = value
    with open(SETTINGS_FILE, "w") as f: json.dump(current, f, indent=2)
    return jsonify({"ok": True})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Chat endpoint — AI assistant with project context."""
    body     = request.get_json(force=True)
    user_msg = (body or {}).get("message", "").strip()
    if not user_msg: return jsonify({"error": "Empty message"}), 400
    settings = read_settings()
    cerebras_key, groq_key = resolve_keys(settings)
    system_prompt = (
        "You are KATALYST Assistant — an AI inside the KATALYST build system dashboard. "
        "Help the user understand their project, debug errors, and answer coding questions. "
        "Be concise and direct.\n\n"
        f"=== PROJECT CONTEXT ===\n{build_chat_context(read_project())}\n==="
    )
    return jsonify({"reply": call_ai(cerebras_key, groq_key, system_prompt, user_msg, settings)})

@app.route("/api/labs/run", methods=["POST"])
def api_labs_run():
    """Labs quick AI tool. Modes: code, explain, debug, convert, refactor."""
    body     = request.get_json(force=True)
    prompt   = (body or {}).get("prompt", "").strip()
    mode     = (body or {}).get("mode", "code")
    language = (body or {}).get("language", "Python")
    if not prompt: return jsonify({"error": "Empty prompt"}), 400
    settings = read_settings()
    cerebras_key, groq_key = resolve_keys(settings)
    instructions = {
        "code":     f"Write complete working {language} code. No markdown fences, no explanations, code only.",
        "explain":  "Explain the following code clearly. Describe what each part does in plain language.",
        "debug":    "Debug the following code. List every bug with a clear explanation, then show the complete corrected code.",
        "convert":  f"Convert the following code to {language}. Return only the converted code, no markdown fences.",
        "refactor": f"Refactor the following {language} code for clarity and efficiency. Return only refactored code, no markdown fences.",
    }
    system_prompt = "You are an expert software engineer. " + instructions.get(mode, instructions["code"])
    result = call_ai(cerebras_key, groq_key, system_prompt, prompt, settings)
    return jsonify({"result": result, "mode": mode})

@app.route("/api/generate_code", methods=["POST"])
def api_generate_code():
    """
    Code generation through the FULL agent pipeline:
    Coder → Reviewer → Debugger (up to 3 fix attempts).
    Every quick-generate goes through QA just like a full project build.
    Returns the reviewed+fixed code plus pipeline metadata.
    """
    body     = request.get_json(force=True) or {}
    prompt   = body.get("prompt", "").strip()
    language = body.get("language", "Python")
    if not prompt:
        return jsonify({"error": "Empty prompt"}), 400
    try:
        sys.path.insert(0, BASE_DIR)
        from agent_pipeline import run_single_file_pipeline
        result = run_single_file_pipeline(prompt, language)
        code = result.get("code", "")
        if not code:
            return jsonify({"error": "All agents returned empty — check your API keys"}), 500
        import re
        code = re.sub(r"^```[a-zA-Z]*\n?", "", code.strip())
        code = re.sub(r"\n?```$", "", code.strip())
        return jsonify({
            "code":     code.strip(),
            "language": language,
            "score":    result.get("score", 0),
            "attempts": result.get("attempts", 0),
            "passed":   result.get("passed", False),
        })
    except Exception as e:
        import traceback
        print(f"[generate_code ERROR] {traceback.format_exc()}")
        return jsonify({"error": f"Pipeline error: {str(e)[:200]}"}), 500

@app.route("/api/project_info")
def api_project_info():
    """Returns a summary of the currently loaded project."""
    project = read_project()
    if not project: return jsonify({"loaded": False})
    completed, total, failed, active = count_progress(project)
    return jsonify({
        "loaded": True, "name": project["project"]["name"],
        "type": project["project"].get("type", ""),
        "desc": project["project"].get("description", ""),
        "phases": len(project.get("phases", [])),
        "total": total, "completed": completed, "failed": failed, "active": active,
    })

@app.route("/api/clear_logs", methods=["POST"])
def api_clear_logs():
    """Wipes the activity log file."""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "w") as f: f.write("")
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/clear_memory", methods=["POST"])
def api_clear_memory():
    """Clears all saved AI memory lessons."""
    try:
        os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
        with open(MEMORY_FILE, "w") as f: json.dump([], f)
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/clear_output", methods=["POST"])
def api_clear_output():
    """Deletes all generated files in the output directory."""
    try:
        import shutil
        if os.path.exists(OUTPUT_DIR):
            shutil.rmtree(OUTPUT_DIR)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/unload_project", methods=["POST"])
def api_unload_project():
    """Removes current_project.json and flushes all agent memory for a clean slate."""
    try:
        if os.path.exists(PROJECT_FILE):
            os.remove(PROJECT_FILE)
        bus_file  = os.path.join(BASE_DIR, "memory", "agent_bus.json")
        chat_json = os.path.join(BASE_DIR, "memory", "agent_chat.json")
        chat_log  = os.path.join(BASE_DIR, "memory", "agent_chat.log")
        mem_file  = os.path.join(BASE_DIR, "memory", "agent_memory.json")
        if os.path.exists(bus_file):
            with open(bus_file,  "w") as f: json.dump([], f)
        if os.path.exists(chat_json):
            with open(chat_json, "w") as f: json.dump([], f)
        if os.path.exists(chat_log):
            with open(chat_log,  "w") as f: f.write("")
        if os.path.exists(mem_file):
            with open(mem_file,  "w") as f: json.dump({}, f)
        return jsonify({"ok": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/health")
def api_health():
    """Returns provider availability and system status."""
    settings = read_settings()
    cerebras_key, groq_key = resolve_keys(settings)
    return jsonify({
        "cerebras": bool(cerebras_key),
        "groq":     bool(groq_key),
        "cpu":      round(psutil.cpu_percent(interval=None)),
        "ram":      round(psutil.virtual_memory().percent),
    })

@app.route("/api/agents/provider_status")
def api_agents_provider_status():
    """Returns which provider (primary or fallback) each agent used on its last call."""
    try:
        from api_handler import get_agent_provider_status
        return jsonify(get_agent_provider_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── PHASE 7 — PACKAGER ENDPOINTS ──────────────────────────────────────────

_packager_thread = None

@app.route("/api/package", methods=["POST"])
def api_package():
    """Starts packaging the output folder. Body: { target: 'docker'|'exe'|'apk'|'zip' }"""
    global _packager_thread
    body   = request.get_json(force=True) or {}
    target = body.get("target", "zip").lower()

    if target not in ("docker", "exe", "apk", "zip"):
        return jsonify({"error": f"Unknown target: {target}. Use docker, exe, apk, or zip."}), 400

    output_dir = os.path.join(BASE_DIR, "output")
    if not os.path.exists(output_dir) or not os.listdir(output_dir):
        return jsonify({"error": "Output folder is empty — build a project first"}), 400

    # Kill any existing packager run
    if _packager_thread and _packager_thread.is_alive():
        return jsonify({"error": "Packager is already running — wait for it to finish"}), 400

    try:
        sys.path.insert(0, BASE_DIR)
        from packager import Packager, _set_status
        _set_status("Starting...", 0)

        def run_packager():
            p = Packager()
            p.package(target)

        _packager_thread = threading.Thread(target=run_packager, daemon=True)
        _packager_thread.start()

        return jsonify({"ok": True, "target": target, "message": f"Packaging as {target} started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/package/status")
def api_package_status():
    """Returns current packaging progress."""
    try:
        from packager import get_status
        status = get_status()
        # If there's an output file, add a download URL
        if status.get("output_file"):
            fname = os.path.basename(status["output_file"])
            status["download_url"] = f"/api/package/download/{fname}"
        return jsonify(status)
    except Exception as e:
        return jsonify({"running": False, "done": False, "step": "Packager not loaded", "progress": 0})


@app.route("/api/package/download/<filename>")
def api_package_download(filename):
    """Downloads a packaged output file."""
    packages_dir = os.path.join(BASE_DIR, "packages")
    safe = os.path.realpath(os.path.join(packages_dir, filename))
    if not safe.startswith(os.path.realpath(packages_dir)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(safe):
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(packages_dir, filename, as_attachment=True)


@app.route("/api/package/detect")
def api_package_detect():
    """Detects the app type of the current output folder."""
    try:
        from packager import Packager
        p        = Packager()
        app_type = p.detect_app_type()
        return jsonify({"app_type": app_type})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/agent_bus")
def api_agent_bus():
    """Returns current agent bus contents for dashboard display."""
    try:
        import agent_bus
        return jsonify(agent_bus.read_all())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agent_chat")
def api_agent_chat():
    """Returns recent agent chat messages, optionally filtered by agent name."""
    try:
        import agent_chat
        agent_filter = request.args.get("agent")
        if agent_filter:
            return jsonify(agent_chat.get_by_agent(agent_filter))
        return jsonify(agent_chat.get_recent(100))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agent_bus/inject", methods=["POST"])
def api_agent_bus_inject():
    """Manually injects a message into the agent bus — for Orchestrator control panel."""
    try:
        import agent_bus
        body = request.get_json(force=True)
        message_id = agent_bus.post(
            sender       = body.get("from_agent", "dashboard"),
            recipient    = body.get("to_agent", "orchestrator"),
            message_type = body.get("type", "manual"),
            content      = body.get("content", {}),
            task_id      = body.get("task_id"),
        )
        return jsonify({"ok": True, "message_id": message_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/labs")
def labs_page():
    """Serves the Labs page."""
    return send_from_directory(".", "dashboard_home.html")
# ── Startup ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import shutil

    os.makedirs(os.path.join(BASE_DIR, "logs"),   exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "memory"), exist_ok=True)

    # ── Clear output folder on every server start so old files never linger ──
    _out = os.path.join(BASE_DIR, "output")
    if os.path.exists(_out):
        shutil.rmtree(_out)
    os.makedirs(_out, exist_ok=True)
    print("  [✓] Output folder cleared on startup")

    # ── Clear live feed so dashboard doesn't show stale AI output ────────────
    _feed = os.path.join(BASE_DIR, "logs", "live_feed.txt")
    try:
        with open(_feed, "w") as _f:
            _f.write("")
    except Exception:
        pass

    import socket
    try:    local_ip = socket.gethostbyname(socket.gethostname())
    except: local_ip = "unknown"

    print("\n╔══════════════════════════════════════╗")
    print("║  ⚡ KATALYST Server v3.2              ║")
    print("╚══════════════════════════════════════╝\n")
    print(f"  Local  : http://localhost:5000")
    print(f"  Network: http://{local_ip}:5000")
    print(f"\n  CEREBRAS_KEY : {'✓ loaded' if SYSTEM_CEREBRAS_KEY else '✗ not set'}")
    print(f"  GROQ_KEY     : {'✓ loaded' if SYSTEM_GROQ_KEY     else '✗ not set'}")
    if not SYSTEM_CEREBRAS_KEY and not SYSTEM_GROQ_KEY:
        print(f"\n  ⚠  No keys found! Create a .env file here:")
        print(f"     CEREBRAS_KEY=your_key_here")
        print(f"     GROQ_KEY=gsk_...")
        print(f"  Then restart this server.\n")


# ── PHASE 5 ENDPOINTS ──────────────────────────────────────────────────────

@app.route("/api/agents/status")
def api_agents_status():
    """Returns status of all 5 agents based on agent_bus and agent_chat state."""
    try:
        import agent_bus, agent_chat
        messages   = agent_bus.read_all()
        recent     = agent_chat.get_recent(200)
        agents     = ["orchestrator", "planner", "coder", "reviewer", "debugger", "tester", "validator"]
        status_map = {}
        for name in agents:
            pending = [m for m in messages if m.get("to_agent","").startswith(name) and not m.get("acknowledged")]
            last_msg = next((e for e in reversed(recent) if e.get("agent","").startswith(name)), None)
            status_map[name] = {
                "name":        name,
                "pending_msgs": len(pending),
                "last_message": last_msg.get("message","") if last_msg else "",
                "last_time":    last_msg.get("timestamp","") if last_msg else "",
                "active":       len(pending) > 0,
            }
        return jsonify(status_map)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agents/chat")
def api_agents_chat():
    """Returns recent agent chat entries, optionally filtered by agent name."""
    try:
        import agent_chat
        agent_filter = request.args.get("agent")
        limit        = int(request.args.get("limit", 150))
        if agent_filter:
            entries = agent_chat.get_by_agent(agent_filter)
        else:
            entries = agent_chat.get_recent(limit)
        return jsonify(entries)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agents/bus")
def api_agents_bus():
    """Returns agent bus summary — counts per agent."""
    try:
        import agent_bus
        return jsonify(agent_bus.get_bus_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agents/control", methods=["POST"])
def api_agents_control():
    """Pause or resume a specific agent via control signals."""
    try:
        body   = request.get_json(force=True) or {}
        action = body.get("action", "")
        agent  = body.get("agent", "")
        import agent_chat
        agent_chat.log("system", f"Dashboard control: {action} → {agent}")
        return jsonify({"ok": True, "action": action, "agent": agent})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/convert_prompt", methods=["POST"])
def api_convert_prompt():
    """Converts a natural language prompt to a project JSON plan preview via Planner."""
    body   = request.get_json(force=True) or {}
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Empty prompt"}), 400
    try:
        import sys, traceback
        sys.path.insert(0, BASE_DIR)
        from planner import Planner
        planner = Planner()
        project = planner.run(prompt)
        if not project:
            return jsonify({"error": "Planner could not generate a plan. Check your API keys."}), 500
        total  = sum(len(p.get("tasks", [])) for p in project.get("phases", []))
        phases = len(project.get("phases", []))
        return jsonify({
            "ok":      True,
            "project": project,
            "summary": {
                "name":   project.get("project", {}).get("name", ""),
                "phases": phases,
                "tasks":  total,
                "desc":   project.get("project", {}).get("description", ""),
            }
        })
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        print(f"[convert_prompt ERROR] {err_detail}")
        return jsonify({"error": f"Server error: {str(e)[:200]}. Check API keys are set in .env"}), 500

@app.route("/api/start_prompt", methods=["POST"])
def api_start_prompt():
    """Accepts a natural language prompt, converts via Planner, saves as project, starts build."""
    global _runner_proc
    body   = request.get_json(force=True) or {}
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "Empty prompt"}), 400
    try:
        import sys
        sys.path.insert(0, BASE_DIR)
        from planner import Planner
        planner = Planner()
        project = planner.run(prompt)
        if not project:
            return jsonify({"error": "Planner failed to generate plan"}), 500
        with open(PROJECT_FILE, "w") as f:
            json.dump(project, f, indent=2)
        # Flush stale agent state from any previous project
        for _flush_file, _empty in [
            (os.path.join(BASE_DIR, "memory", "agent_bus.json"),    []),
            (os.path.join(BASE_DIR, "memory", "agent_chat.json"),   []),
            (os.path.join(BASE_DIR, "memory", "agent_memory.json"), {}),
        ]:
            try:
                os.makedirs(os.path.dirname(_flush_file), exist_ok=True)
                with open(_flush_file, "w") as f: json.dump(_empty, f)
            except Exception:
                pass
        # Now start the runner
        with _runner_lock:
            if _runner_proc and _runner_proc.poll() is None:
                _runner_proc.terminate()
            write_control("running")
            bootstrap = os.path.join(BASE_DIR, "_katalyst_run.py")
            with open(bootstrap, "w") as f2:
                f2.write(
                    f"import sys, os\n"
                    f"sys.path.insert(0, {repr(BASE_DIR)})\n"
                    f"os.chdir({repr(BASE_DIR)})\n"
                    f"from task_runner import run_project\n"
                    f"run_project('current_project.json')\n"
                )
            _runner_proc = subprocess.Popen([sys.executable, bootstrap], cwd=BASE_DIR)
        total = sum(len(p.get("tasks",[])) for p in project.get("phases",[]))
        return jsonify({"ok": True, "name": project["project"]["name"], "tasks": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agents/stats")
def api_agents_stats():
    """Returns per-agent performance stats derived from agent_chat and agent_bus."""
    try:
        import agent_chat, agent_bus
        entries  = agent_chat.get_recent(500)
        messages = agent_bus.read_all()
        agents   = ["orchestrator", "planner", "coder", "reviewer", "debugger", "tester", "validator"]
        stats    = {}
        for name in agents:
            agent_entries = [e for e in entries if e.get("agent","").startswith(name)]
            # Count tasks handled (look for task_id references)
            task_ids = set(e.get("task_id") for e in agent_entries if e.get("task_id"))
            # Reviewer pass rate
            passes = len([e for e in agent_entries if "PASS" in e.get("message","")])
            fails  = len([e for e in agent_entries if "FAIL" in e.get("message","")])
            total_reviews = passes + fails
            pass_rate = round(passes / total_reviews * 100) if total_reviews else 0
            # Errors
            errors = len([e for e in agent_entries if e.get("type") == "error" or "error" in e.get("message","").lower()])
            # Messages sent (from bus)
            sent = len([m for m in messages if m.get("from_agent","").startswith(name)])
            stats[name] = {
                "tasks_handled": len(task_ids),
                "pass_rate":     pass_rate if name in ("reviewer", "tester") else None,
                "total_messages": len(agent_entries),
                "bus_messages_sent": sent,
                "errors": errors,
            }
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agents/pause/<agent_name>", methods=["POST"])
def api_agents_pause(agent_name):
    """Pause or resume a specific agent by writing a signal to agent_bus."""
    try:
        import agent_bus, agent_chat
        body   = request.get_json(force=True) or {}
        action = body.get("action", "pause")
        agent_bus.post(
            sender       = "dashboard",
            recipient    = agent_name,
            message_type = f"control_{action}",
            content      = {"action": action},
        )
        agent_chat.log("system", f"Dashboard sent {action} signal to {agent_name}")
        return jsonify({"ok": True, "agent": agent_name, "action": action})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/agents/rerun/<task_id>", methods=["POST"])
def api_agents_rerun(task_id):
    """Re-queues a specific task by resetting its status and injecting a bus message."""
    try:
        import agent_bus, agent_chat
        # Reset task status in project JSON
        project = read_project()
        if project:
            for phase in project.get("phases", []):
                for task in phase.get("tasks", []):
                    if task["task_id"] == task_id:
                        task["status"] = "pending"
            with open(PROJECT_FILE, "w") as f:
                json.dump(project, f, indent=2)
        # Signal orchestrator
        agent_bus.post(
            sender       = "dashboard",
            recipient    = "orchestrator",
            message_type = "rerun_task",
            content      = {"task_id": task_id},
            task_id      = task_id,
        )
        agent_chat.log("system", f"Dashboard requested rerun of task {task_id}")
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
