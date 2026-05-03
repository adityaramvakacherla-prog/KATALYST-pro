"""
health_monitor.py — KATALYST Health Monitor
Watches agent threads, detects crashes, handles rate limits, pings API providers.
Started as a background thread by Orchestrator during project runs.
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_chat
import agent_bus

# How often to check agent threads (seconds)
HEALTH_CHECK_INTERVAL = 10

# Rate limit thresholds before switching provider
RATE_LIMIT_SWITCH_THRESHOLD = 3

# How long to back off after hitting Groq rate limit (seconds)
GROQ_RATE_LIMIT_PAUSE = 60


class HealthMonitor:

    def __init__(self):
        """Initializes counters and tracking dicts."""
        self.watched_agents   = {}   # agent_name → thread
        self.crash_counts     = {}   # agent_name → int
        self.disabled_agents  = set()
        self.rate_limit_hits  = {"cerebras": 0, "groq": 0}
        self.provider_paused  = {"cerebras": False, "groq": False}
        self._running         = False
        self._thread          = None
        self.agent_name       = "system"

    def watch_agent(self, agent_name, thread):
        """Registers a thread to be monitored. Called by Orchestrator when spawning agents."""
        self.watched_agents[agent_name] = thread
        self.crash_counts.setdefault(agent_name, 0)
        agent_chat.log(self.agent_name, f"Now watching agent thread: {agent_name}")

    def start(self):
        """Starts the health monitor in a background daemon thread."""
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        agent_chat.log(self.agent_name, "Health monitor started")

    def stop(self):
        """Signals the monitor loop to stop."""
        self._running = False
        agent_chat.log(self.agent_name, "Health monitor stopping")

    def _loop(self):
        """Main monitoring loop — checks all watched threads periodically."""
        while self._running:
            self._check_all_agents()
            self._check_api_health()
            time.sleep(HEALTH_CHECK_INTERVAL)

    def _check_all_agents(self):
        """Checks each watched thread — detects death, increments crash counts."""
        for agent_name, thread in list(self.watched_agents.items()):
            if agent_name in self.disabled_agents:
                continue
            if thread and not thread.is_alive():
                self.crash_counts[agent_name] = self.crash_counts.get(agent_name, 0) + 1
                count = self.crash_counts[agent_name]

                if count >= 3:
                    # Disable after 3 crashes
                    self.disabled_agents.add(agent_name)
                    agent_chat.log(
                        self.agent_name,
                        f"Agent {agent_name} crashed {count} times — DISABLED",
                        message_type="error",
                    )
                    agent_bus.post(
                        sender       = self.agent_name,
                        recipient    = "orchestrator",
                        message_type = "agent_disabled",
                        content      = {"agent": agent_name, "crashes": count},
                    )
                else:
                    agent_chat.log(
                        self.agent_name,
                        f"Agent {agent_name} thread died (crash #{count}) — flagging orchestrator",
                        message_type="error",
                    )
                    agent_bus.post(
                        sender       = self.agent_name,
                        recipient    = "orchestrator",
                        message_type = "agent_crashed",
                        content      = {"agent": agent_name, "crash_count": count},
                    )
                # Remove from watch list — orchestrator will re-register if it respawns
                del self.watched_agents[agent_name]

    def restart_agent(self, agent_name):
        """
        Attempts to respawn a dead agent by posting a restart signal to the bus.
        Orchestrator reads this and re-spawns the agent thread.
        """
        agent_chat.log(
            self.agent_name,
            f"Attempting restart of agent: {agent_name}",
            message_type="error",
        )
        agent_bus.post(
            sender       = self.agent_name,
            recipient    = "orchestrator",
            message_type = "restart_agent",
            content      = {"agent": agent_name, "crash_count": self.crash_counts.get(agent_name, 0)},
        )

    def _check_api_health(self):
        """Private alias called by the monitor loop."""
        return self.check_api_health()

    def check_api_health(self):
        """
        Checks API provider availability using client-presence only — zero API calls.
        Live pings are never made; if a provider had rate limit hits we trust the
        backoff/resume logic in handle_rate_limit() to clear them.
        Logs result once per check interval.
        """
        try:
            from api_handler import cerebras_client, groq_client
            cerebras_ok = cerebras_client is not None and not self.provider_paused.get("cerebras")
            groq_ok     = groq_client     is not None and not self.provider_paused.get("groq")
        except ImportError:
            cerebras_ok = False
            groq_ok     = False

        agent_chat.log(
            self.agent_name,
            f"API health — Cerebras: {'✓' if cerebras_ok else '✗'} | Groq: {'✓' if groq_ok else '✗'}"
        )
        return {"cerebras": cerebras_ok, "groq": groq_ok}

    def _ping_cerebras(self):
        """Kept for backwards compatibility — no longer called."""
        try:
            from api_handler import cerebras_client
            return cerebras_client is not None
        except Exception:
            return False

    def _ping_groq(self):
        """Kept for backwards compatibility — no longer called."""
        try:
            from api_handler import groq_client
            return groq_client is not None
        except Exception:
            return False

    def handle_rate_limit(self, provider, retry_after=None):
        """
        Called by api_handler when a 429 is received.
        Tracks hits, backs off, switches provider after threshold.
        """
        self.rate_limit_hits[provider] = self.rate_limit_hits.get(provider, 0) + 1
        hits = self.rate_limit_hits[provider]

        # Exponential backoff: 2, 4, 8, 16, 32 seconds max
        wait = min(2 ** hits, 32)
        agent_chat.log(
            self.agent_name,
            f"Rate limit hit on {provider} (hit #{hits}) — backing off {wait}s",
            message_type="error",
        )
        time.sleep(wait)

        if provider == "cerebras" and hits >= RATE_LIMIT_SWITCH_THRESHOLD:
            agent_chat.log(
                self.agent_name,
                f"Cerebras rate limited {hits} times — switching session to Groq only",
                message_type="error",
            )
            self.provider_paused["cerebras"] = True

        if provider == "groq" and hits >= RATE_LIMIT_SWITCH_THRESHOLD:
            agent_chat.log(
                self.agent_name,
                f"Groq rate limited {hits} times — pausing {GROQ_RATE_LIMIT_PAUSE}s",
                message_type="error",
            )
            self.provider_paused["groq"] = True
            time.sleep(GROQ_RATE_LIMIT_PAUSE)
            self.provider_paused["groq"] = False
            self.rate_limit_hits["groq"] = 0
            agent_chat.log(self.agent_name, "Groq pause complete — resuming")

    def is_provider_available(self, provider):
        """Returns True if this provider is not currently paused due to rate limits."""
        return not self.provider_paused.get(provider, False)

    def reset_rate_limits(self):
        """Resets all rate limit counters — called when a project completes."""
        self.rate_limit_hits  = {"cerebras": 0, "groq": 0}
        self.provider_paused  = {"cerebras": False, "groq": False}
        agent_chat.log(self.agent_name, "Rate limit counters reset")

    def get_status(self):
        """Returns a summary dict for dashboard display."""
        return {
            "watched":          list(self.watched_agents.keys()),
            "disabled":         list(self.disabled_agents),
            "crash_counts":     dict(self.crash_counts),
            "rate_limit_hits":  dict(self.rate_limit_hits),
            "providers_paused": dict(self.provider_paused),
        }


# Module-level singleton — imported by Orchestrator
monitor = HealthMonitor()
