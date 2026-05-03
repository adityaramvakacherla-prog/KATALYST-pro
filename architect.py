"""
architect.py — KATALYST Architect Agent
Runs BEFORE Planner. Takes a high-level prompt and produces a full
technical blueprint — screens, components, data flow, file map.
Uses DeepSeek-R1 on Groq (reasoning model — thinks step by step).
This is what makes Spotify-level apps possible: deep design before any code.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat
import agent_memory
from api_handler import ask_for_agent


ARCHITECT_PROMPT = """You are a senior software architect designing a complete application.

USER REQUEST: {prompt}

Your job is to produce a complete technical blueprint BEFORE any code is written.
Think deeply and thoroughly. This blueprint will guide all coding agents.

Produce a detailed blueprint covering:

1. APP OVERVIEW
   - What the app does in plain language
   - Target user and their main goal
   - Core features (list every major feature)

2. SCREENS / PAGES
   - List every screen/page/view with its purpose
   - What the user sees and can do on each

3. FILE ARCHITECTURE
   - Every file that needs to be created
   - What each file is responsible for
   - Which files depend on which (imports/uses)

4. DATA FLOW
   - How data moves through the app
   - What gets stored and where
   - Key functions/methods each file must have

5. UI THEME
   - Color scheme (specific hex codes)
   - Layout style (sidebar, cards, tabs, etc.)
   - Font and spacing style

6. TECHNICAL DECISIONS
   - Language and framework choices with reasons
   - Libraries needed and why
   - Any tricky implementation notes

Be specific. Name actual files like app.py, not "main file".
Name actual colors like #1DB954, not "green".
This document will be used directly to write code — vagueness causes bugs.

Return the blueprint as structured text. No JSON needed here. Be thorough.
"""


class Architect:

    def __init__(self):
        """Sets up architect with memory and chat access."""
        self.agent_name = "architect"

    def design(self, prompt):
        """
        Takes a user prompt and returns a complete technical blueprint string.
        Called by Planner before generating the task list.
        """
        agent_chat.log(self.agent_name, f"Designing architecture for: {prompt[:80]}...")

        full_prompt = ARCHITECT_PROMPT.format(prompt=prompt)
        blueprint = ask_for_agent(full_prompt, "architect")

        if not blueprint:
            agent_chat.log(
                self.agent_name,
                "Blueprint generation failed — Planner will proceed without it",
                message_type="error",
            )
            return None

        # Store blueprint so all agents can reference it
        agent_memory.store("app_blueprint", blueprint, agent_name=self.agent_name)
        agent_chat.log(
            self.agent_name,
            f"Blueprint ready — {len(blueprint)} chars — stored for all agents",
        )
        return blueprint
