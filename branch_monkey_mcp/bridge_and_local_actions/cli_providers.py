"""
CLI Provider abstraction for supporting multiple AI coding CLI tools.

Supports Claude Code CLI and OpenAI Codex CLI with a unified interface
for command building, output normalization, and availability checking.
"""

import json
import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class CliCommand:
    """A CLI command ready to execute."""
    args: List[str]
    env_overrides: Dict[str, None]  # Keys to remove from env


class CliProvider:
    """Base class for CLI tool providers."""

    name: str = ""
    display_name: str = ""
    install_hint: str = ""

    def is_available(self) -> Optional[str]:
        """Return path if CLI is installed, None otherwise."""
        raise NotImplementedError

    def build_run_command(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
    ) -> CliCommand:
        """Build command to run a new prompt."""
        raise NotImplementedError

    def build_resume_command(
        self,
        prompt: str,
        session_id: str,
    ) -> CliCommand:
        """Build command to resume a session."""
        raise NotImplementedError

    def build_oneshot_command(
        self,
        prompt: str,
    ) -> CliCommand:
        """Build command for a one-shot (non-streaming) invocation."""
        raise NotImplementedError

    def normalize_event(self, raw_json: dict) -> Optional[dict]:
        """Normalize a JSON output event to the common format.

        Common format follows Claude Code's stream-json structure:
        - {"type": "system", "subtype": "init", "session_id": "..."}
        - {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
        - {"type": "result", "result": "..."}

        Returns None to skip/filter the event.
        """
        return raw_json

    def extract_session_id(self, event: dict) -> Optional[str]:
        """Extract session ID from an init event, if present."""
        return None

    def is_noise(self, text: str) -> bool:
        """Return True if this stderr/non-JSON line should be filtered."""
        return False


class ClaudeCodeProvider(CliProvider):
    """Claude Code CLI provider."""

    name = "claude"
    display_name = "Claude Code"
    install_hint = "npm install -g @anthropic-ai/claude-code"

    def is_available(self) -> Optional[str]:
        return shutil.which("claude")

    def build_run_command(self, prompt, system_prompt=None):
        args = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions"
        ]
        if system_prompt:
            args.extend(["--append-system-prompt", system_prompt])

        return CliCommand(
            args=args,
            env_overrides={"ANTHROPIC_API_KEY": None, "CLAUDECODE": None}
        )

    def build_resume_command(self, prompt, session_id):
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--resume", session_id,
                "--dangerously-skip-permissions"
            ],
            env_overrides={"ANTHROPIC_API_KEY": None}
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--dangerously-skip-permissions"
            ],
            env_overrides={"ANTHROPIC_API_KEY": None}
        )

    def extract_session_id(self, event):
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
        return None

    def is_noise(self, text):
        noise_prefixes = ("warn:", "Warning:", "DeprecationWarning", "[DEP")
        noise_substrings = ("oven-sh/bun", "baseline.zip", "baseline build")
        return text.startswith(noise_prefixes) or any(s in text for s in noise_substrings)


class CodexProvider(CliProvider):
    """OpenAI Codex CLI provider."""

    name = "codex"
    display_name = "Codex CLI"
    install_hint = "npm install -g @openai/codex"

    def is_available(self) -> Optional[str]:
        return shutil.which("codex")

    def build_run_command(self, prompt, system_prompt=None):
        args = [
            "codex",
            "exec", prompt,
            "--full-auto",
            "--json",
        ]
        if system_prompt:
            args.extend(["--system-prompt", system_prompt])

        return CliCommand(
            args=args,
            env_overrides={}
        )

    def build_resume_command(self, prompt, session_id):
        # Codex uses `codex resume <session_id>` then continues with the prompt
        return CliCommand(
            args=[
                "codex",
                "exec", prompt,
                "--full-auto",
                "--json",
                "--resume", session_id,
            ],
            env_overrides={}
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "codex",
                "exec", prompt,
                "--full-auto",
                "--json",
            ],
            env_overrides={}
        )

    def normalize_event(self, raw_json):
        """Normalize Codex JSON output to Claude stream-json format.

        Codex emits newline-delimited JSON with different event types.
        We translate them to match Claude's stream-json format so the
        frontend streaming code works unchanged.
        """
        event_type = raw_json.get("type", "")

        # Map Codex init/session events
        if event_type in ("session.start", "session_start"):
            return {
                "type": "system",
                "subtype": "init",
                "session_id": raw_json.get("session_id", raw_json.get("id", "")),
                "provider": "codex"
            }

        # Map message/response events to assistant format
        if event_type in ("message", "response", "content"):
            text = raw_json.get("text", raw_json.get("content", raw_json.get("message", "")))
            if isinstance(text, dict):
                text = text.get("text", str(text))
            return {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": str(text)}]
                }
            }

        # Map tool use events
        if event_type in ("tool_use", "function_call", "tool_call"):
            return {
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": raw_json.get("name", raw_json.get("tool", "unknown")),
                        "input": raw_json.get("input", raw_json.get("arguments", {}))
                    }]
                }
            }

        # Map tool result events
        if event_type in ("tool_result", "function_result"):
            return {
                "type": "tool_result",
                "content": raw_json.get("output", raw_json.get("result", ""))
            }

        # Map completion/done events
        if event_type in ("done", "complete", "result", "session.end"):
            return {
                "type": "result",
                "result": raw_json.get("result", raw_json.get("output", raw_json.get("text", "")))
            }

        # Pass through unknown events as-is (the frontend can handle them)
        return raw_json

    def extract_session_id(self, event):
        # Check both raw and normalized formats
        if event.get("type") in ("session.start", "session_start"):
            return event.get("session_id", event.get("id"))
        # Also check normalized format
        if (event.get("type") == "system" and event.get("subtype") == "init"):
            return event.get("session_id")
        return None

    def is_noise(self, text):
        noise_prefixes = ("warn:", "Warning:", "DeprecationWarning", "[DEP", "npm warn")
        return text.startswith(noise_prefixes)


# --- Provider Registry ---

_PROVIDERS: Dict[str, CliProvider] = {
    "claude": ClaudeCodeProvider(),
    "codex": CodexProvider(),
}

# Default provider
DEFAULT_CLI = "claude"


def get_provider(name: Optional[str] = None) -> CliProvider:
    """Get a CLI provider by name. Falls back to default."""
    name = name or DEFAULT_CLI
    provider = _PROVIDERS.get(name)
    if not provider:
        raise ValueError(f"Unknown CLI provider: {name}. Available: {list(_PROVIDERS.keys())}")
    return provider


def get_available_providers() -> Dict[str, dict]:
    """Return info about all registered providers and their availability."""
    result = {}
    for name, provider in _PROVIDERS.items():
        path = provider.is_available()
        result[name] = {
            "name": name,
            "display_name": provider.display_name,
            "installed": path is not None,
            "path": path,
            "install_hint": provider.install_hint,
        }
    return result
