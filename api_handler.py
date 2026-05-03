"""
api_handler.py — KATALYST AI Provider Layer
Providers: NVIDIA NIM + SambaNova + Cerebras + Mistral + Groq (universal fallback)

Agent → Primary model assignments:
  architect    → NVIDIA NIM    deepseek-ai/deepseek-r1
  planner      → SambaNova     gpt-oss-120b
  orchestrator → Groq          llama-3.3-70b-versatile
  coder        → Cerebras      gpt-oss-120b
  debugger     → Cerebras      gpt-oss-120b
  reviewer     → Mistral       codestral-latest
  validator    → Groq          llama-3.1-8b-instant

Universal fallback for ALL agents → Groq llama-3.3-70b-versatile
"""
import os
import time
import threading
from logger import log

# ── Load .env ────────────────────────────────────────────────────────────────
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()

load_env()

NIM_KEY       = os.environ.get("NIM_KEY", "")
SAMBANOVA_KEY = os.environ.get("SAMBANOVA_KEY", "")
CEREBRAS_KEY  = os.environ.get("CEREBRAS_KEY", "")
MISTRAL_KEY   = os.environ.get("MISTRAL_KEY", "")
GROQ_KEY      = os.environ.get("GROQ_KEY", "")

# ── Model IDs ─────────────────────────────────────────────────────────────────
NIM_ARCHITECT       = "deepseek-ai/deepseek-r1"
SAMBANOVA_PLANNER   = "gpt-oss-120b"            # OpenAI GPT-OSS 120B MoE on SambaNova RDUs (600+ t/s)
GROQ_ORCHESTRATOR   = "llama-3.3-70b-versatile"
CEREBRAS_CODER      = "gpt-oss-120b"            # OpenAI GPT-OSS 120B MoE — Cerebras production model
CEREBRAS_DEBUGGER   = "gpt-oss-120b"            # same — llama-3.3-70b never existed on Cerebras
MISTRAL_REVIEWER    = "codestral-latest"
GROQ_VALIDATOR      = "llama-3.1-8b-instant"
GROQ_FALLBACK       = "llama-3.3-70b-versatile"   # universal fallback

# ── Provider base URLs ────────────────────────────────────────────────────────
NIM_BASE_URL       = "https://integrate.api.nvidia.com/v1"
SAMBANOVA_BASE_URL = "https://api.sambanova.ai/v1"

LIVE_FEED_FILE = "logs/live_feed.txt"

# ── Rate limit tracking ───────────────────────────────────────────────────────
_rate_lock       = threading.Lock()
_rate_hits       = {"nim": 0, "sambanova": 0, "cerebras": 0, "mistral": 0, "groq": 0}
_provider_paused = {"nim": False, "sambanova": False, "cerebras": False, "mistral": False, "groq": False}
_call_counts     = {"nim": 0, "sambanova": 0, "cerebras": 0, "mistral": 0, "groq": 0}
RATE_LIMIT_SWITCH = 3

# Tracks which provider + model each agent actually used last call (primary or fallback).
# Format: { "coder": {"provider": "cerebras", "model": "gpt-oss-120b", "is_fallback": False}, ... }
_last_provider_used = {}
_last_provider_lock = threading.Lock()

# ── Client init ───────────────────────────────────────────────────────────────

# NVIDIA NIM — OpenAI-compatible
nim_client = None
if NIM_KEY:
    try:
        from openai import OpenAI as OpenAIClient
        nim_client = OpenAIClient(api_key=NIM_KEY, base_url=NIM_BASE_URL)
        log("NVIDIA NIM client ready")
    except Exception as e:
        log(f"NIM init failed: {e}", "WARNING")

# SambaNova — OpenAI-compatible
sambanova_client = None
if SAMBANOVA_KEY:
    try:
        from openai import OpenAI as OpenAIClient
        sambanova_client = OpenAIClient(api_key=SAMBANOVA_KEY, base_url=SAMBANOVA_BASE_URL)
        log("SambaNova client ready")
    except Exception as e:
        log(f"SambaNova init failed: {e}", "WARNING")

# Cerebras
cerebras_client = None
if CEREBRAS_KEY:
    try:
        from cerebras.cloud.sdk import Cerebras
        cerebras_client = Cerebras(api_key=CEREBRAS_KEY)
        log("Cerebras client ready")
    except Exception as e:
        log(f"Cerebras init failed: {e}", "WARNING")

# Mistral
mistral_client = None
if MISTRAL_KEY:
    try:
        from mistralai import Mistral
        mistral_client = Mistral(api_key=MISTRAL_KEY)
        log("Mistral client ready")
    except Exception as e:
        log(f"Mistral init failed: {e}", "WARNING")

# Groq
groq_client = None
if GROQ_KEY:
    try:
        from groq import Groq
        groq_client = Groq(api_key=GROQ_KEY)
        log("Groq client ready")
    except Exception as e:
        log(f"Groq init failed: {e}", "WARNING")


# ── Helpers ───────────────────────────────────────────────────────────────────

def write_live(text):
    """Writes current AI output to live feed for dashboard display."""
    os.makedirs("logs", exist_ok=True)
    with open(LIVE_FEED_FILE, "w") as f:
        f.write(text)


def strip_markdown(text):
    """Strips code fences from AI output — returns raw code only."""
    if not text:
        return text
    text  = text.strip()
    lines = text.split("\n")
    return "\n".join(l for l in lines if not l.strip().startswith("```")).strip()


def _handle_rate_limit(provider):
    """Exponential backoff on rate limit. Pauses provider after threshold."""
    with _rate_lock:
        _rate_hits[provider] = _rate_hits.get(provider, 0) + 1
        hits = _rate_hits[provider]
    wait = min(2 ** hits, 32)
    log(f"Rate limit on {provider} (#{hits}) — waiting {wait}s", "WARNING")
    time.sleep(wait)
    if hits >= RATE_LIMIT_SWITCH:
        with _rate_lock:
            _provider_paused[provider] = True
        if provider == "groq":
            log(f"Groq rate limited {hits}x — pausing 60s", "WARNING")
            time.sleep(60)
            with _rate_lock:
                _provider_paused["groq"] = False
                _rate_hits["groq"]       = 0
            log("Groq resumed")
        return False
    return True


# ── Provider call functions ───────────────────────────────────────────────────

def ask_nim(prompt, model=NIM_ARCHITECT):
    """NVIDIA NIM call — OpenAI-compatible streaming. Returns cleaned text or None."""
    if not nim_client:
        return None
    with _rate_lock:
        if _provider_paused.get("nim"):
            return None
        _call_counts["nim"] = _call_counts.get("nim", 0) + 1
    try:
        log(f"NIM ({model})...")
        stream = nim_client.chat.completions.create(
            model    = model,
            messages = [{"role": "user", "content": prompt}],
            max_tokens = 16000,
            stream   = True,
        )
        full = ""
        write_live("▋")
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full += delta
                write_live(full + "▋")
        write_live(full)
        if not full.strip():
            log("NIM empty response", "WARNING")
            return None
        result = strip_markdown(full)
        log(f"NIM → {len(result)} chars")
        return result
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _handle_rate_limit("nim")
        else:
            log(f"NIM error: {err[:120]}", "WARNING")
        return None


def ask_sambanova(prompt, model=SAMBANOVA_PLANNER):
    """SambaNova call — OpenAI-compatible streaming. Returns cleaned text or None."""
    if not sambanova_client:
        return None
    with _rate_lock:
        if _provider_paused.get("sambanova"):
            return None
        _call_counts["sambanova"] = _call_counts.get("sambanova", 0) + 1
    try:
        log(f"SambaNova ({model})...")
        stream = sambanova_client.chat.completions.create(
            model    = model,
            messages = [{"role": "user", "content": prompt}],
            max_tokens = 16000,
            stream   = True,
        )
        full = ""
        write_live("▋")
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full += delta
                write_live(full + "▋")
        write_live(full)
        if not full.strip():
            log("SambaNova empty response", "WARNING")
            return None
        result = strip_markdown(full)
        log(f"SambaNova → {len(result)} chars")
        return result
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _handle_rate_limit("sambanova")
        else:
            log(f"SambaNova error: {err[:120]}", "WARNING")
        return None


def ask_cerebras(prompt, model=CEREBRAS_CODER):
    """Cerebras call with streaming. Returns cleaned text or None."""
    if not cerebras_client:
        return None
    with _rate_lock:
        if _provider_paused.get("cerebras"):
            return None
        _call_counts["cerebras"] = _call_counts.get("cerebras", 0) + 1
    try:
        log(f"Cerebras ({model})...")
        stream = cerebras_client.chat.completions.create(
            model    = model,
            messages = [{"role": "user", "content": prompt}],
            max_tokens = 16000,
            stream   = True,
        )
        full = ""
        write_live("▋")
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full += delta
                write_live(full + "▋")
        write_live(full)
        if not full.strip():
            log("Cerebras empty response", "WARNING")
            return None
        result = strip_markdown(full)
        log(f"Cerebras → {len(result)} chars")
        return result
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _handle_rate_limit("cerebras")
        else:
            log(f"Cerebras error: {err[:120]}", "WARNING")
        return None


def ask_mistral(prompt, model=MISTRAL_REVIEWER):
    """Mistral call — non-streaming. Returns cleaned text or None."""
    if not mistral_client:
        return None
    with _rate_lock:
        if _provider_paused.get("mistral"):
            return None
        _call_counts["mistral"] = _call_counts.get("mistral", 0) + 1
    try:
        log(f"Mistral ({model})...")
        response = mistral_client.chat.complete(
            model    = model,
            messages = [{"role": "user", "content": prompt}],
            max_tokens = 4000,
        )
        result = response.choices[0].message.content
        if not result or not result.strip():
            log("Mistral empty response", "WARNING")
            return None
        log(f"Mistral → {len(result)} chars")
        return result.strip()
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _handle_rate_limit("mistral")
        else:
            log(f"Mistral error: {err[:120]}", "WARNING")
        return None


def ask_groq(prompt, model=GROQ_FALLBACK):
    """Groq call with streaming. Returns cleaned text or None."""
    if not groq_client:
        return None
    with _rate_lock:
        if _provider_paused.get("groq"):
            return None
        _call_counts["groq"] = _call_counts.get("groq", 0) + 1
    try:
        log(f"Groq ({model})...")
        stream = groq_client.chat.completions.create(
            model      = model,
            messages   = [{"role": "user", "content": prompt}],
            max_tokens = 4096,
            stream     = True,
        )
        full = ""
        write_live("▋")
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                full += delta
                write_live(full + "▋")
        write_live(full)
        if not full.strip():
            log("Groq empty response", "WARNING")
            return None
        result = strip_markdown(full)
        log(f"Groq → {len(result)} chars")
        return result
    except Exception as e:
        err = str(e)
        if "429" in err or "rate" in err.lower():
            _handle_rate_limit("groq")
        else:
            log(f"Groq error: {err[:120]}", "WARNING")
        return None


# ── Main routing function ─────────────────────────────────────────────────────

def ask_for_agent(prompt, agent_name):
    """
    Routes each agent to the best available provider.
    Falls back gracefully based on which keys are actually set in .env.

    With all keys:
      architect    → NVIDIA NIM    deepseek-r1
      planner      → SambaNova     gpt-oss-120b
      coder        → Cerebras      gpt-oss-120b
      debugger     → Cerebras      gpt-oss-120b
      reviewer     → Mistral       codestral-latest
      orchestrator → Groq          llama-3.3-70b-versatile
      validator    → Groq          llama-3.1-8b-instant

    With only GROQ_KEY + MISTRAL_KEY (the common setup):
      architect    → Groq          llama-3.3-70b-versatile
      planner      → Groq          llama-3.3-70b-versatile
      coder        → Groq          llama-3.3-70b-versatile
      debugger     → Groq          llama-3.3-70b-versatile
      reviewer     → Mistral       codestral-latest  (if MISTRAL_KEY set)
      orchestrator → Groq          llama-3.3-70b-versatile
      validator    → Groq          llama-3.1-8b-instant

    Universal fallback for everything → Groq llama-3.3-70b-versatile
    """
    result        = None
    used_provider = None
    used_model    = None
    is_fallback   = False

    if agent_name == "architect":
        if nim_client:
            result = ask_nim(prompt, model=NIM_ARCHITECT)
            if result:
                used_provider, used_model, is_fallback = "nim", NIM_ARCHITECT, False
        if not result:
            result = ask_groq(prompt, model=GROQ_FALLBACK)
            if result:
                used_provider, used_model, is_fallback = "groq", GROQ_FALLBACK, True

    elif agent_name == "planner":
        if sambanova_client:
            result = ask_sambanova(prompt, model=SAMBANOVA_PLANNER)
            if result:
                used_provider, used_model, is_fallback = "sambanova", SAMBANOVA_PLANNER, False
        if not result:
            result = ask_groq(prompt, model=GROQ_FALLBACK)
            if result:
                used_provider, used_model, is_fallback = "groq", GROQ_FALLBACK, True

    elif agent_name == "orchestrator":
        result = ask_groq(prompt, model=GROQ_ORCHESTRATOR)
        if result:
            used_provider, used_model, is_fallback = "groq", GROQ_ORCHESTRATOR, False

    elif agent_name in ("coder", "debugger"):
        if cerebras_client:
            result = ask_cerebras(prompt, model=CEREBRAS_CODER)
            if result:
                used_provider, used_model, is_fallback = "cerebras", CEREBRAS_CODER, False
        if not result:
            result = ask_groq(prompt, model=GROQ_FALLBACK)
            if result:
                used_provider, used_model, is_fallback = "groq", GROQ_FALLBACK, True

    elif agent_name == "reviewer":
        if mistral_client:
            result = ask_mistral(prompt, model=MISTRAL_REVIEWER)
            if result:
                used_provider, used_model, is_fallback = "mistral", MISTRAL_REVIEWER, False
        if not result:
            result = ask_groq(prompt, model=GROQ_FALLBACK)
            if result:
                used_provider, used_model, is_fallback = "groq", GROQ_FALLBACK, True

    elif agent_name == "validator":
        result = ask_groq(prompt, model=GROQ_VALIDATOR)
        if result:
            used_provider, used_model, is_fallback = "groq", GROQ_VALIDATOR, False

    else:
        if cerebras_client:
            result = ask_cerebras(prompt)
            if result:
                used_provider, used_model, is_fallback = "cerebras", CEREBRAS_CODER, False
        if not result:
            result = ask_groq(prompt, model=GROQ_FALLBACK)
            if result:
                used_provider, used_model, is_fallback = "groq", GROQ_FALLBACK, True

    # Last resort fallback
    if not result:
        log(f"{agent_name}: all providers failed — final Groq attempt", "WARNING")
        result = ask_groq(prompt, model=GROQ_FALLBACK)
        if result:
            used_provider, used_model, is_fallback = "groq", GROQ_FALLBACK, True

    # Record which provider this agent actually used so dashboard can show it
    if used_provider:
        with _last_provider_lock:
            _last_provider_used[agent_name] = {
                "provider":    used_provider,
                "model":       used_model,
                "is_fallback": is_fallback,
            }

    return result


def get_agent_provider_status():
    """Returns which provider each agent last used and whether it was a fallback."""
    with _last_provider_lock:
        return dict(_last_provider_used)


# ── Utility functions (used by other modules) ─────────────────────────────────

def get_blueprint_for_task(full_blueprint, task_description, max_chars=5000):
    """
    Returns the relevant portion of the blueprint for a specific task.
    Prioritises the File Architecture section then fills remaining budget.
    """
    if not full_blueprint:
        return ""
    arch_markers = ["FILE ARCHITECTURE", "FILE MAP", "FILES", "ARCHITECTURE"]
    arch_section = ""
    bp_upper = full_blueprint.upper()
    for marker in arch_markers:
        idx = bp_upper.find(marker)
        if idx != -1:
            arch_section = full_blueprint[idx:idx + 1500]
            break
    if arch_section:
        remaining   = max_chars - len(arch_section) - 100
        top_section = full_blueprint[:remaining] if remaining > 0 else ""
        result = top_section + "\n\n...[FILE ARCHITECTURE]...\n" + arch_section
    else:
        result = full_blueprint[:max_chars]
    return result.strip()


def validate_non_python(code, filename):
    """
    Basic structural validation for non-Python file types.
    Returns (passed: bool, reason: str)
    """
    ext = os.path.splitext(filename)[1].lower()

    if ext in (".html", ".htm"):
        stripped = code.strip().lower()
        if len(code.strip()) < 20:
            return False, "HTML file is nearly empty"
        if "<html" not in stripped and "<!doctype" not in stripped:
            return False, "HTML file missing <html> or <!DOCTYPE> tag"
        if stripped.count("<body") > 0 and stripped.count("</body") == 0:
            return False, "HTML file has <body> but no </body> — file is truncated"
        return True, "ok"

    if ext == ".json":
        try:
            import json
            json.loads(code)
            return True, "ok"
        except Exception as e:
            return False, f"Invalid JSON: {str(e)[:100]}"

    if ext == ".js":
        if len(code.strip()) < 10:
            return False, "JS file is nearly empty"
        opens  = code.count("{")
        closes = code.count("}")
        if opens > 0 and abs(opens - closes) > 3:
            return False, f"JS file has {opens} open braces and {closes} close braces — likely truncated"
        return True, "ok"

    if ext == ".css":
        if len(code.strip()) < 10:
            return False, "CSS file is nearly empty"
        opens  = code.count("{")
        closes = code.count("}")
        if opens != closes:
            return False, f"CSS file has mismatched braces ({opens} open, {closes} close)"
        return True, "ok"

    return True, f"no structural check for {ext} files"


def smart_ask(prompt, mode="code"):
    """Generic ask — Cerebras first, Groq fallback."""
    result = ask_cerebras(prompt)
    if result:
        return result
    log("Cerebras failed — switching to Groq", "WARNING")
    return ask_groq(prompt)


def ask_with_retry(prompt, max_attempts=2, agent_name="unknown"):
    """Retries across providers up to max_attempts. Returns None only if all fail."""
    for attempt in range(1, max_attempts + 1):
        log(f"ask_with_retry attempt {attempt}/{max_attempts} for {agent_name}")
        result = ask_for_agent(prompt, agent_name)
        if result:
            return result
        if attempt < max_attempts:
            log(f"Attempt {attempt} failed — retrying in 3s", "WARNING")
            time.sleep(3)
    log(f"All {max_attempts} attempts failed for {agent_name}", "WARNING")
    return None


def get_available_providers():
    """Returns status of all providers — used by dashboard and health monitor."""
    with _rate_lock:
        return {
            "nim":             bool(nim_client)       and not _provider_paused.get("nim"),
            "sambanova":       bool(sambanova_client) and not _provider_paused.get("sambanova"),
            "cerebras":        bool(cerebras_client)  and not _provider_paused.get("cerebras"),
            "mistral":         bool(mistral_client)   and not _provider_paused.get("mistral"),
            "groq":            bool(groq_client)      and not _provider_paused.get("groq"),
            "nim_calls":       _call_counts.get("nim", 0),
            "sambanova_calls": _call_counts.get("sambanova", 0),
            "cerebras_calls":  _call_counts.get("cerebras", 0),
            "mistral_calls":   _call_counts.get("mistral", 0),
            "groq_calls":      _call_counts.get("groq", 0),
            "rate_hits":       dict(_rate_hits),
        }


def get_available_provider():
    """Returns name of first available provider, or None."""
    p = get_available_providers()
    for name in ("nim", "sambanova", "cerebras", "mistral", "groq"):
        if p.get(name):
            return name
    return None


def reset_rate_limits():
    """Resets all rate limit counters — called at project start."""
    with _rate_lock:
        _rate_hits.clear()
        for k in _provider_paused:
            _provider_paused[k] = False
        _call_counts.clear()
    log("Rate limit counters reset")


# Legacy aliases kept so other files don't break
def ask_groq_small(prompt):
    return ask_groq(prompt, model=GROQ_FALLBACK)

CEREBRAS_MODEL    = CEREBRAS_CODER
GROQ_MODEL_SMALL  = GROQ_VALIDATOR
