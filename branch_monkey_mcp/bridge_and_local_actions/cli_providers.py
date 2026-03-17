"""
CLI Provider abstraction for supporting multiple AI coding CLI tools.

Supports Claude Code CLI and OpenAI Codex CLI with a unified interface
for command building, output normalization, and availability checking.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class CliCommand:
    """A CLI command ready to execute."""
    args: List[str]
    env_overrides: Dict[str, None]  # Keys to remove from env
    env_inject: Dict[str, str] = None  # Keys to add/set in env

    def __post_init__(self):
        if self.env_inject is None:
            self.env_inject = {}


class CliProvider:
    """Base class for CLI tool providers."""

    name: str = ""
    display_name: str = ""
    install_hint: str = ""
    install_cmd: List[str] = []  # e.g. ["npm", "install", "-g", "package-name"]
    api_key_env: str = ""       # Env var name for API key (e.g. ANTHROPIC_API_KEY)
    api_key_config: str = ""    # Config key in ~/.kompany/config.json

    def is_available(self) -> Optional[str]:
        """Return path if CLI is installed, None otherwise."""
        raise NotImplementedError

    def install(self) -> dict:
        """Install the CLI. Returns {success: bool, output: str}."""
        if not self.install_cmd:
            return {"success": False, "output": "No install command configured"}
        try:
            result = subprocess.run(
                self.install_cmd,
                capture_output=True, text=True, timeout=120,
            )
            success = result.returncode == 0
            output = result.stdout + result.stderr
            return {"success": success, "output": output.strip()}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "Install timed out"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def get_auth_status(self) -> dict:
        """Check authentication status.

        Returns dict with:
          - authenticated: bool
          - method: str - 'api_key', 'oauth', 'none'
          - detail: str - email, key hint, or error message
        """
        return {"authenticated": False, "method": "none", "detail": "Not implemented"}

    def get_auth_env(self) -> Dict[str, str]:
        """Return env vars to inject for authentication.

        Checks: 1) API key in ~/.kompany/config.json, 2) API key in env.
        Returns dict of env vars to set when spawning the CLI process.
        """
        if self.api_key_config:
            config = _load_config()
            stored_key = config.get(self.api_key_config)
            if stored_key:
                return {self.api_key_env: stored_key}
        return {}

    def set_api_key(self, key: str):
        """Store an API key in persistent config."""
        if not self.api_key_config:
            raise ValueError(f"{self.display_name} does not support API key auth")
        _save_config({self.api_key_config: key})

    def clear_api_key(self):
        """Remove stored API key from config."""
        if self.api_key_config:
            config = _load_config()
            config.pop(self.api_key_config, None)
            _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)

    def start_device_auth(self) -> Optional[dict]:
        """Start device auth flow. Returns {url, code} or None if not supported."""
        return None

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
    install_cmd = ["npm", "install", "-g", "@anthropic-ai/claude-code"]
    api_key_env = "ANTHROPIC_API_KEY"
    api_key_config = "anthropic_api_key"

    def is_available(self) -> Optional[str]:
        return shutil.which("claude")

    def get_auth_status(self) -> dict:
        """Check Claude Code auth: try `claude auth status` (JSON output)."""
        # 1. Check for stored API key in our config
        config = _load_config()
        if config.get(self.api_key_config):
            key = config[self.api_key_config]
            hint = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key ({hint})"}

        # 2. Check for API key in environment
        env_key = os.environ.get(self.api_key_env)
        if env_key:
            hint = env_key[:8] + "..." + env_key[-4:] if len(env_key) > 12 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key from env ({hint})"}

        # 3. Check CLI's own auth via `claude auth status`
        path = self.is_available()
        if not path:
            return {"authenticated": False, "method": "none", "detail": "CLI not installed"}

        try:
            result = subprocess.run(
                ["claude", "auth", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get("loggedIn"):
                    email = data.get("email", "")
                    method = data.get("authMethod", "oauth")
                    sub = data.get("subscriptionType", "")
                    detail = f"{email}" + (f" ({sub})" if sub else "")
                    return {"authenticated": True, "method": method, "detail": detail}
            return {"authenticated": False, "method": "none", "detail": "Not signed in"}
        except Exception as e:
            return {"authenticated": False, "method": "none", "detail": str(e)}

    def start_device_auth(self) -> Optional[dict]:
        """Start Claude device auth — opens browser via `claude auth login`."""
        import webbrowser

        path = self.is_available()
        if not path:
            return None

        try:
            # claude auth login is interactive — spawn it detached.
            # It opens the browser automatically for Anthropic OAuth.
            subprocess.Popen(
                ["claude", "auth", "login"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            return {
                "type": "browser",
                "message": "Opening browser for Anthropic sign-in...",
                "url": "https://console.anthropic.com",
            }
        except Exception:
            return None

    def _build_env_overrides(self) -> Dict[str, None]:
        """Determine which env vars to strip.

        If user has a stored API key in our config, DON'T strip it — we'll inject it.
        If no stored key, strip ANTHROPIC_API_KEY so Claude uses its own OAuth.
        """
        config = _load_config()
        if config.get(self.api_key_config):
            # User has stored an API key — don't strip, we'll inject it
            return {"CLAUDECODE": None}
        return {"ANTHROPIC_API_KEY": None, "CLAUDECODE": None}

    def _build_env_inject(self) -> Dict[str, str]:
        """Return env vars to inject (stored API key)."""
        return self.get_auth_env()

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
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
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
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--dangerously-skip-permissions"
            ],
            env_overrides=self._build_env_overrides(),
            env_inject=self._build_env_inject(),
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
    install_cmd = ["npm", "install", "-g", "@openai/codex"]
    api_key_env = "OPENAI_API_KEY"
    api_key_config = "openai_api_key"

    def is_available(self) -> Optional[str]:
        return shutil.which("codex")

    def set_api_key(self, key: str):
        """Store API key in our config AND register with `codex login --with-api-key`."""
        super().set_api_key(key)
        # Also register with Codex's own auth system
        if self.is_available():
            try:
                subprocess.run(
                    ["codex", "login", "--with-api-key"],
                    input=key, text=True, capture_output=True, timeout=10,
                )
            except Exception:
                pass  # Best effort — env var injection still works as fallback

    def get_auth_status(self) -> dict:
        """Check Codex auth: stored API key or `codex login status`."""
        # 1. Check for stored API key in our config
        config = _load_config()
        if config.get(self.api_key_config):
            key = config[self.api_key_config]
            hint = key[:7] + "..." + key[-4:] if len(key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key ({hint})"}

        # 2. Check for API key in environment
        env_key = os.environ.get(self.api_key_env)
        if env_key:
            hint = env_key[:7] + "..." + env_key[-4:] if len(env_key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key from env ({hint})"}

        # 3. Check CLI's own auth via `codex login status`
        path = self.is_available()
        if not path:
            return {"authenticated": False, "method": "none", "detail": "CLI not installed"}

        try:
            result = subprocess.run(
                ["codex", "login", "status"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                output = result.stdout.strip()
                return {"authenticated": True, "method": "oauth", "detail": output or "Signed in"}
            return {"authenticated": False, "method": "none", "detail": "Not signed in"}
        except Exception as e:
            return {"authenticated": False, "method": "none", "detail": str(e)}

    def start_device_auth(self) -> Optional[dict]:
        """Start Codex device auth — runs `codex login --device-auth` and captures URL+code."""
        import re
        import time

        path = self.is_available()
        if not path:
            return None

        try:
            # Use Popen so the process stays alive while user completes auth in browser.
            # Read lines until we capture the URL and code, then return.
            proc = subprocess.Popen(
                ["codex", "login", "--device-auth"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )

            output = ""
            end_time = time.time() + 8
            while time.time() < end_time:
                line = proc.stdout.readline()
                if not line:
                    break
                output += line
                # Stop once we see the device code pattern
                if re.search(r'[A-Z0-9]{4,5}-[A-Z0-9]{4,5}', line):
                    break

            # Strip ANSI escape codes
            clean = re.sub(r'\x1b\[[0-9;]*m', '', output)

            url_match = re.search(r'(https://\S+)', clean)
            code_match = re.search(r'([A-Z0-9]{4,5}-[A-Z0-9]{4,5})', clean)

            if url_match:
                # Don't kill the process — it needs to stay alive to complete
                # the auth flow when the user approves in the browser.
                return {
                    "type": "device_code",
                    "url": url_match.group(1),
                    "code": code_match.group(1) if code_match else None,
                    "message": "Visit the URL and enter the code to sign in",
                }

            proc.kill()
            return None
        except Exception:
            return None

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
            env_overrides={},
            env_inject=self.get_auth_env(),
        )

    def build_resume_command(self, prompt, session_id):
        return CliCommand(
            args=[
                "codex",
                "exec", prompt,
                "--full-auto",
                "--json",
                "--resume", session_id,
            ],
            env_overrides={},
            env_inject=self.get_auth_env(),
        )

    def build_oneshot_command(self, prompt):
        return CliCommand(
            args=[
                "codex",
                "exec", prompt,
                "--full-auto",
                "--json",
            ],
            env_overrides={},
            env_inject=self.get_auth_env(),
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


class GrokProvider(CliProvider):
    """xAI Grok CLI provider (runs Claude Code with Grok models via proxy)."""

    name = "grok"
    display_name = "Grok"
    install_hint = "npm install -g grok-cli"
    install_cmd = ["npm", "install", "-g", "grok-cli"]
    api_key_env = "XAI_API_KEY"
    api_key_config = "xai_api_key"

    def is_available(self) -> Optional[str]:
        return shutil.which("grok")

    def get_auth_status(self) -> dict:
        """Check Grok auth: stored API key or XAI_API_KEY env var."""
        # 1. Check stored key
        config = _load_config()
        if config.get(self.api_key_config):
            key = config[self.api_key_config]
            hint = key[:7] + "..." + key[-4:] if len(key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key ({hint})"}

        # 2. Check env
        env_key = os.environ.get(self.api_key_env)
        if env_key:
            hint = env_key[:7] + "..." + env_key[-4:] if len(env_key) > 11 else "***"
            return {"authenticated": True, "method": "api_key", "detail": f"API key from env ({hint})"}

        path = self.is_available()
        if not path:
            return {"authenticated": False, "method": "none", "detail": "CLI not installed"}

        return {"authenticated": False, "method": "none", "detail": "No API key — get one at console.x.ai"}

    def build_run_command(self, prompt, system_prompt=None):
        # grok-cli runs claude code under a proxy, so output is claude stream-json format.
        # We pass the API key via -k flag if stored, otherwise grok uses its keychain.
        args = ["grok"]
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        if api_key:
            args.extend(["-k", api_key])

        # grok starts the proxy and then spawns claude, which reads from stdin.
        # For non-interactive use, we need to pass claude args after --.
        # Actually grok-cli spawns claude -p automatically — we need to set
        # CLAUDE_CODE_ARGS or similar. Let's check...
        # grok-cli runs: claude -p <prompt> with proxy env vars.
        # We'll set the prompt via env vars that grok passes to claude.

        # grok-cli doesn't support passing prompts directly — it spawns
        # interactive claude. For our use case we run claude directly with
        # the proxy env vars that grok would set.
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--dangerously-skip-permissions"
            ],
            env_overrides={"CLAUDECODE": None},
            env_inject={
                # Point Claude at the xAI API via anthropic-compatible endpoint
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **(auth_env if auth_env else {}),
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

    def build_resume_command(self, prompt, session_id):
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--resume", session_id,
                "--dangerously-skip-permissions"
            ],
            env_overrides={"CLAUDECODE": None},
            env_inject={
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

    def build_oneshot_command(self, prompt):
        auth_env = self.get_auth_env()
        api_key = auth_env.get(self.api_key_env)
        return CliCommand(
            args=[
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--dangerously-skip-permissions"
            ],
            env_overrides={"CLAUDECODE": None},
            env_inject={
                "ANTHROPIC_BASE_URL": "https://api.x.ai",
                **({"ANTHROPIC_API_KEY": api_key} if api_key else {}),
            },
        )

    def extract_session_id(self, event):
        # Same as Claude — grok uses claude under the hood
        if event.get("type") == "system" and event.get("subtype") == "init":
            return event.get("session_id")
        return None

    def is_noise(self, text):
        noise_prefixes = ("warn:", "Warning:", "DeprecationWarning", "[DEP")
        noise_substrings = ("oven-sh/bun", "baseline.zip", "baseline build", "proxy")
        return text.startswith(noise_prefixes) or any(s in text for s in noise_substrings)


# --- Provider Registry ---

_PROVIDERS: Dict[str, CliProvider] = {
    "claude": ClaudeCodeProvider(),
    "codex": CodexProvider(),
    "grok": GrokProvider(),
}

# Fallback default when no persistent config exists
_FALLBACK_CLI = "claude"

# Persistent config path
_CONFIG_FILE = Path.home() / ".kompany" / "config.json"


def _load_config() -> dict:
    """Load ~/.kompany/config.json."""
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(updates: dict):
    """Merge and save to ~/.kompany/config.json."""
    config = _load_config()
    config.update(updates)
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_default_cli() -> str:
    """Get the default CLI provider from persistent config, falling back to 'claude'."""
    saved = _load_config().get("default_cli")
    if saved and saved in _PROVIDERS:
        return saved
    return _FALLBACK_CLI


def set_default_cli(name: str):
    """Set the default CLI provider and persist to config."""
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown CLI provider: {name}. Available: {list(_PROVIDERS.keys())}")
    _save_config({"default_cli": name})


def get_provider(name: Optional[str] = None) -> CliProvider:
    """Get a CLI provider by name. Falls back to default."""
    name = name or get_default_cli()
    provider = _PROVIDERS.get(name)
    if not provider:
        raise ValueError(f"Unknown CLI provider: {name}. Available: {list(_PROVIDERS.keys())}")
    return provider


def get_available_providers() -> Dict[str, dict]:
    """Return info about all registered providers and their availability + auth status."""
    default = get_default_cli()
    result = {}
    for name, provider in _PROVIDERS.items():
        path = provider.is_available()
        auth = provider.get_auth_status()
        result[name] = {
            "name": name,
            "display_name": provider.display_name,
            "installed": path is not None,
            "path": path,
            "install_hint": provider.install_hint,
            "is_default": name == default,
            "authenticated": auth.get("authenticated", False),
            "auth_method": auth.get("method", "none"),
            "auth_detail": auth.get("detail", ""),
        }
    return result
