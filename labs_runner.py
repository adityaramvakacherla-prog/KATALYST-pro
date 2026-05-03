"""
labs_runner.py — KATALYST Labs Execution Backend
Runs Python/HTML/JS/Bash/Streamlit/Flask code from the Labs page.
Registered as a Flask blueprint in server.py.

KEY FEATURES:
- Auto-installs missing pip packages on ModuleNotFoundError — no manual pip needed
- Streamlit and Flask apps launch as background processes and return a preview URL
- HTML/JS rendered in frontend iframe with full keyboard capture support
- JavaScript runs via Node.js if available, falls back to browser-side execution
- Bash execution with live output
- /labs/running endpoint lets frontend check if a long-running app is still alive
- /labs/app_stop kills any running Streamlit/Flask preview process
"""
import os
import re
import sys
import subprocess
import threading
import time
import signal
from flask import Blueprint, request, jsonify, Response

labs = Blueprint("labs", __name__)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR   = os.path.join(BASE_DIR, "output")
LABS_FEED    = os.path.join(BASE_DIR, "logs", "labs_feed.txt")
TEMP_PY      = os.path.join(BASE_DIR, "logs", "labs_temp.py")
TEMP_JS      = os.path.join(BASE_DIR, "logs", "labs_temp.js")
TEMP_SH      = os.path.join(BASE_DIR, "logs", "labs_temp.sh")

# Module-level process trackers
_current_proc    = None   # short-running (python/bash/node)
_app_proc        = None   # long-running (streamlit/flask) preview server
_app_port        = None   # port the preview server is on
_app_url         = None   # full URL for the preview iframe
_proc_lock       = threading.Lock()
_app_lock        = threading.Lock()

# Ports to try for preview servers
PREVIEW_PORTS = [8501, 8502, 8503, 8504, 8600, 8601]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_feed(text, mode="w"):
    """Writes to the labs feed file so SSE stream can pick it up."""
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    with open(LABS_FEED, mode, encoding="utf-8") as f:
        f.write(text)


def _detect_language(filename, code):
    """Detects language from filename extension or code content heuristics."""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext == ".py":            return "python"
        if ext == ".html":          return "html"
        if ext == ".js":            return "javascript"
        if ext in (".sh", ".bash"): return "bash"
    if code:
        stripped = code.strip().lower()
        if stripped.startswith("<!doctype") or stripped.startswith("<html"):
            return "html"
        if stripped.startswith("#!/bin/bash") or stripped.startswith("#!/bin/sh"):
            return "bash"
        if "import streamlit" in stripped or "st.title" in stripped or "st.write" in stripped:
            return "streamlit"
        if "from flask import" in stripped or "flask(__name__)" in stripped:
            return "flask"
        if "def " in code or "import " in code or "print(" in code or "class " in code:
            return "python"
        if "function " in code or "const " in code or "let " in code or "document." in code:
            return "javascript"
    return "python"


def _detect_app_type(code):
    """Checks if Python code is a Streamlit or Flask app that needs a server."""
    low = code.lower()
    if "import streamlit" in low or "from streamlit" in low:
        return "streamlit"
    if ("from flask import" in low or "import flask" in low) and "app.run" in low:
        return "flask"
    if "from fastapi import" in low or "import fastapi" in low:
        return "fastapi"
    return "script"


def _node_available():
    """Checks if Node.js is installed and runnable."""
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def _find_free_port(ports):
    """Returns first port from the list that is not in use."""
    import socket
    for port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                if s.connect_ex(("localhost", port)) != 0:
                    return port
        except Exception:
            pass
    return ports[0]


def _extract_missing_module(stderr):
    """Parses ModuleNotFoundError from stderr and returns module name to install."""
    # "No module named 'requests'" or "No module named 'PIL'"
    m = re.search(r"No module named '([^']+)'", stderr)
    if m:
        name = m.group(1).split(".")[0]  # get top-level package
        # Map import name → pip package name for common mismatches
        pip_name_map = {
            "PIL":        "Pillow",
            "cv2":        "opencv-python",
            "sklearn":    "scikit-learn",
            "bs4":        "beautifulsoup4",
            "yaml":       "pyyaml",
            "dotenv":     "python-dotenv",
            "serial":     "pyserial",
            "wx":         "wxPython",
            "gi":         "PyGObject",
            "Crypto":     "pycryptodome",
            "jwt":        "PyJWT",
            "attr":       "attrs",
            "magic":      "python-magic",
        }
        return pip_name_map.get(name, name)
    return None


def _auto_install(package_name):
    """Installs a package via pip. Returns (success, output)."""
    _write_feed(f"\n[AUTO-INSTALL] pip installing {package_name}...\n", mode="a")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package_name,
         "--break-system-packages", "--quiet"],
        capture_output=True, text=True, timeout=120,
    )
    success = result.returncode == 0
    msg = f"[AUTO-INSTALL] {'✓ installed ' + package_name if success else '✗ failed: ' + result.stderr[:200]}\n"
    _write_feed(msg, mode="a")
    return success, msg


def _run_subprocess(cmd, cwd=None, timeout=60):
    """Runs a subprocess with timeout. Returns (stdout, stderr, exit_code, elapsed_ms)."""
    global _current_proc
    start = time.time()
    with _proc_lock:
        if _current_proc and _current_proc.poll() is None:
            _current_proc.terminate()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd or BASE_DIR,
            )
            _current_proc = proc
        except Exception as e:
            return "", str(e), 1, 0

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        elapsed = round((time.time() - start) * 1000)
        _write_feed(f"\n[LABS] TIMEOUT after {timeout}s\n", mode="a")
        return stdout, f"Timeout after {timeout}s\n" + stderr, -1, elapsed

    elapsed = round((time.time() - start) * 1000)
    return stdout, stderr, proc.returncode, elapsed


def _run_python_with_autoinstall(code_path, max_install_attempts=3):
    """
    Runs a Python file. On ModuleNotFoundError, auto-installs the missing
    package and retries — up to max_install_attempts times.
    Returns (stdout, stderr, exit_code, elapsed_ms).
    """
    for attempt in range(max_install_attempts + 1):
        stdout, stderr, exit_code, elapsed = _run_subprocess(
            [sys.executable, code_path], timeout=60
        )
        if exit_code == 0:
            return stdout, stderr, exit_code, elapsed
        if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
            pkg = _extract_missing_module(stderr)
            if pkg and attempt < max_install_attempts:
                ok, msg = _auto_install(pkg)
                if not ok:
                    return stdout, stderr + "\n" + msg, exit_code, elapsed
                continue  # retry
        break  # non-import error, don't retry
    return stdout, stderr, exit_code, elapsed


def _launch_preview_server(code, app_type, code_path):
    """
    Launches Streamlit or Flask app as a background process.
    Returns (url, error_message).
    """
    global _app_proc, _app_port, _app_url

    # Kill any existing preview server
    with _app_lock:
        if _app_proc and _app_proc.poll() is None:
            _app_proc.terminate()
            time.sleep(0.5)

    port = _find_free_port(PREVIEW_PORTS)

    if app_type == "streamlit":
        # Check streamlit is installed, auto-install if not
        r = subprocess.run([sys.executable, "-m", "streamlit", "--version"],
                           capture_output=True, timeout=5)
        if r.returncode != 0:
            _auto_install("streamlit")

        cmd = [
            sys.executable, "-m", "streamlit", "run", code_path,
            "--server.port", str(port),
            "--server.address", "0.0.0.0",
            "--server.headless", "true",
            "--server.runOnSave", "false",
            "--browser.gatherUsageStats", "false",
        ]
    elif app_type == "flask":
        # Patch the code to use our port
        patched = re.sub(
            r"app\.run\s*\(.*?\)",
            f"app.run(host='0.0.0.0', port={port}, debug=False)",
            code, flags=re.DOTALL
        )
        with open(code_path, "w", encoding="utf-8") as f:
            f.write(patched)
        cmd = [sys.executable, code_path]
    elif app_type == "fastapi":
        # Install uvicorn if needed
        r = subprocess.run([sys.executable, "-m", "uvicorn", "--version"],
                           capture_output=True, timeout=5)
        if r.returncode != 0:
            _auto_install("uvicorn")
        module = os.path.splitext(os.path.basename(code_path))[0]
        cmd = [sys.executable, "-m", "uvicorn", f"{module}:app",
               "--host", "0.0.0.0", "--port", str(port)]
    else:
        return None, "Unknown app type"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(code_path),
        )
        with _app_lock:
            _app_proc = proc
            _app_port = port
            _app_url  = f"http://localhost:{port}"

        # Wait up to 8 seconds for the server to start
        for _ in range(16):
            time.sleep(0.5)
            if proc.poll() is not None:
                err = proc.stderr.read(500).decode("utf-8", errors="replace")
                return None, f"App crashed on startup: {err}"
            import socket
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    if s.connect_ex(("localhost", port)) == 0:
                        return _app_url, None
            except Exception:
                pass

        return _app_url, None  # return anyway — might just be slow

    except Exception as e:
        return None, str(e)


# ── Routes ────────────────────────────────────────────────────────────────────

@labs.route("/labs/run", methods=["POST"])
def labs_run():
    """
    Executes code. Handles: HTML, Python, JavaScript, Bash, Streamlit, Flask.
    Auto-installs missing pip packages on ImportError.
    Returns JSON with output, errors, exit_code, and optionally html_content or app_url.
    """
    body     = request.get_json(force=True) or {}
    code     = body.get("code", "").strip()
    filename = body.get("filename", "")
    language = body.get("language", "").lower() or _detect_language(filename, code)

    if not code:
        return jsonify({"error": "No code provided"}), 400

    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    _write_feed(f"[LABS] Running {language}...\n")

    # ── HTML ──────────────────────────────────────────────────────────────
    if language == "html":
        _write_feed("[LABS] HTML rendered in preview iframe\n", mode="a")
        return jsonify({
            "output": "", "errors": "", "exit_code": 0, "runtime_ms": 0,
            "language": "html", "html_content": code,
        })

    # ── Python ───────────────────────────────────────────────────────────
    if language in ("python", "py"):
        with open(TEMP_PY, "w", encoding="utf-8") as f:
            f.write(code)

        # Check if this is a Streamlit/Flask app that needs a server
        app_type = _detect_app_type(code)
        if app_type in ("streamlit", "flask", "fastapi"):
            _write_feed(f"[LABS] Detected {app_type} app — launching preview server...\n", mode="a")
            url, err = _launch_preview_server(code, app_type, TEMP_PY)
            if err:
                _write_feed(f"[LABS] Launch error: {err}\n", mode="a")
                return jsonify({
                    "output": "", "errors": err, "exit_code": 1,
                    "runtime_ms": 0, "language": "python",
                })
            _write_feed(f"[LABS] {app_type} running at {url}\n", mode="a")
            return jsonify({
                "output": f"{app_type.title()} app running at {url}",
                "errors": "", "exit_code": 0, "runtime_ms": 0,
                "language": "python", "app_url": url, "app_type": app_type,
            })

        # Regular Python script — run with auto-install
        stdout, stderr, exit_code, elapsed = _run_python_with_autoinstall(TEMP_PY)
        if stdout: _write_feed(stdout, mode="a")
        if stderr: _write_feed(f"\n[STDERR]\n{stderr}", mode="a")
        _write_feed(f"\n[LABS] Done — exit:{exit_code} time:{elapsed}ms\n", mode="a")
        return jsonify({
            "output": stdout, "errors": stderr,
            "exit_code": exit_code, "runtime_ms": elapsed, "language": "python",
        })

    # ── Streamlit (explicit language selection) ───────────────────────────
    if language == "streamlit":
        with open(TEMP_PY, "w", encoding="utf-8") as f:
            f.write(code)
        url, err = _launch_preview_server(code, "streamlit", TEMP_PY)
        if err:
            return jsonify({"output": "", "errors": err, "exit_code": 1,
                            "runtime_ms": 0, "language": "streamlit"})
        return jsonify({
            "output": f"Streamlit running at {url}", "errors": "", "exit_code": 0,
            "runtime_ms": 0, "language": "streamlit", "app_url": url, "app_type": "streamlit",
        })

    # ── JavaScript via Node.js ────────────────────────────────────────────
    if language in ("javascript", "js"):
        if _node_available():
            with open(TEMP_JS, "w", encoding="utf-8") as f:
                f.write(code)
            stdout, stderr, exit_code, elapsed = _run_subprocess(["node", TEMP_JS])
            if stdout: _write_feed(stdout, mode="a")
            if stderr: _write_feed(f"\n[STDERR]\n{stderr}", mode="a")
            _write_feed(f"\n[LABS] Done — exit:{exit_code} time:{elapsed}ms\n", mode="a")
            return jsonify({"output": stdout, "errors": stderr,
                            "exit_code": exit_code, "runtime_ms": elapsed, "language": "javascript"})
        else:
            # Wrap JS in an HTML page so it runs in the browser iframe
            html_wrap = f"""<!DOCTYPE html>
<html>
<head><style>
  body{{background:#0e1117;color:#3dd68c;font-family:monospace;padding:16px;margin:0;font-size:13px}}
  .err{{color:#f05252}}.warn{{color:#f5a623}}
</style></head>
<body>
<pre id="out"></pre>
<script>
var _out=document.getElementById('out');
function _print(cls,args){{
  var line=Array.from(args).map(function(a){{try{{return typeof a==='object'?JSON.stringify(a,null,2):String(a);}}catch(e){{return String(a);}}
  }}).join(' ');
  var span=document.createElement('span');
  if(cls)span.className=cls;
  span.textContent=line+'\\n';
  _out.appendChild(span);
}}
var _l=console.log,_e=console.error,_w=console.warn;
console.log=function(){{_print('',arguments);_l.apply(console,arguments);}};
console.error=function(){{_print('err',arguments);_e.apply(console,arguments);}};
console.warn=function(){{_print('warn',arguments);_w.apply(console,arguments);}};
window.onerror=function(msg,src,line){{_print('err',[msg+' (line '+line+')']);return false;}};
try{{{code}}}catch(e){{_print('err',[e.toString()]);}}
</script>
</body></html>"""
            return jsonify({
                "output": "", "errors": "", "exit_code": 0, "runtime_ms": 0,
                "language": "javascript", "html_content": html_wrap,
            })

    # ── Bash ─────────────────────────────────────────────────────────────
    if language in ("bash", "sh"):
        with open(TEMP_SH, "w", encoding="utf-8") as f:
            f.write(code)
        os.chmod(TEMP_SH, 0o755)
        stdout, stderr, exit_code, elapsed = _run_subprocess(["bash", TEMP_SH])
        if stdout: _write_feed(stdout, mode="a")
        if stderr: _write_feed(f"\n[STDERR]\n{stderr}", mode="a")
        _write_feed(f"\n[LABS] Done — exit:{exit_code} time:{elapsed}ms\n", mode="a")
        return jsonify({"output": stdout, "errors": stderr,
                        "exit_code": exit_code, "runtime_ms": elapsed, "language": "bash"})

    # ── Fallback: Python ──────────────────────────────────────────────────
    with open(TEMP_PY, "w", encoding="utf-8") as f:
        f.write(code)
    stdout, stderr, exit_code, elapsed = _run_python_with_autoinstall(TEMP_PY)
    if stdout: _write_feed(stdout, mode="a")
    if stderr: _write_feed(f"\n[STDERR]\n{stderr}", mode="a")
    _write_feed(f"\n[LABS] Done — exit:{exit_code} time:{elapsed}ms\n", mode="a")
    return jsonify({"output": stdout, "errors": stderr,
                    "exit_code": exit_code, "runtime_ms": elapsed, "language": "python"})


@labs.route("/labs/stop", methods=["POST"])
def labs_stop():
    """Kills the currently running short-lived labs process."""
    global _current_proc
    with _proc_lock:
        if _current_proc and _current_proc.poll() is None:
            _current_proc.terminate()
            _write_feed("\n[LABS] Process stopped by user\n", mode="a")
            return jsonify({"stopped": True})
    return jsonify({"stopped": False})


@labs.route("/labs/app_stop", methods=["POST"])
def labs_app_stop():
    """Kills the long-running preview server (Streamlit/Flask)."""
    global _app_proc, _app_port, _app_url
    with _app_lock:
        if _app_proc and _app_proc.poll() is None:
            _app_proc.terminate()
            _app_proc = None
            _app_port = None
            _app_url  = None
            _write_feed("\n[LABS] Preview server stopped\n", mode="a")
            return jsonify({"stopped": True})
    return jsonify({"stopped": False})


@labs.route("/labs/running")
def labs_running():
    """Returns whether a preview server is currently alive and its URL."""
    with _app_lock:
        alive = _app_proc is not None and _app_proc.poll() is None
        return jsonify({"running": alive, "url": _app_url if alive else None,
                        "port": _app_port if alive else None})


@labs.route("/labs/stream")
def labs_stream():
    """SSE endpoint — streams labs_feed.txt content to the browser in real time."""
    def generate():
        last_size  = 0
        idle_count = 0
        while idle_count < 300:
            try:
                if os.path.exists(LABS_FEED):
                    current_size = os.path.getsize(LABS_FEED)
                    if current_size > last_size:
                        with open(LABS_FEED, "r", errors="replace") as f:
                            f.seek(last_size)
                            new_content = f.read()
                        last_size  = current_size
                        idle_count = 0
                        for line in new_content.splitlines():
                            yield f"data: {line}\n\n"
                    else:
                        idle_count += 1
            except Exception:
                pass
            time.sleep(0.1)
        yield "data: [STREAM_END]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@labs.route("/labs/files")
def labs_files():
    """Returns all runnable/previewable files in the output directory."""
    if not os.path.exists(OUTPUT_DIR):
        return jsonify([])
    files = []
    runnable_exts = {".py", ".html", ".js", ".sh", ".bash"}
    for root, dirs, filenames in os.walk(OUTPUT_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in runnable_exts:
                continue
            full_path = os.path.join(root, fname)
            rel_path  = os.path.relpath(full_path, OUTPUT_DIR)
            stat      = os.stat(full_path)
            files.append({
                "name":     rel_path,
                "language": _detect_language(fname, None),
                "size_kb":  round(stat.st_size / 1024, 1),
            })
    return jsonify(files)


@labs.route("/labs/file/<path:filename>")
def labs_file_content(filename):
    """Returns the content of a specific output file for the editor."""
    safe = os.path.realpath(os.path.join(OUTPUT_DIR, filename))
    if not safe.startswith(os.path.realpath(OUTPUT_DIR)):
        return jsonify({"error": "Access denied"}), 403
    if not os.path.exists(safe):
        return jsonify({"error": "File not found"}), 404
    try:
        with open(safe, encoding="utf-8", errors="replace") as f:
            content = f.read()
        language = _detect_language(filename, content)
        return jsonify({"content": content, "language": language, "name": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@labs.route("/labs/preview/<path:filename>")
def labs_preview(filename):
    """Serves an HTML file directly for iframe preview with console injection."""
    safe = os.path.realpath(os.path.join(OUTPUT_DIR, filename))
    if not safe.startswith(os.path.realpath(OUTPUT_DIR)):
        return "Access denied", 403
    if not os.path.exists(safe):
        return "File not found", 404
    if not filename.endswith(".html"):
        return "<p style='font-family:monospace;color:red'>Preview only works for HTML files.</p>", 400
    try:
        with open(safe, encoding="utf-8", errors="replace") as f:
            html = f.read()
        inject = """<script>
(function(){
  var _l=console.log,_e=console.error,_w=console.warn;
  function post(type,args){
    var msg=Array.from(args).map(function(a){try{return JSON.stringify(a);}catch(e){return String(a);}}).join(' ');
    var el=document.getElementById('katalyst-console')||document.createElement('div');
    el.id='katalyst-console';
    el.style.cssText='position:fixed;bottom:0;left:0;right:0;background:#111;color:#3dd68c;font:11px monospace;padding:4px 8px;z-index:99999;max-height:80px;overflow:auto';
    el.textContent='['+type+'] '+msg;
    document.body&&document.body.appendChild(el);
  }
  console.log=function(){post('LOG',arguments);_l.apply(console,arguments);};
  console.error=function(){post('ERR',arguments);_e.apply(console,arguments);};
  console.warn=function(){post('WARN',arguments);_w.apply(console,arguments);};
  window.onerror=function(msg){post('ERR',[msg]);return false;};
})();
</script>"""
        if "<head>" in html:
            html = html.replace("<head>", "<head>" + inject, 1)
        else:
            html = inject + html
        return html, 200, {"Content-Type": "text/html"}
    except Exception as e:
        return f"<p>Error: {e}</p>", 500
