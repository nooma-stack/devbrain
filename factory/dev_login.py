"""Business logic for `devbrain login` / `logins` / `logout`.

Kept separate from cli.py so it can be unit-tested without invoking click's
CliRunner machinery. cli.py thin-wraps these functions with @cli.command()
decorators.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterable

from ai_clis import default_registry
from ai_clis.base import AICliAdapter, LoginResult
import profiles

logger = logging.getLogger(__name__)


@dataclass
class LoginOutcome:
    dev_id: str
    cli_name: str
    success: bool
    error: str | None = None
    hint: str | None = None


@dataclass
class LoginsRow:
    dev_id: str
    cli_name: str
    logged_in: bool


def _dev_from_db(db, dev_id: str) -> SimpleNamespace:
    """Return the dev as a SimpleNamespace (so adapters' getattr works)."""
    row = db.get_dev(dev_id) if hasattr(db, "get_dev") else None
    if row is None:
        return SimpleNamespace(
            dev_id=dev_id, full_name=None, email=None, gemini_api_key=None,
        )
    return SimpleNamespace(
        dev_id=row.get("dev_id", dev_id),
        full_name=row.get("full_name"),
        email=row.get("email"),
        gemini_api_key=row.get("gemini_api_key"),
    )


def login_dev(
    dev_id: str,
    cli_names: Iterable[str],
    *,
    db,
    git_name: str | None = None,
    git_email: str | None = None,
    prompt_identity: Callable[[], tuple[str, str]] | None = None,
    set_tmux_env: bool = True,
) -> list[LoginOutcome]:
    """Log a dev into one or more AI CLIs.

    Creates the profile dir, populates per-dev .gitconfig (prompting the caller
    for name+email if not supplied and the dotfile doesn't exist yet),
    refreshes shared symlinks, and runs each adapter's `login()` in turn.

    Returns a list of LoginOutcome — one per CLI.
    """
    profiles.validate_dev_id(dev_id)
    profile_dir = profiles.get_profile_dir(dev_id)

    # Populate .gitconfig if missing. Resolution order:
    #   1) explicit --git-name + --git-email
    #   2) dev record's full_name + email (preferred — already known)
    #   3) prompt the operator (interactive only)
    #   4) fall back to dev_id + dev_id@devbrain.local
    gitconfig = profile_dir / ".gitconfig"
    if not gitconfig.exists():
        dev_for_identity = _dev_from_db(db, dev_id)
        if git_name and git_email:
            profiles.populate_gitconfig(profile_dir, git_name, git_email)
        elif dev_for_identity.full_name and dev_for_identity.email:
            profiles.populate_gitconfig(
                profile_dir,
                dev_for_identity.full_name,
                dev_for_identity.email,
            )
        elif prompt_identity:
            name, email = prompt_identity()
            profiles.populate_gitconfig(profile_dir, name, email)
        else:
            name = dev_for_identity.full_name or dev_id
            email = dev_for_identity.email or f"{dev_id}@devbrain.local"
            profiles.populate_gitconfig(profile_dir, name, email)

    profiles.refresh_shared_symlinks(profile_dir)

    dev = _dev_from_db(db, dev_id)
    outcomes: list[LoginOutcome] = []
    for cli_name in cli_names:
        try:
            adapter_cls = default_registry.get(cli_name)
        except KeyError as e:
            outcomes.append(
                LoginOutcome(dev_id=dev_id, cli_name=cli_name, success=False, error=str(e))
            )
            continue
        adapter: AICliAdapter = adapter_cls()
        result: LoginResult = adapter.login(dev, profile_dir)
        outcomes.append(
            LoginOutcome(
                dev_id=dev_id,
                cli_name=cli_name,
                success=result.success,
                error=result.error,
                hint=result.hint,
            )
        )

    if set_tmux_env and os.environ.get("TMUX"):
        try:
            subprocess.run(
                ["tmux", "setenv", "DEVBRAIN_DEV_ID", dev_id],
                check=False,
            )
        except FileNotFoundError:
            logger.debug("tmux binary not found; skipping setenv")

    return outcomes


def list_logins(*, db, dev_id: str | None = None) -> list[LoginsRow]:
    """Return a flat list of (dev_id, cli_name, logged_in) tuples for tabular display."""
    if dev_id is not None:
        profiles.validate_dev_id(dev_id)
        dev_ids = [dev_id]
    else:
        dev_ids = [p.dev_id for p in profiles.list_profiles()]

    rows: list[LoginsRow] = []
    for did in dev_ids:
        try:
            profile_dir = profiles.get_profile_dir(did)
        except ValueError:
            continue
        dev = _dev_from_db(db, did)
        for adapter_cls in default_registry.all():
            adapter: AICliAdapter = adapter_cls()
            rows.append(
                LoginsRow(
                    dev_id=did,
                    cli_name=adapter.name,
                    logged_in=adapter.is_logged_in(dev, profile_dir),
                )
            )
    return rows


# Profile-level shared paths that logout --cli should never touch.
# These belong to the profile (per-dev), not to any single AI CLI.
_PROFILE_SHARED_DOTFILES: frozenset[str] = frozenset({
    ".gitconfig",
    ".npmrc",
    ".config/gcloud",
    ".config/gh",
})


def logout_dev(
    dev_id: str,
    cli_names: Iterable[str] | None = None,
) -> None:
    """Remove a dev's profile dir, or specific CLI subdirs.

    With cli_names=None: removes the whole profile dir.
    With cli_names set: removes only the named CLIs' subdirs (e.g. .claude/, .codex/),
    leaving the profile dir + .gitconfig + symlinks intact.

    Adapters' required_dotfiles() may include profile-shared paths
    (.gitconfig, etc.) — those are skipped here so a per-CLI logout
    doesn't strip git authorship from other CLIs' future invocations.
    """
    profiles.validate_dev_id(dev_id)
    if cli_names is None:
        profiles.delete_profile(dev_id)
        return

    profile_dir = profiles.get_profile_dir(dev_id)
    for cli_name in cli_names:
        try:
            adapter_cls = default_registry.get(cli_name)
        except KeyError:
            continue
        for rel in adapter_cls.required_dotfiles(adapter_cls()):
            normalized = rel.rstrip("/")
            if normalized in _PROFILE_SHARED_DOTFILES:
                continue
            target = profile_dir / rel
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
