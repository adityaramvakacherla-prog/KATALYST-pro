# ⚡ KATALYST — AI Build System

> A multi-agent AI pipeline that turns a plain-English description into a complete, reviewed, tested, and packaged codebase. Built by a student. Runs on free API keys.

---

## What is this, really?

KATALYST is something I built because I got frustrated watching AI assistants produce half-baked code with no quality control. The idea is simple: instead of one AI writing everything and hoping for the best, a whole team of specialised agents works on it — one writes, one reviews, one debugs, one tests, one judges the visuals.

Each agent has a specific job and can't skip it. The code doesn't get saved to disk until it passes every gate. If it fails, the Debugger fixes exactly what the Reviewer flagged, and it goes back through the line again. This keeps iterating until the code is genuinely good or the attempt limit is hit.

It's not perfect. But it produces noticeably better output than asking a single model to "write me an app."

---

## How the pipeline works

```
Your prompt
    ↓
Architect  — designs the full technical blueprint
    ↓
Planner    — breaks the blueprint into tasks with specific file assignments
    ↓
Coder ×N   — writes each file (up to 3 in parallel)
    ↓
Reviewer   — adversarial checklist review, 8/10 minimum to pass
    ↓
Debugger   — fixes only what the Reviewer flagged (up to 3 attempts)
    ↓
Validator  — syntax check + AI sanity check
    ↓
Tester     — runtime execution, function existence, placeholder detection
    ↓
Visual Tester — headless Chromium screenshot + vision model QA (HTML/JS)
    ↓
/output    — saved to disk only if all gates pass
```

The Orchestrator sits above all of this, reads the agent bus, routes messages, tracks timeouts, and writes the final files. The dashboard gives you a live view of everything happening.

---

## What it can build

- Single-page HTML/JS apps and games
- Python CLI tools and scripts
- Flask and FastAPI backends
- Streamlit data apps
- Multi-file projects with dependency ordering

Once built, it can package the output as a Docker image, standalone EXE, Android APK, or a clean ZIP with a README.

---

## Getting started

### What you need

- Python 3.11+
- At least one free API key (Groq is the easiest to get — [console.groq.com](https://console.groq.com))
- Optional but recommended: Cerebras, Mistral, SambaNova, NVIDIA NIM keys for better per-agent routing

### Setup

```bash
# Clone the repo
git clone https://github.com/yourusername/KATALYST.git
cd KATALYST

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up your API keys
cp .env.example .env
# Then edit .env and add your keys
```

Your `.env` file should look like this:

```
GROQ_KEY=gsk_...
CEREBRAS_KEY=csk-...       # optional but recommended
MISTRAL_KEY=...            # optional — used by Reviewer
SAMBANOVA_KEY=...          # optional — used by Planner
NIM_KEY=...                # optional — used by Architect
```

You can run the whole system with only a `GROQ_KEY`. The other keys unlock better per-agent model routing (each agent gets a model suited to its specific job) but Groq will handle everything as a fallback.

### Running it

```bash
python server.py
```

Then open [http://localhost:5000](http://localhost:5000) in your browser.

---

## Using the dashboard

**Input tab** — describe what you want to build in plain English, or upload a project JSON if you already have a structured plan. You can also generate a single file instantly with "Generate Code."

**Dashboard tab** — live CPU/RAM charts, task progress bar, activity log, and a list of all generated output files. The floating island at the bottom lets you pause, stop, or reset a running build.

**Agents tab** — see which agent is active, read the live conversation between agents, and use the control panel to re-run specific tasks or inject messages.

**Labs tab** — paste any code and run it immediately. Supports Python, HTML/JS, Bash, and JavaScript (via Node.js). Generated HTML files render in a live preview pane. You can also load any output file, edit it in the scratchpad, and re-run it.

**Settings** — configure API keys, model selection, parallel coder count, poll interval, and toggle individual agents on or off.

---

## Provider routing

When all keys are configured, each agent uses the model best suited to its role:

| Agent | Provider | Model | Why |
|---|---|---|---|
| Architect | NVIDIA NIM | DeepSeek-R1 | Reasoning model — thinks step by step |
| Planner | SambaNova | GPT-OSS 120B | Fast large model — good at structured JSON |
| Coder | Cerebras | GPT-OSS 120B | Very fast inference — good for long files |
| Reviewer | Mistral | Codestral | Purpose-built code review model |
| Orchestrator | Groq | Llama 3.3 70B | Fast routing decisions |
| Validator | Groq | Llama 3.1 8B | Tiny model for simple sanity checks |
| Debugger | Cerebras | GPT-OSS 120B | Same as Coder — needs full code context |

With only a Groq key, all agents fall back to `llama-3.3-70b-versatile`. It still works — it's just slower and less specialised.

---

## Running the test suite

```bash
python test_providers.py    # pings every configured provider
python test_core.py         # full integration test suite
```

`test_core.py` runs infrastructure tests (agent bus, memory, chat log), API connection tests, individual agent tests (Reviewer, Debugger, Coder), and a full single-task pipeline test end to end.

---

## Project JSON format

If you want to upload a pre-planned project instead of describing it in plain English, the format is:

```json
{
  "project": {
    "name": "My App",
    "version": "1.0",
    "type": "web",
    "description": "What the app does"
  },
  "technical": {
    "language": "HTML/JS",
    "framework": "none",
    "dependencies": []
  },
  "phases": [
    {
      "phase_id": 1,
      "phase_name": "Core",
      "tasks": [
        {
          "task_id": "1.1",
          "file": "index.html",
          "description": "Build a snake game. Canvas 600x600px...",
          "expected_output": "Playable snake game with score display",
          "status": "pending"
        }
      ]
    }
  ]
}
```

The more specific your task descriptions (exact hex colors, pixel sizes, font names, interaction states), the better the output. The Planner generates these specs automatically from a plain-English prompt, but you can write them yourself for precise control.

---

## Packaging output

Once a build completes, the Package panel appears on the dashboard. Choose a target:

- **ZIP** — clean archive with a generated README explaining how to run the app
- **Docker** — Dockerfile + docker-compose.yml (no Docker installation required on your machine)
- **EXE** — standalone executable via PyInstaller (Linux/Mac → Linux/Mac binary)
- **APK** — Android package via Capacitor (HTML/JS apps) or Buildozer (Python/Kivy)

APK builds require Node.js, Java 17+, and about 4GB of disk space for the Android SDK. The first build takes 20–45 minutes. Subsequent builds use the cached SDK and take 3–8 minutes.

---

## File structure

```
KATALYST/
├── server.py              — Flask backend, dashboard API, spawns task runner
├── orchestrator.py        — Pipeline coordinator, agent bus reader
├── planner.py             — Prompt → structured project JSON
├── architect.py           — Technical blueprint designer
├── coder.py               — File writer
├── reviewer.py            — Adversarial code reviewer
├── debugger.py            — Targeted bug fixer
├── validator.py           — Syntax + sanity checker
├── tester.py              — Runtime executor and placeholder detector
├── visual_tester.py       — Headless browser + vision model QA
├── agent_bus.py           — Thread-safe message queue between agents
├── agent_memory.py        — Shared knowledge base (files, lessons, context)
├── agent_chat.py          — Structured conversation log for dashboard
├── api_handler.py         — All AI provider clients and routing logic
├── health_monitor.py      — Thread watcher and rate limit handler
├── packager.py            — Docker/EXE/APK/ZIP output packaging
├── labs_runner.py         — Code execution backend for Labs page
├── retry_engine.py        — Retry loop with escalation to expert prompt
├── error_handler.py       — Temp file execution and error classification
├── logger.py              — Activity log writer
├── dashboard_home.html    — Single-file frontend dashboard
├── CODER_RULES.md         — Quality standard injected into every Coder prompt
├── settings.json          — Runtime configuration
├── requirements.txt       — Python dependencies
├── output/                — All generated files land here
├── memory/                — Agent memory, lessons, bus messages
└── logs/                  — Activity log, live feed, control signals
```

---

## Known limitations

- **APK builds are Linux/Mac only.** Windows users need WSL2.
- **Visual Tester requires Playwright.** Run `playwright install chromium` after setup to enable it. It degrades gracefully (skips visual check) if unavailable.
- **`fcntl` is used for file locking in `agent_bus.py`.** This is a POSIX-only call that will break on Windows. A cross-platform fallback using threading locks is planned.
- **Rate limits.** Free tier API keys have tight limits. The health monitor handles backoff automatically, but a 20-task project at peak hours might hit Groq's free tier ceiling. The system backs off and retries rather than crashing.
- **Long builds can take 30–90 minutes.** Each task goes through 7+ agents, each making at least one API call. A 15-task project = 100+ API calls minimum.

---

## Tips for better output

**Be specific in your prompt.** The Planner turns your words into task descriptions, and those task descriptions go directly to the Coder. "Build a todo app" produces weaker specs than "Build a dark-themed todo app in HTML/JS with add, complete, and delete. Background #0e1117, accent color #7c6af7, Inter font."

**Check the Agents tab during a build.** You can see exactly what each agent is saying and whether any are stuck. The control panel lets you re-run individual tasks without restarting the whole project.

**Use Labs to test individual files.** If a generated file looks wrong, paste it into the Labs scratchpad, run it, and use the Feature Modifier chat to ask for a specific change. The modifier rewrites only what you ask — everything else stays the same.

**Lessons accumulate across projects.** Every error the Debugger fixes is stored in `memory/lessons.json`. Future builds automatically avoid patterns that caused problems before. The more you use it, the better it gets on your specific setup.

---

## A note on this being a student project

I built this over time while learning how agents, APIs, and multi-threaded Python actually work. Some parts of the code are cleaner than others. The architecture holds together — separate concerns, message-passing between agents, proper error handling in the critical paths — but there are rough edges.

If you find a bug or have a suggestion, open an issue. I'm actively learning and actively improving this.

---

## License

MIT — use it, break it, build on it.
