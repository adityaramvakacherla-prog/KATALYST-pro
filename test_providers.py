"""
test_providers.py — KATALYST Provider Test
Run with: python3 test_providers.py
Checks every provider and model with a simple ping.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load the api_handler from same folder
from api_handler import (
    ask_nim, ask_sambanova, ask_cerebras, ask_mistral, ask_groq,
    NIM_ARCHITECT, SAMBANOVA_PLANNER, GROQ_ORCHESTRATOR,
    CEREBRAS_CODER, MISTRAL_REVIEWER, GROQ_VALIDATOR, GROQ_FALLBACK,
    nim_client, sambanova_client, cerebras_client, mistral_client, groq_client,
)

PASS = "✓ PASS"
FAIL = "✗ FAIL"
SKIP = "⚠ SKIP"

results = []

def test(name, fn):
    print(f"  Testing {name}...", end=" ", flush=True)
    try:
        result = fn()
        if result:
            print(PASS + f" — got {len(result)} chars")
            results.append((name, True))
        else:
            print(FAIL + " — returned empty or None")
            results.append((name, False))
    except Exception as e:
        print(FAIL + f" — {type(e).__name__}: {str(e)[:80]}")
        results.append((name, False))

PING = "Say the single word PONG and nothing else."

print("\n╔══════════════════════════════════════════╗")
print("║  ⚡ KATALYST — Provider Test              ║")
print("╚══════════════════════════════════════════╝\n")

# ── Check keys are set ────────────────────────────────────────────────────────
print("── Key Check ──")
for name, client, key_name in [
    ("NVIDIA NIM",  nim_client,       "NIM_KEY"),
    ("SambaNova",   sambanova_client, "SAMBANOVA_KEY"),
    ("Cerebras",    cerebras_client,  "CEREBRAS_KEY"),
    ("Mistral",     mistral_client,   "MISTRAL_KEY"),
    ("Groq",        groq_client,      "GROQ_KEY"),
]:
    status = "✓ key loaded" if client else f"✗ {key_name} not set in .env"
    print(f"  {name}: {status}")

print("\n── Model IDs in Use ──")
print(f"  Architect  → NIM:       {NIM_ARCHITECT}")
print(f"  Planner    → SambaNova: {SAMBANOVA_PLANNER}")
print(f"  Coder      → Cerebras:  {CEREBRAS_CODER}")
print(f"  Debugger   → Cerebras:  {CEREBRAS_CODER}")
print(f"  Reviewer   → Mistral:   {MISTRAL_REVIEWER}")
print(f"  Orchestr.  → Groq:      {GROQ_ORCHESTRATOR}")
print(f"  Validator  → Groq:      {GROQ_VALIDATOR}")
print(f"  Fallback   → Groq:      {GROQ_FALLBACK}")

print("\n── Live Ping Tests ──")

# NVIDIA NIM — Architect
if nim_client:
    test(f"NVIDIA NIM ({NIM_ARCHITECT})",
         lambda: ask_nim(PING, model=NIM_ARCHITECT))
else:
    print(f"  {SKIP} NVIDIA NIM — NIM_KEY not set")

# SambaNova — Planner
if sambanova_client:
    test(f"SambaNova ({SAMBANOVA_PLANNER})",
         lambda: ask_sambanova(PING, model=SAMBANOVA_PLANNER))
else:
    print(f"  {SKIP} SambaNova — SAMBANOVA_KEY not set")

# Cerebras — Coder + Debugger
if cerebras_client:
    test(f"Cerebras ({CEREBRAS_CODER})",
         lambda: ask_cerebras(PING, model=CEREBRAS_CODER))
else:
    print(f"  {SKIP} Cerebras — CEREBRAS_KEY not set")

# Mistral — Reviewer
if mistral_client:
    test(f"Mistral ({MISTRAL_REVIEWER})",
         lambda: ask_mistral(PING, model=MISTRAL_REVIEWER))
else:
    print(f"  {SKIP} Mistral — MISTRAL_KEY not set")

# Groq — Orchestrator model
if groq_client:
    test(f"Groq orchestrator ({GROQ_ORCHESTRATOR})",
         lambda: ask_groq(PING, model=GROQ_ORCHESTRATOR))
else:
    print(f"  {SKIP} Groq — GROQ_KEY not set")

# Groq — Validator model
if groq_client:
    test(f"Groq validator ({GROQ_VALIDATOR})",
         lambda: ask_groq(PING, model=GROQ_VALIDATOR))

# Groq — Universal fallback
if groq_client:
    test(f"Groq fallback ({GROQ_FALLBACK})",
         lambda: ask_groq(PING, model=GROQ_FALLBACK))

# ── Summary ───────────────────────────────────────────────────────────────────
passed = sum(1 for _, ok in results if ok)
failed = sum(1 for _, ok in results if not ok)
total  = len(results)

print(f"\n╔══════════════════════════════════════════╗")
print(f"║  Results: {passed}/{total} passed, {failed} failed{' ' * (18 - len(str(passed)) - len(str(total)) - len(str(failed)))}║")
print(f"╚══════════════════════════════════════════╝")

if failed:
    print("\nFailed providers:")
    for name, ok in results:
        if not ok:
            print(f"  ✗ {name}")
    print("\nCommon fixes:")
    print("  - Check your .env file has the correct key")
    print("  - Make sure the key has API access enabled")
    print("  - Check if the model ID is still valid at the provider's site")
    print("\nCurrent production model IDs (May 2026):")
    print("  Cerebras:  gpt-oss-120b")
    print("  SambaNova: gpt-oss-120b")
    print("  Groq:      llama-3.3-70b-versatile / llama-3.1-8b-instant")
    print("  Mistral:   codestral-latest")
    print("  NIM:       deepseek-ai/deepseek-r1")

print()
sys.exit(0 if failed == 0 else 1)
