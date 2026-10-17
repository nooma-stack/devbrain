"""A dedicated, tool-less CODEX_HOME for cognify's `codex exec` calls.

Cognify sends transcript and memory text to `codex exec`. Run against the
host's interactive ~/.codex, codex loads that user's MCP servers, plugins
and apps (Slack, calendar, a memory server with a write tool, ...). A
transcript carrying injected instructions could then drive write-capable
tools. So cognify runs codex with its own CODEX_HOME:

  * its own login (``CODEX_HOME=<dir> codex login --device-auth``), so it
    never shares or rotates the interactive session's tokens
  * a config.toml with no MCP servers, plugins, marketplaces or notify
    hook, and the tool features below switched off

``check()`` validates the directory before every call; codex is refused
(fail-closed, logged) when it is missing or unsafe. Set it up with
``devbrain cognify-codex-home init`` then the login command it prints.

Location: env DEVBRAIN_CODEX_HOME > yaml cognify.codex_home >
~/.devbrain/codex-cognify.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_HOME = "~/.devbrain/codex-cognify"

# Must be explicitly false: features that reach outside the read-only
# sandbox or act with the account's connected apps.
REQUIRED_OFF = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode_host",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "tool_suggest",
)
# Also off in the template (extraction needs no tools at all), but not
# required, so a host that needs them back can re-enable them.
TEMPLATE_ALSO_OFF = ("shell_tool", "unified_exec", "view_image", "skill_search")

# Top-level config keys that add tools, servers or side effects.
FORBIDDEN_KEYS = ("mcp_servers", "plugins", "marketplaces", "notify", "apps", "hooks")


def template() -> str:
    lines = [
        "# DevBrain cognify CODEX_HOME (written by `devbrain cognify-codex-home init`).",
        "# Cognify sends transcript text to codex: this home must never gain MCP",
        "# servers, plugins, apps or a notify hook. `devbrain cognify-codex-home",
        "# check` (and every cognify codex call) refuses to run if it does.",
        'approval_policy = "never"',
        'sandbox_mode = "read-only"',
        "",
        "[features]",
    ]
    lines += [f"{name} = false" for name in (*REQUIRED_OFF, *TEMPLATE_ALSO_OFF)]
    return "\n".join(lines) + "\n"


def resolve_home() -> Path:
    raw = os.environ.get("DEVBRAIN_CODEX_HOME")
    if not raw:
        try:
            from config import load_config as _load  # factory/config.py

            raw = str((_load().get("cognify") or {}).get("codex_home") or "")
        except Exception:  # noqa: BLE001
            raw = ""
    return Path(os.path.expanduser(raw or DEFAULT_HOME))


def _interactive_home() -> Path:
    return Path(os.path.expanduser("~/.codex"))


def check(home: Path | None = None) -> tuple[bool, str]:
    """(ok, reason). Never raises."""
    home = home or resolve_home()
    try:
        real = home.resolve()
        if not real.is_dir():
            return (
                False,
                f"{home} does not exist (run `devbrain cognify-codex-home init`)",
            )
        if real == _interactive_home().resolve():
            return (
                False,
                f"{home} is the interactive ~/.codex; use a dedicated directory",
            )
        if not (real / "auth.json").is_file():
            return False, (
                f"{home}/auth.json missing (run `CODEX_HOME={home} codex login --device-auth`)"
            )
        cfg_path = real / "config.toml"
        if not cfg_path.is_file():
            return (
                False,
                f"{home}/config.toml missing (run `devbrain cognify-codex-home init`)",
            )
        with open(cfg_path, "rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return False, f"{home}: unreadable ({type(exc).__name__}: {exc})"
    bad = [k for k in FORBIDDEN_KEYS if k in cfg]
    if bad:
        return False, f"{home}/config.toml must not set {', '.join(bad)}"
    features = cfg.get("features") or {}
    if not isinstance(features, dict):
        return False, f"{home}/config.toml [features] is not a table"
    on = [f for f in REQUIRED_OFF if features.get(f) is not False]
    if on:
        return False, f"{home}/config.toml must set false: {', '.join(on)}"
    return True, "ok"


def init(home: Path | None = None, *, force: bool = False) -> Path:
    """Create the directory (0700) and write config.toml. Never touches
    auth.json. Raises FileExistsError unless ``force`` when a config.toml
    is already there."""
    home = home or resolve_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    cfg_path = home / "config.toml"
    if cfg_path.exists() and not force:
        raise FileExistsError(str(cfg_path))
    cfg_path.write_text(template())
    os.chmod(cfg_path, 0o600)
    return cfg_path


def env_for(home: Path) -> dict[str, str]:
    """Subprocess environment pointing codex at the isolated home."""
    env = dict(os.environ)
    env["CODEX_HOME"] = str(home)
    return env
