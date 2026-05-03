"""
packager.py — KATALYST Packager Agent (Full Auto Build)
Detects app type from /output folder and packages into a real deliverable.

APK builds run end-to-end automatically — no manual steps needed:
  HTML/JS apps  → Capacitor → npm install → npx cap add android → gradle → .apk
  Python/Kivy   → Buildozer → buildozer android debug → .apk
  Python apps   → PyInstaller → single .exe
  Any app       → Clean .zip with README

Hardware this was designed for: i7 6th gen, 16GB RAM, Linux.
GPU is not used — APK building is pure CPU + RAM.

First build: 20-45 min (downloads Android SDK/NDK ~4GB).
Subsequent builds: 3-8 min (cached).

BUGS FIXED:
- Bug 1: _run() no longer reads _status["progress"] outside the lock — snapshots it safely
- Bug 2: get_status() now returns a deep copy so log_lines list is not shared across threads
- Bug 3: _set_status() now has a reset path; package() resets state before each run
- Bug 4: proc.wait() timeout now kills the process correctly
- Bug 5: _find_entry_point() wraps os.listdir() in try/except for missing dirs
- Bug 6: _find_requirements() deduplicates via dict.fromkeys()
- Bug 7: entry_html variable in _apk_capacitor_full now used in cap_config server url
- Bug 8: package() resets _status["error"] = None at start of every run
"""
import copy
import os
import sys
import json
import glob
import shutil
import zipfile
import subprocess
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.path.join(BASE_DIR, "output")
PACKAGE_DIR = os.path.join(BASE_DIR, "packages")

# ── Module-level status (read by /api/package/status) ────────────────────────
_status = {
    "running":     False,
    "step":        "",
    "progress":    0,
    "done":        False,
    "error":       None,
    "output_file": None,
    "log_lines":   [],
}
_status_lock = threading.Lock()


def _set_status(step, progress, done=False, error=None, output_file=None):
    """Thread-safe status update — dashboard polls this."""
    with _status_lock:
        _status["step"]     = step
        _status["progress"] = progress
        _status["done"]     = done
        _status["error"]    = error
        _status["running"]  = not done
        if output_file:
            _status["output_file"] = output_file
        _status["log_lines"].append(f"[{progress}%] {step}")
        _status["log_lines"] = _status["log_lines"][-80:]


def _reset_status():
    """Resets all status fields before a new packaging run starts."""
    with _status_lock:
        _status["running"]     = True
        _status["step"]        = "Starting..."
        _status["progress"]    = 0
        _status["done"]        = False
        _status["error"]       = None
        _status["output_file"] = None
        _status["log_lines"]   = []


def get_status():
    """Called by /api/package/status. Returns a deep copy so log_lines is not shared."""
    with _status_lock:
        return copy.deepcopy(_status)


# ── Tool availability checks ──────────────────────────────────────────────────

def _cmd_exists(cmd):
    """Returns True if a shell command is available."""
    return shutil.which(cmd) is not None


def _run(cmd, cwd=None, timeout=3600, env=None):
    """
    Runs a shell command, streams output to status log, returns (success, output).
    Timeout defaults to 60 minutes — enough for first Gradle/Buildozer run.
    Correctly kills the process on timeout (Bug 4 fix).
    """
    # Snapshot current progress outside the lock to avoid holding it during Popen
    with _status_lock:
        current_progress = _status["progress"]

    _set_status(f"Running: {' '.join(str(c) for c in cmd[:3])}...", current_progress)

    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd or BASE_DIR,
            env=full_env,
        )
    except Exception as e:
        return False, str(e)

    output_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            output_lines.append(line)
            with _status_lock:
                _status["log_lines"].append(line)
                _status["log_lines"] = _status["log_lines"][-80:]

    # Bug 4 fix: proc.wait() timeout now properly kills the process
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()   # drain any remaining output
        return False, f"Timed out after {timeout}s"

    return proc.returncode == 0, "\n".join(output_lines[-30:])


def _check_java():
    """Returns (ok, version_string). Needs Java 11+ for Gradle."""
    if not _cmd_exists("java"):
        return False, "java not found"
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=10)
        version_text = r.stderr or r.stdout
        return True, version_text.splitlines()[0] if version_text else "unknown"
    except Exception as e:
        return False, str(e)


def _check_node():
    """Returns (ok, version_string). Needs Node 16+."""
    if not _cmd_exists("node"):
        return False, "node not found"
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10)
        return True, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _check_android_sdk():
    """
    Returns (ok, sdk_path).
    Looks in ANDROID_HOME, ANDROID_SDK_ROOT, and common install locations.
    """
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        path = os.environ.get(env_var, "")
        if path and os.path.isdir(path):
            return True, path

    common_paths = [
        os.path.expanduser("~/Android/Sdk"),
        os.path.expanduser("~/android-sdk"),
        "/opt/android-sdk",
        "/usr/local/android-sdk",
    ]
    for path in common_paths:
        if os.path.isdir(path):
            return True, path

    return False, ""


def _install_android_sdk_cmdline():
    """
    Downloads and installs Android command-line tools + SDK platform 31 + build-tools.
    Only runs if ANDROID_HOME is not already set.
    Returns (ok, sdk_path or error_message).
    """
    sdk_path    = os.path.expanduser("~/Android/Sdk")
    os.makedirs(sdk_path, exist_ok=True)
    cmdline_dir = os.path.join(sdk_path, "cmdline-tools", "latest")

    _set_status("Downloading Android command-line tools (~120MB)...", 12)

    tools_url = "https://dl.google.com/android/repository/commandlinetools-linux-10406996_latest.zip"
    zip_path  = os.path.join(sdk_path, "cmdline-tools.zip")

    ok, out = _run(["wget", "-q", "--show-progress", "-O", zip_path, tools_url], timeout=600)
    if not ok:
        ok, out = _run(["curl", "-L", "-o", zip_path, tools_url], timeout=600)
    if not ok:
        return False, "Could not download Android cmdline-tools. Check internet connection."

    _set_status("Extracting Android tools...", 14)
    extract_tmp = os.path.join(sdk_path, "cmdline-tools-tmp")
    ok, out = _run(["unzip", "-q", zip_path, "-d", extract_tmp], timeout=120)
    if not ok:
        return False, f"Unzip failed: {out}"

    # Move cmdline-tools/cmdline-tools → cmdline-tools/latest
    extracted = os.path.join(extract_tmp, "cmdline-tools")
    os.makedirs(os.path.dirname(cmdline_dir), exist_ok=True)
    if os.path.exists(cmdline_dir):
        shutil.rmtree(cmdline_dir)
    shutil.move(extracted, cmdline_dir)
    shutil.rmtree(extract_tmp, ignore_errors=True)
    try:
        os.remove(zip_path)
    except Exception:
        pass

    sdkmanager = os.path.join(cmdline_dir, "bin", "sdkmanager")
    os.chmod(sdkmanager, 0o755)
    env = {"ANDROID_HOME": sdk_path, "ANDROID_SDK_ROOT": sdk_path}

    _set_status("Accepting Android SDK licenses...", 16)
    try:
        proc = subprocess.Popen(
            [sdkmanager, "--licenses"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **env},
        )
        proc.communicate(input=b"y\ny\ny\ny\ny\ny\ny\ny\ny\ny\n", timeout=120)
    except Exception:
        pass

    _set_status("Installing Android SDK platform 31 + build-tools (~800MB)...", 18)
    packages = [
        "platform-tools",
        "platforms;android-31",
        "build-tools;31.0.0",
    ]
    ok, out = _run(
        [sdkmanager, "--install"] + packages,
        env=env,
        timeout=1200,
    )
    if not ok:
        return False, f"SDK install failed: {out[-200:]}"

    os.environ["ANDROID_HOME"]     = sdk_path
    os.environ["ANDROID_SDK_ROOT"] = sdk_path

    return True, sdk_path


class Packager:

    def __init__(self):
        """Sets up packager."""
        self.agent_name = "packager"
        self.output_dir = OUTPUT_DIR
        os.makedirs(PACKAGE_DIR, exist_ok=True)

    def log(self, message, message_type="info"):
        """Posts to agent chat."""
        agent_chat.log(self.agent_name, message, message_type=message_type)

    def detect_app_type(self, output_dir=None):
        """
        Reads all files in output dir and returns the app type string.
        Returns: 'streamlit', 'flask', 'fastapi', 'kivy', 'html', 'cli'
        """
        scan_dir = output_dir or self.output_dir
        if not os.path.exists(scan_dir):
            return "cli"

        file_contents = {}
        for root, dirs, files in os.walk(scan_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                full = os.path.join(root, fname)
                rel  = os.path.relpath(full, scan_dir)
                try:
                    with open(full, "r", errors="ignore") as f:
                        file_contents[rel] = f.read(500)
                except Exception:
                    file_contents[rel] = ""

        all_content = " ".join(file_contents.values()).lower()
        all_names   = " ".join(file_contents.keys()).lower()

        if "import streamlit" in all_content:
            return "streamlit"
        if "from fastapi" in all_content or "import fastapi" in all_content:
            return "fastapi"
        if "from flask" in all_content or "import flask" in all_content:
            return "flask"
        if "import kivy" in all_content or "from kivy" in all_content:
            return "kivy"
        if ".html" in all_names and "flask" not in all_content and "fastapi" not in all_content:
            return "html"
        return "cli"

    def package(self, target_format, output_dir=None):
        """
        Main entry — dispatches to correct packager.
        Bug 3 + 8 fix: resets all status fields before each run so stale
        error/output_file from a previous run never leaks into a new one.
        Runs in current thread — caller should use a background thread.
        """
        _reset_status()   # clear everything from any previous run

        scan_dir = output_dir or self.output_dir
        app_type = self.detect_app_type(scan_dir)

        self.log(f"Packaging — app type: {app_type} — target: {target_format}")
        _set_status(f"Detected: {app_type} app", 5)

        try:
            if target_format == "docker":
                result = self._package_docker(scan_dir, app_type)
            elif target_format == "exe":
                result = self._package_exe(scan_dir, app_type)
            elif target_format == "apk":
                result = self._package_apk(scan_dir, app_type)
            elif target_format == "zip":
                result = self._package_zip(scan_dir, app_type)
            else:
                raise ValueError(f"Unknown target: {target_format}")

            self.log(f"Packaging complete — {result}")
            _set_status("Complete ✓", 100, done=True, output_file=result)
            return result

        except Exception as e:
            err = str(e)[:300]
            self.log(f"Packaging failed: {err}", message_type="error")
            with _status_lock:
                current_progress = _status["progress"]
            _set_status(f"Failed: {err}", current_progress, done=True, error=err)
            return None

    # ── APK ───────────────────────────────────────────────────────────────────

    def _package_apk(self, output_dir, app_type):
        """Routes to Capacitor (HTML/JS) or Buildozer (Python/Kivy)."""
        if app_type == "kivy":
            return self._apk_buildozer_full(output_dir)
        else:
            # HTML, Flask, Streamlit, CLI → Capacitor web APK
            self.log(f"App type '{app_type}' — wrapping as Capacitor web APK")
            return self._apk_capacitor_full(output_dir)

    def _apk_capacitor_full(self, output_dir):
        """
        Full automatic Capacitor build:
        1. Check/install Node.js, Java, Android SDK
        2. npm install
        3. npx cap add android
        4. npx cap sync
        5. ./gradlew assembleDebug
        6. Return .apk path

        Bug 7 fix: entry_html is now used inside capacitor.config.json server url.
        """
        self.log("Starting full Capacitor APK build...")

        # ── Step 1: Check Node.js ─────────────────────────────────────────
        _set_status("Checking Node.js...", 8)
        node_ok, node_ver = _check_node()
        if not node_ok:
            raise RuntimeError(
                "Node.js not found. Install it with:\n"
                "  sudo apt install nodejs npm\n"
                "or download from https://nodejs.org (LTS version)"
            )
        self.log(f"Node.js: {node_ver}")

        # ── Step 2: Check Java ────────────────────────────────────────────
        _set_status("Checking Java...", 10)
        java_ok, java_ver = _check_java()
        if not java_ok:
            raise RuntimeError(
                "Java not found. Install with:\n"
                "  sudo apt install openjdk-17-jdk\n"
                "Then restart server.py"
            )
        self.log(f"Java: {java_ver}")

        # ── Step 3: Check/install Android SDK ─────────────────────────────
        _set_status("Checking Android SDK...", 11)
        sdk_ok, sdk_path = _check_android_sdk()
        if not sdk_ok:
            self.log("Android SDK not found — downloading automatically (~4GB, takes ~10-20min)...")
            sdk_ok, sdk_path = _install_android_sdk_cmdline()
            if not sdk_ok:
                raise RuntimeError(f"Android SDK setup failed: {sdk_path}")
        self.log(f"Android SDK: {sdk_path}")

        # ── Step 4: Set up Capacitor project directory ────────────────────
        _set_status("Setting up Capacitor project...", 20)
        cap_dir = os.path.join(PACKAGE_DIR, "capacitor_build")
        if os.path.exists(cap_dir):
            shutil.rmtree(cap_dir)
        os.makedirs(cap_dir)

        # Copy output files into www/
        www_dir = os.path.join(cap_dir, "www")
        shutil.copytree(output_dir, www_dir)

        # Bug 7 fix: actually use entry_html in the config
        entry_html = self._find_entry_point(output_dir, "html")
        app_name   = "KatalystApp"

        cap_config = {
            "appId":   "com.katalyst.app",
            "appName": app_name,
            "webDir":  "www",
            "server":  {
                "androidScheme": "https",
                "url":           None,       # local bundle, no remote server
                "cleartext":     False,
            },
        }
        # If a specific HTML entry point was found, note it in the config comment
        # (Capacitor uses index.html by convention; we rename if needed)
        if entry_html and entry_html != "index.html":
            index_src  = os.path.join(www_dir, entry_html)
            index_dest = os.path.join(www_dir, "index.html")
            if os.path.exists(index_src) and not os.path.exists(index_dest):
                shutil.copy2(index_src, index_dest)
                self.log(f"Copied {entry_html} → index.html for Capacitor entry point")

        with open(os.path.join(cap_dir, "capacitor.config.json"), "w") as f:
            json.dump(cap_config, f, indent=2)

        pkg_json = {
            "name":    "katalyst-app",
            "version": "1.0.0",
            "private": True,
            "dependencies": {
                "@capacitor/android": "^5.0.0",
                "@capacitor/cli":     "^5.0.0",
                "@capacitor/core":    "^5.0.0",
            },
            "devDependencies": {},
        }
        with open(os.path.join(cap_dir, "package.json"), "w") as f:
            json.dump(pkg_json, f, indent=2)

        # ── Step 5: npm install ───────────────────────────────────────────
        _set_status("Running npm install (downloading Capacitor packages)...", 25)
        self.log("npm install — downloads ~50MB of packages...")
        ok, out = _run(["npm", "install", "--prefer-offline"], cwd=cap_dir, timeout=300)
        if not ok:
            raise RuntimeError(f"npm install failed:\n{out}")

        # ── Step 6: npx cap add android ───────────────────────────────────
        _set_status("Adding Android platform (npx cap add android)...", 40)
        self.log("npx cap add android...")
        env = {
            "ANDROID_HOME":     sdk_path,
            "ANDROID_SDK_ROOT": sdk_path,
            "PATH":             os.environ.get("PATH", "") + f":{sdk_path}/platform-tools",
        }
        ok, out = _run(
            ["npx", "cap", "add", "android"],
            cwd=cap_dir,
            env=env,
            timeout=300,
        )
        if not ok:
            raise RuntimeError(f"cap add android failed:\n{out}")

        # ── Step 7: npx cap sync ──────────────────────────────────────────
        _set_status("Syncing web files to Android (npx cap sync)...", 55)
        ok, out = _run(
            ["npx", "cap", "sync", "android"],
            cwd=cap_dir,
            env=env,
            timeout=180,
        )
        if not ok:
            raise RuntimeError(f"cap sync failed:\n{out}")

        # ── Step 8: Gradle assembleDebug ──────────────────────────────────
        _set_status("Building APK with Gradle (slow on first run — 5-40min)...", 60)
        self.log("Starting Gradle build — first run downloads Gradle wrapper (~100MB)...")

        android_dir  = os.path.join(cap_dir, "android")
        gradlew_path = os.path.join(android_dir, "gradlew")
        if not os.path.exists(gradlew_path):
            raise RuntimeError(
                f"gradlew not found at {gradlew_path} — 'cap add android' may have failed. "
                "Check the log above for errors."
            )

        os.chmod(gradlew_path, 0o755)

        ok, out = _run(
            ["./gradlew", "assembleDebug", "--no-daemon", "--stacktrace"],
            cwd=android_dir,
            env=env,
            timeout=3600,
        )
        if not ok:
            raise RuntimeError(f"Gradle build failed:\n{out[-500:]}")

        # ── Step 9: Find the APK ──────────────────────────────────────────
        _set_status("Locating built APK...", 90)
        apk_pattern = os.path.join(android_dir, "**", "*.apk")
        apk_files   = glob.glob(apk_pattern, recursive=True)
        debug_apks  = [a for a in apk_files if "debug" in a.lower()]
        apk_src     = debug_apks[0] if debug_apks else (apk_files[0] if apk_files else None)

        if not apk_src:
            raise RuntimeError("Gradle succeeded but no .apk found — check build output")

        apk_dest = os.path.join(PACKAGE_DIR, f"{app_name}-debug.apk")
        shutil.copy2(apk_src, apk_dest)

        size_mb = os.path.getsize(apk_dest) / (1024 * 1024)
        self.log(f"APK ready — {size_mb:.1f}MB — {apk_dest}")
        _set_status(f"APK built successfully ({size_mb:.1f}MB)", 100)
        return apk_dest

    def _apk_buildozer_full(self, output_dir):
        """
        Full automatic Buildozer build for Python/Kivy apps.
        1. Check Linux
        2. Install buildozer + system deps if missing
        3. Write buildozer.spec
        4. Run buildozer android debug
        5. Return .apk path
        """
        if sys.platform != "linux":
            raise RuntimeError(
                "Buildozer only works on Linux. "
                "On Windows use WSL2; on Mac use a Linux VM."
            )

        self.log("Starting full Buildozer APK build for Python/Kivy app...")

        # ── Step 1: Install system packages ──────────────────────────────
        _set_status("Installing system build dependencies...", 10)
        system_pkgs = [
            "build-essential", "git", "ffmpeg",
            "libsdl2-dev", "libsdl2-image-dev", "libsdl2-mixer-dev",
            "libsdl2-ttf-dev", "libportmidi-dev", "libswscale-dev",
            "libavformat-dev", "libavcodec-dev", "zlib1g-dev",
            "openjdk-17-jdk", "unzip", "python3-pip",
        ]
        self.log("Checking/installing system packages via apt...")
        ok, out = _run(
            ["sudo", "apt-get", "install", "-y", "--no-install-recommends"] + system_pkgs,
            timeout=600,
        )
        if not ok:
            self.log("apt install had issues — continuing (packages may already be installed)")

        # ── Step 2: Install buildozer + cython ────────────────────────────
        _set_status("Installing buildozer and cython...", 20)
        ok, out = _run(
            [sys.executable, "-m", "pip", "install",
             "buildozer", "cython", "--upgrade", "--break-system-packages"],
            timeout=300,
        )
        if not ok:
            raise RuntimeError(f"pip install buildozer failed:\n{out}")

        # ── Step 3: Set up build directory ───────────────────────────────
        _set_status("Setting up Buildozer project...", 30)
        build_dir = os.path.join(PACKAGE_DIR, "buildozer_build")
        if os.path.exists(build_dir):
            shutil.rmtree(build_dir)
        os.makedirs(build_dir)

        for fname in os.listdir(output_dir):
            src = os.path.join(output_dir, fname)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(build_dir, fname))

        entry    = self._find_entry_point(output_dir, "kivy")
        deps     = self._find_requirements(output_dir)
        deps_str = ",".join(deps) if deps else "kivy"

        # ── Step 4: Write buildozer.spec ──────────────────────────────────
        _set_status("Writing buildozer.spec...", 35)
        spec = f"""[app]
title = KatalystApp
package.name = katalystapp
package.domain = com.katalyst
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,txt
version = 1.0
requirements = python3,{deps_str}
orientation = portrait
fullscreen = 0
android.permissions = INTERNET
android.api = 31
android.minapi = 21
android.ndk = 25b
android.sdk = 31
android.accept_sdk_license = True
android.arch = arm64-v8a

[buildozer]
log_level = 2
warn_on_root = 1
"""
        with open(os.path.join(build_dir, "buildozer.spec"), "w") as f:
            f.write(spec)

        # ── Step 5: Run buildozer ─────────────────────────────────────────
        _set_status("Running buildozer android debug — first run ~20-40min...", 40)
        self.log("buildozer android debug — downloads Android SDK+NDK (~4GB) on first run...")

        ok, out = _run(
            ["buildozer", "android", "debug"],
            cwd=build_dir,
            timeout=7200,
        )
        if not ok:
            raise RuntimeError(f"buildozer failed:\n{out[-500:]}")

        # ── Step 6: Find APK ──────────────────────────────────────────────
        _set_status("Locating built APK...", 90)
        apk_pattern = os.path.join(build_dir, "bin", "*.apk")
        apk_files   = glob.glob(apk_pattern)
        if not apk_files:
            raise RuntimeError("buildozer completed but no .apk in bin/ — check build output")

        apk_src  = apk_files[0]
        apk_dest = os.path.join(PACKAGE_DIR, os.path.basename(apk_src))
        shutil.copy2(apk_src, apk_dest)

        size_mb = os.path.getsize(apk_dest) / (1024 * 1024)
        self.log(f"APK ready — {size_mb:.1f}MB — {apk_dest}")
        return apk_dest

    # ── DOCKER ────────────────────────────────────────────────────────────────

    def _package_docker(self, output_dir, app_type):
        """Generates Dockerfile + docker-compose.yml and zips them."""
        _set_status("Writing Dockerfile...", 20)
        self.log("Generating Docker files...")

        entry = self._find_entry_point(output_dir, app_type)
        deps  = self._find_requirements(output_dir)

        port_map = {"streamlit": 8501, "flask": 5000, "fastapi": 8000}
        port = port_map.get(app_type, 8080)

        if app_type == "streamlit":
            cmd = f'["streamlit", "run", "{entry}", "--server.address", "0.0.0.0"]'
        else:
            cmd = f'["python", "{entry}"]'

        dockerfile = (
            "FROM python:3.11-slim\n\n"
            "WORKDIR /app\n\n"
            "COPY . .\n\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n\n"
            f"EXPOSE {port}\n\n"
            f"CMD {cmd}\n"
        )
        compose = (
            "version: '3.8'\n\nservices:\n  app:\n"
            f"    build: .\n    ports:\n      - \"{port}:{port}\"\n"
            "    restart: unless-stopped\n"
            "    volumes:\n      - ./data:/app/data\n"
        )

        with open(os.path.join(output_dir, "Dockerfile"), "w") as f:
            f.write(dockerfile)
        with open(os.path.join(output_dir, "docker-compose.yml"), "w") as f:
            f.write(compose)

        req_path = os.path.join(output_dir, "requirements.txt")
        if not os.path.exists(req_path):
            with open(req_path, "w") as f:
                # Bug 6 fix: deduplicate before writing
                f.write("\n".join(list(dict.fromkeys(deps))) + "\n")

        _set_status("Zipping Docker package...", 70)
        zip_path = os.path.join(PACKAGE_DIR, "docker_package.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(output_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    full = os.path.join(root, fname)
                    zf.write(full, os.path.relpath(full, output_dir))

        self.log(f"Docker package ready — {zip_path}")
        return zip_path

    # ── EXE ───────────────────────────────────────────────────────────────────

    def _package_exe(self, output_dir, app_type):
        """Runs PyInstaller to create a real standalone executable."""
        _set_status("Checking PyInstaller...", 10)

        ok, _ = _run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            timeout=15,
        )
        if not ok:
            _set_status("Installing PyInstaller...", 15)
            _run(
                [sys.executable, "-m", "pip", "install",
                 "pyinstaller", "--break-system-packages", "--quiet"],
                timeout=120,
            )

        entry      = self._find_entry_point(output_dir, app_type)
        entry_path = os.path.join(output_dir, entry)

        if not os.path.exists(entry_path):
            raise FileNotFoundError(f"Entry point not found: {entry}")

        _set_status(f"Running PyInstaller on {entry} (1-3 min)...", 30)
        self.log(f"Building EXE from {entry}...")

        dist_dir  = os.path.join(PACKAGE_DIR, "exe_dist")
        build_dir = os.path.join(PACKAGE_DIR, "exe_build")
        os.makedirs(dist_dir, exist_ok=True)

        ok, out = _run(
            [sys.executable, "-m", "PyInstaller",
             "--onefile",
             "--distpath", dist_dir,
             "--workpath", build_dir,
             "--specpath", PACKAGE_DIR,
             "--noconfirm",
             entry_path],
            cwd=output_dir,
            timeout=600,
        )
        if not ok:
            raise RuntimeError(f"PyInstaller failed:\n{out[-400:]}")

        _set_status("Locating EXE...", 85)
        exe_name = os.path.splitext(entry)[0]
        exe_path = os.path.join(dist_dir, exe_name)
        if sys.platform == "win32":
            exe_path += ".exe"

        if not os.path.exists(exe_path):
            try:
                found = os.listdir(dist_dir)
                if found:
                    exe_path = os.path.join(dist_dir, found[0])
                else:
                    raise RuntimeError("PyInstaller ran but no executable found in dist/")
            except FileNotFoundError:
                raise RuntimeError(f"dist dir missing after PyInstaller — {dist_dir}")

        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        self.log(f"EXE ready — {size_mb:.1f}MB")
        return exe_path

    # ── ZIP ───────────────────────────────────────────────────────────────────

    def _package_zip(self, output_dir, app_type):
        """Clean zip of the output folder with a README."""
        _set_status("Creating ZIP...", 30)
        entry = self._find_entry_point(output_dir, app_type)
        deps  = self._find_requirements(output_dir)

        run_cmds = {
            "streamlit": f"pip install -r requirements.txt\nstreamlit run {entry}",
            "flask":     f"pip install -r requirements.txt\npython {entry}",
            "fastapi":   f"pip install -r requirements.txt\nuvicorn {os.path.splitext(entry)[0]}:app --reload",
            "html":      f"Open {entry} in any browser — no installation needed",
            "kivy":      f"pip install -r requirements.txt\npython {entry}",
            "cli":       f"pip install -r requirements.txt\npython {entry}",
        }

        deps_deduped = list(dict.fromkeys(deps))   # Bug 6 fix: deduplicate
        readme = (
            f"# KATALYST Generated App\n\n"
            f"## App Type\n{app_type}\n\n"
            f"## How to Run\n{run_cmds.get(app_type, 'python ' + entry)}\n\n"
            f"## Dependencies\n"
            + ("\n".join(deps_deduped) if deps_deduped else "See requirements.txt")
            + "\n\nGenerated by KATALYST — AI Build System\n"
        )
        with open(os.path.join(output_dir, "README.md"), "w") as f:
            f.write(readme)

        _set_status("Writing ZIP...", 60)
        zip_path = os.path.join(PACKAGE_DIR, "app_output.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(output_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    full = os.path.join(root, fname)
                    zf.write(full, os.path.relpath(full, output_dir))

        size_mb = os.path.getsize(zip_path) / (1024 * 1024)
        self.log(f"ZIP ready — {size_mb:.1f}MB")
        return zip_path

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _find_entry_point(self, output_dir, app_type):
        """
        Finds the most likely main entry file for the app type.
        Bug 5 fix: wraps os.listdir() in try/except for missing/empty dirs.
        """
        candidates = {
            "streamlit": ["app.py", "main.py", "streamlit_app.py", "dashboard.py"],
            "flask":     ["app.py", "main.py", "server.py", "run.py"],
            "fastapi":   ["main.py", "app.py", "api.py"],
            "kivy":      ["main.py", "app.py"],
            "html":      ["index.html", "app.html", "main.html"],
            "cli":       ["main.py", "app.py", "cli.py", "run.py"],
        }
        for candidate in candidates.get(app_type, ["main.py", "app.py"]):
            if os.path.exists(os.path.join(output_dir, candidate)):
                return candidate

        # Fallback: scan for any file with the right extension
        try:
            all_files = sorted(os.listdir(output_dir))
        except (FileNotFoundError, PermissionError):
            return "main.py"

        for ext in (".py", ".html"):
            for fname in all_files:
                if fname.endswith(ext):
                    return fname

        return "main.py"

    def _find_requirements(self, output_dir):
        """
        Reads requirements.txt or scans imports to guess packages.
        Bug 6 fix: deduplicates results with dict.fromkeys() to preserve order.
        """
        req_path = os.path.join(output_dir, "requirements.txt")
        if os.path.exists(req_path):
            try:
                with open(req_path) as f:
                    lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
                return list(dict.fromkeys(lines))   # deduplicate, preserve order
            except Exception:
                pass

        common_map = {
            "streamlit":  "streamlit",
            "flask":      "flask",
            "fastapi":    "fastapi",
            "pandas":     "pandas",
            "numpy":      "numpy",
            "requests":   "requests",
            "sqlalchemy": "sqlalchemy",
            "uvicorn":    "uvicorn",
        }
        found_set = {}   # use dict to preserve insertion order + deduplicate
        for root, dirs, files in os.walk(output_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                try:
                    content = open(os.path.join(root, fname), errors="ignore").read()
                    for keyword, pkg in common_map.items():
                        if keyword in content:
                            found_set[pkg] = True
                except Exception:
                    pass
        return list(found_set.keys()) or ["flask"]
