"""Gemini CLI adapter.

Gemini, like Claude, has no documented config-dir env var. We swap HOME
for the spawned subprocess to redirect `~/.gemini/` to the per-dev
profile. The swap is constrained to the single subprocess invocation.

Auth: an API key from https://aistudio.google.com/app/apikey. The key
is stashed at `<profile>/.devbrain/env` as `GEMINI_API_KEY=...` (mode
600). cli_executor sources this file before each gemini spawn.

`devbrain login --cli gemini` prompts the dev interactively (the SSH
session has a TTY) for the key. OAuth-via-browser is not supported
under SSH (the localhost callback path doesn't reach the dev's
machine) — API key is the only headless-friendly option.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)

_GEMINI_ENV_REL = Path(".devbrain") / "env"
_GEMINI_API_KEY_PREFIX = "AIza"


def _dev_api_key(dev) -> str | None:
    """Return the dev's gemini API key if set on the dev record, else None."""
    return getattr(dev, "gemini_api_key", None) or None


def _read_gemini_key_from_env_file(profile_dir: Path) -> str | None:
    """Read GEMINI_API_KEY=... from <profile>/.devbrain/env if present."""
    env_file = profile_dir / _GEMINI_ENV_REL
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip()
    return None


def _stash_gemini_key(profile_dir: Path, api_key: str) -> None:
    """Write GEMINI_API_KEY=<key> to <profile>/.devbrain/env (mode 600).

    Preserves any other KEY=VALUE lines already in the file; replaces
    any existing GEMINI_API_KEY line.
    """
    env_dir = profile_dir / ".devbrain"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / "env"
    existing = env_file.read_text() if env_file.exists() else ""
    lines = [
        ln for ln in existing.splitlines()
        if not ln.startswith("GEMINI_API_KEY=")
    ]
    lines.append(f"GEMINI_API_KEY={api_key.strip()}")
    env_file.write_text("\n".join(lines) + "\n")
    env_file.chmod(0o600)


def _prompt_for_api_key(prompt_in=None, prompt_out=None) -> str:
    """Prompt the dev interactively for their Gemini API key.

    Reads from the controlling tty so `devbrain login` over SSH works:
    the dev's local agent gets the prompt streamed through the SSH
    pipe, the dev pastes the key, the adapter receives it on stdin.

    prompt_in / prompt_out are injectable for tests; default to stdin / stderr.
    """
    if prompt_in is None:
        prompt_in = sys.stdin
    if prompt_out is None:
        prompt_out = sys.stderr
    prompt_out.write(
        "\nGemini API key required.\n"
        "Get one from https://aistudio.google.com/app/apikey "
        "(create one if you don't already have it), then paste it below.\n"
        "API key (starts with AIza): "
    )
    prompt_out.flush()
    return prompt_in.readline().strip()


class GeminiAdapter(AICliAdapter):
    name = "gemini"
    oauth_callback_ports = []  # API key flow has no callback

    def spawn_args(self, dev, profile_dir: Path) -> SpawnArgs:
        gitconfig = str(profile_dir / ".gitconfig")
        env: dict[str, str] = {
            "HOME": str(profile_dir),
            "GIT_CONFIG_GLOBAL": gitconfig,
            **git_author_env(dev),
        }
        api_key = _dev_api_key(dev) or _read_gemini_key_from_env_file(profile_dir)
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        return SpawnArgs(env=env, argv_prefix=["gemini"])

    def login(self, dev, profile_dir: Path) -> LoginResult:
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / ".gemini").mkdir(exist_ok=True)

        # Already-set key on the dev record: nothing to prompt for.
        if _dev_api_key(dev):
            return LoginResult(
                success=True,
                hint="Using GEMINI_API_KEY from dev record; no prompt needed.",
            )

        api_key = _prompt_for_api_key()
        if not api_key:
            return LoginResult(
                success=False,
                error="no API key provided",
                hint="Re-run `devbrain login --dev <id> --cli gemini` and paste your key when prompted.",
            )
        if not api_key.startswith(_GEMINI_API_KEY_PREFIX):
            return LoginResult(
                success=False,
                error=f"key doesn't look like a Gemini API key (expected prefix {_GEMINI_API_KEY_PREFIX!r})",
                hint="Get a fresh key at https://aistudio.google.com/app/apikey and try again.",
            )

        _stash_gemini_key(profile_dir, api_key)
        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        if _dev_api_key(dev):
            return True
        return _read_gemini_key_from_env_file(profile_dir) is not None

    def required_dotfiles(self) -> list[str]:
        return [str(_GEMINI_ENV_REL), ".gemini/", ".gitconfig"]


default_register = True
if default_register:
    from ai_clis.base import default_registry

    default_registry.register(GeminiAdapter)
