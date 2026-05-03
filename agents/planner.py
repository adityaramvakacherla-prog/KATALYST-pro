"""
planner.py — KATALYST Planner Agent
Converts natural language OR uploaded JSON into a structured project plan.
Now calls Architect first to get full technical blueprint.
Blueprint is injected into every task context so Coder understands the full app.

FIXES APPLIED:
- Fix 2: TASK_DESCRIPTION_RULES forces technical-spec-level task descriptions.
         Planner now produces full specs (hex colors, px sizes, font names, every
         interactive state) instead of vague one-liners.
- Fix 5: _write_context_packet calls _extract_task_blueprint() which pulls only the
         blueprint sections relevant to each specific task. Each Coder gets targeted
         signal, not a generic 5000-char dump where the relevant parts are buried.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_bus
import agent_memory
import agent_chat
from api_handler import ask_for_agent
from architect import Architect
from planner_validator import PlannerValidator


PLAN_FORMAT = """
{
  "project": { "name": "", "version": "1.0", "type": "", "description": "", "target_user": "" },
  "technical": { "language": "", "framework": "", "dependencies": [] },
  "phases": [
    {
      "phase_id": 1,
      "phase_name": "",
      "tasks": [
        {
          "task_id": "1.1",
          "file": "filename.py",
          "description": "",
          "expected_output": "",
          "status": "pending"
        }
      ]
    }
  ]
}
"""

TASK_DESCRIPTION_RULES = """
=== CRITICAL: TASK DESCRIPTION QUALITY ===

Every task description MUST be a technical specification, not a vague instruction.
You are writing a brief for a coder who cannot ask follow-up questions.

BANNED (produce bad minimal output):
  - "Create the main HTML file for the snake game"
  - "Build the calculator UI"
  - "Write the main Python app"
  - "Create a working dashboard"

REQUIRED format (write descriptions like these):

  HTML/JS example:
    "Create a single-file HTML/JS snake game. Canvas: 600x600px, centered on page.
    Body background: #0e1117. Snake: neon green #3dd68c, 20px grid cells with 2px gap.
    Food: #f05252 circle same cell size. Score: top-right corner, JetBrains Mono 18px bold,
    color #e2e8f0. Game-over overlay: rgba(0,0,0,0.85) full-canvas backdrop with blur(4px),
    GAME OVER heading 32px #f05252, final score 48px #e2e8f0 center, PLAY AGAIN button
    bg #7c6af7 hover #9b8dff, border-radius 8px, padding 12px 28px, Outfit font 14px.
    Controls: arrow keys AND WASD, active immediately on page load without clicking canvas.
    Game loop: requestAnimationFrame ONLY, never setInterval. Speed starts 150ms per tick,
    decreases 5ms every 5 points (minimum 60ms)."

  Python example:
    "Create a CLI tool in main.py. Entry: if __name__ == __main__ guard. Reads CSV path
    from sys.argv[1]. Parses with csv.DictReader. Per-column: total row count, mean (skip
    non-numeric), min, max, null/empty count. Prints table using str.ljust(20) aligned:
    Column | Count | Mean | Min | Max | Nulls. Raises SystemExit('File not found: X') if
    missing. Raises SystemExit('Not a CSV: X') if wrong extension. Zero third-party deps.
    Type hints on all functions."

For EVERY task you MUST specify:
  HTML/JS/CSS:
    - Every color as exact hex (#rrggbb)
    - Every dimension in exact px
    - Every font by exact name
    - Every state: hover, active, focus, disabled, empty, loading, error
    - Animation timing in ms and easing function
    - What user sees on first load
    - What happens on every interaction
    - What error and empty states look like visually
  Python:
    - Exact function signatures with type hints
    - Input source (sys.argv index, stdin, file path)
    - Output format (printed/returned/written)
    - Every error case and exact error message
    - Which stdlib modules to use
  ALL:
    - The specific observable result that proves the task is done

expected_output must also be specific:
  BAD:  "A working snake game"
  GOOD: "60fps canvas game. Snake moves on arrow/WASD immediately without clicking canvas.
         Score increments on food. Wall/self collision shows overlay with score and PLAY AGAIN.
         Dark theme #0e1117 applied. Speed visibly increases as score grows."
"""


class Planner:

    def run(self, input_data):
        """Main entry — accepts natural language string or project dict."""
        agent_chat.log("planner", "Planner started")

        if isinstance(input_data, str):
            agent_chat.log("planner", "Natural language input — running Architect first")

            # Step 1: Architect designs the full app blueprint
            architect = Architect()
            blueprint = architect.design(input_data)

            # Step 2: Convert prompt + blueprint to structured plan
            project = self._parse_natural_language(input_data, blueprint)
            if not project:
                agent_chat.log("planner", "Failed to generate plan", message_type="error")
                return None

        elif isinstance(input_data, dict):
            agent_chat.log("planner", "JSON input — validating")
            project = self._validate_json(input_data)
            if not project:
                agent_chat.log("planner", "Invalid JSON structure", message_type="error")
                return None
        else:
            agent_chat.log("planner", "Unknown input type", message_type="error")
            return None

        project = self._enrich(project)
        agent_memory.set_project_context(project)

        total = sum(len(p.get("tasks", [])) for p in project.get("phases", []))
        agent_chat.log("planner", f"Plan ready — {total} tasks — dependency graph built")
        agent_bus.post("planner", "orchestrator", "plan_ready", {"project": project})
        return project

    def _parse_natural_language(self, prompt, blueprint=None):
        """Sends prompt + blueprint to Planner model and returns structured project JSON."""
        blueprint_section = ""
        if blueprint:
            blueprint_section = (
                "\nARCHITECT BLUEPRINT — extract relevant sections and embed them "
                "in each task description (the Coder will NOT see this blueprint directly):\n"
                + blueprint
                + "\n"
            )

        system_prompt = (
            "You are a software project planner. Convert the user description into a structured"
            " project plan.\nOutput ONLY valid JSON matching this exact format"
            " — no markdown, no explanation:\n"
            + PLAN_FORMAT
            + "\n"
            + TASK_DESCRIPTION_RULES
            + blueprint_section
            + "\nAdditional rules:\n"
            + "- Create enough tasks to build the COMPLETE app from the blueprint\n"
            + "- Each task = one file. Never combine multiple files into one task\n"
            + "- For complex apps create 3-6 phases with multiple tasks each\n"
            + "- The Coder sees ONLY the task description — embed every spec detail there\n"
            + "- Extract blueprint sections relevant to each file and embed them in that task\n"
        )

        response = ask_for_agent(system_prompt + "\n\nUser request: " + prompt, "planner")
        if not response:
            return None

        project = self._extract_json(response)
        if not project:
            agent_chat.log("planner", "JSON parse failed — retrying", message_type="error")
            retry = (
                "Return ONLY a JSON object. No text. No markdown.\n\nPlan this: "
                + prompt
                + "\n\nFormat:\n"
                + PLAN_FORMAT
            )
            response = ask_for_agent(retry, "planner")
            project = self._extract_json(response) if response else None

        if not project:
            return None

        # Validate task description quality—retry once with targeted feedback if too vague
        validator = PlannerValidator()
        valid, issues = validator.validate(project)
        if not valid:
            agent_chat.log(
                "planner",
                f"Plan rejected — {len(issues)} vague task descriptions — retrying with feedback",
                message_type="error",
            )
            retry_prompt = validator.build_retry_prompt(prompt, issues)
            retry_response = ask_for_agent(
                system_prompt + "\n\nUSER REQUEST: " + retry_prompt, "planner"
            )
            if retry_response:
                retry_project = self._extract_json(retry_response)
                if retry_project:
                    project = retry_project
                    agent_chat.log("planner", "Plan retry succeeded — using improved descriptions")

        return project

    def _extract_json(self, text):
        """Extracts JSON from AI response — handles markdown fences and prose."""
        if not text:
            return None
        text = text.strip()

        if "```" in text:
            for part in text.split("```"):
                part = part.strip()
                if part.lower().startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    try:
                        return json.loads(part)
                    except Exception:
                        pass

        try:
            return json.loads(text)
        except Exception:
            pass

        # Find outermost { }
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        return json.loads(text[start:i+1])
                    except Exception:
                        break
        return None

    def _validate_json(self, project_dict):
        """Checks required fields and normalises task statuses."""
        if "project" not in project_dict or "phases" not in project_dict:
            return None
        for phase in project_dict.get("phases", []):
            for task in phase.get("tasks", []):
                task.setdefault("status", "pending")
                task.setdefault("task_id", f"{phase.get('phase_id',0)}.{phase['tasks'].index(task)+1}")
        return project_dict

    def _detect_complexity(self, prompt):
        """Small task vs full project detection."""
        small = ["function", "script", "snippet", "class", "fix", "single", "one file"]
        if any(w in prompt.lower() for w in small) and len(prompt.split()) < 30:
            return "small_task"
        return "full_project"

    def _enrich(self, project):
        """Adds dependency graph to each task and writes context packets."""
        graph = self._build_dependency_graph(project)
        blueprint = agent_memory.get("app_blueprint") or ""

        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                tid = task["task_id"]
                deps = graph.get(tid, {"needs": [], "reads": [], "extends": []})
                task["needs"]   = deps["needs"]
                task["reads"]   = deps["reads"]
                task["extends"] = deps["extends"]
                context = self._write_context_packet(task, project, phase, blueprint)
                agent_memory.store(f"context:{tid}", context, agent_name="planner")

        order = self._build_dependency_order(project, graph)
        project["dependency_order"] = order
        agent_memory.store("dependency_order", order, agent_name="planner")
        return project

    def _build_dependency_graph(self, project):
        """Analyses task descriptions to determine needs/reads/extends."""
        graph = {}
        all_tasks = []

        for phase in project.get("phases", []):
            for task in phase.get("tasks", []):
                all_tasks.append(task)
                graph[task["task_id"]] = {"needs": [], "reads": [], "extends": []}

        file_creators = {t["file"]: t["task_id"] for t in all_tasks if "file" in t}

        for task in all_tasks:
            desc = task.get("description", "").lower()
            tid  = task["task_id"]
            for filename, creator_tid in file_creators.items():
                if creator_tid == tid:
                    continue
                base = os.path.splitext(filename)[0].lower()
                if filename.lower() in desc or base in desc:
                    if creator_tid not in graph[tid]["needs"]:
                        graph[tid]["needs"].append(creator_tid)
                    if filename not in graph[tid]["reads"]:
                        graph[tid]["reads"].append(filename)

            extend_kw = ["add to", "extend", "existing", "append to", "update"]
            if any(kw in desc for kw in extend_kw):
                if task.get("file") in file_creators:
                    creator = file_creators[task["file"]]
                    if creator != tid and creator not in graph[tid]["needs"]:
                        graph[tid]["needs"].append(creator)
                        graph[tid]["extends"].append(task["file"])

        return graph

    def _build_dependency_order(self, project, graph):
        """Returns flat ordered list of task_ids respecting dependencies."""
        all_ids   = [t["task_id"] for p in project.get("phases", []) for t in p.get("tasks", [])]
        ordered   = []
        remaining = list(all_ids)
        max_passes = len(all_ids) * 2

        passes = 0
        while remaining and passes < max_passes:
            passes += 1
            for tid in list(remaining):
                if all(n in ordered for n in graph.get(tid, {}).get("needs", [])):
                    ordered.append(tid)
                    remaining.remove(tid)

        ordered.extend(remaining)
        return ordered

    def _write_context_packet(self, task, project, phase, blueprint=""):
        """
        Builds context packet for Coder.
        Extracts task-specific blueprint sections so the Coder gets targeted info,
        not a generic blob where relevant content is buried.
        """
        coder_rules = ""
        rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CODER_RULES.md")
        if os.path.exists(rules_path):
            with open(rules_path) as f:
                coder_rules = f.read()

        dependency_files = {}
        for filename in task.get("reads", []):
            file_content = agent_memory.get_file_content(filename)
            if file_content:
                dependency_files[filename] = file_content

        # Task-specific extract — not a full generic dump
        task_blueprint = self._extract_task_blueprint(blueprint, task)

        return {
            "task_id":          task["task_id"],
            "file_to_create":   task.get("file", ""),
            "description":      task.get("description", ""),
            "expected_output":  task.get("expected_output", ""),
            "needs":            task.get("needs", []),
            "reads":            task.get("reads", []),
            "extends":          task.get("extends", []),
            "dependency_files": dependency_files,
            "project_name":     project["project"]["name"],
            "project_desc":     project["project"].get("description", ""),
            "phase_name":       phase.get("phase_name", ""),
            "coder_rules":      coder_rules,
            "app_blueprint":    task_blueprint,
        }

    def _extract_task_blueprint(self, blueprint, task):
        """
        Returns the parts of the blueprint most relevant to this specific task.
        Always includes: UI THEME, FILE ARCHITECTURE sections.
        Also includes sections whose heading matches the task's file or keywords.
        Capped at 4000 chars — enough detail without overwhelming the Coder prompt.
        """
        if not blueprint:
            return ""

        filename  = task.get("file", "").lower().replace("_", " ").replace("-", " ")
        desc      = task.get("description", "").lower()
        bp_lines  = blueprint.splitlines()

        # Detect section headings: numbered lines or ALL-CAPS short lines
        sections = []
        for i, line in enumerate(bp_lines):
            stripped = line.strip()
            if re.match(r"^[0-9]+[.)]\s+[A-Z]", stripped):
                sections.append((i, stripped.lower()))
            elif (3 < len(stripped) < 70
                  and stripped == stripped.upper()
                  and any(c.isalpha() for c in stripped)):
                sections.append((i, stripped.lower()))

        if not sections:
            # No detectable headings — return first 3000 chars
            return blueprint[:3000]

        # Sections always included regardless of task
        priority_kw = {
            "ui theme", "theme", "color", "colour", "data flow",
            "file architecture", "file map", "technical decisions",
            "screens", "overview", "app overview",
        }
        # Words from this specific task
        task_words = set(re.sub(r"[^a-z0-9 ]", " ", filename + " " + desc).split())

        relevant = set()
        for i, (line_idx, heading) in enumerate(sections):
            if any(kw in heading for kw in priority_kw):
                relevant.add(i)
            elif any(w in heading for w in task_words if len(w) > 3):
                relevant.add(i)

        if not relevant:
            return blueprint[:3000]

        parts = []
        for i in sorted(relevant):
            start = sections[i][0]
            end   = sections[i + 1][0] if i + 1 < len(sections) else len(bp_lines)
            text  = "\n".join(bp_lines[start:end]).strip()
            if text:
                parts.append(text)

        combined = "\n\n".join(parts)
        return combined[:4000] if len(combined) > 4000 else combined

    def replan(self, project, failed_task, error):
        """Updates context with failure info after a task fails."""
        tid = failed_task["task_id"]
        agent_chat.log("planner", f"Replanning context for task {tid}", task_id=tid)
        context = agent_memory.get(f"context:{tid}") or {}
        context["previous_error"] = error
        agent_memory.store(f"context:{tid}", context, agent_name="planner")
        return context
