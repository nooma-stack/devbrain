"""Per-dev profile directory management for the DevBrain factory.

Each registered dev gets a profile directory at <DEVBRAIN_HOME>/profiles/<dev_id>/
that holds:
- .claude/, .codex/, .gemini/ — per-dev AI CLI credentials
- .gitconfig — per-dev git author identity (so factory commits attribute correctly)
- symlinks to shared host dotfiles (.npmrc, .config/gcloud, etc.) configured via
  factory.shared_dotfiles in devbrain.yaml

The AI CLI adapter for each phase swaps env (HOME=<profile> for Claude/Gemini,
CODEX_HOME=<profile>/.codex for Codex) so the spawned subprocess reads
this dev's credentials rather than the host macOS user's.

See docs/plans/2026-04-28-multi-dev-home-profiles-design.md for the architecture.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Override hook for tests — when set, profiles_root() returns this instead of
# computing from config. Tests use monkeypatch to set it.
_PROFILES_ROOT_OVERRIDE: Path | None = None

DEV_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

DEFAULT_SHARED_DOTFILES: list[str] = [
    ".npmrc",
    ".config/gcloud",
    ".config/gh",
]


@dataclass
class ProfileInfo:
    dev_id: str
    path: Path
    created_at: datetime


def validate_dev_id(dev_id: str) -> None:
    """Raise ValueError if dev_id contains anything other than [a-z0-9_-], len 1-64.

    Lowercase only — enforces a single canonical form so PatrickLHT and
    patrick-lht don't both create profiles. Hyphens and underscores allowed.
    """
    if not DEV_ID_RE.fullmatch(dev_id or ""):
        raise ValueError(
            f"invalid dev_id {dev_id!r}: must match {DEV_ID_RE.pattern}"
        )


def profiles_root() -> Path:
    """Return the directory under which per-dev profile dirs live."""
    if _PROFILES_ROOT_OVERRIDE is not None:
        return _PROFILES_ROOT_OVERRIDE
    from config import DEVBRAIN_HOME

    return Path(DEVBRAIN_HOME) / "profiles"


def get_profile_dir(dev_id: str) -> Path:
    """Return the dev's profile dir, creating it if missing.

    Raises ValueError if dev_id is not a valid dev_id (path traversal,
    bad chars, etc.).
    """
    validate_dev_id(dev_id)
    root = profiles_root()
    profile = root / dev_id
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def list_profiles() -> list[ProfileInfo]:
    """Return all valid profile directories under profiles_root()."""
    root = profiles_root()
    if not root.exists():
        return []
    out: list[ProfileInfo] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        try:
            validate_dev_id(entry.name)
        except ValueError:
            logger.debug("Skipping non-dev_id directory: %s", entry)
            continue
        ctime = datetime.fromtimestamp(entry.stat().st_ctime, tz=timezone.utc)
        out.append(ProfileInfo(dev_id=entry.name, path=entry, created_at=ctime))
    return out


def delete_profile(dev_id: str) -> None:
    """Remove the dev's profile directory and all contents."""
    validate_dev_id(dev_id)
    profile = profiles_root() / dev_id
    if profile.exists():
        shutil.rmtree(profile)
        logger.info("Deleted profile for dev %s", dev_id)


def populate_gitconfig(profile_dir: Path, name: str, email: str) -> None:
    """Write a minimal .gitconfig in the profile dir with [user] block.

    Overwrites existing file. Spawned AI CLIs that do `git commit` will
    pick up these values via GIT_CONFIG_GLOBAL=<profile>/.gitconfig.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    contents = f"[user]\n\tname = {name}\n\temail = {email}\n"
    (profile_dir / ".gitconfig").write_text(contents)


def refresh_shared_symlinks(
    profile_dir: Path,
    host_home: Path | None = None,
    shared_paths: list[str] | None = None,
) -> None:
    """Create symlinks from profile_dir/<path> → host_home/<path> for shared dotfiles.

    Used to make org-shared credentials (.npmrc, gcloud config, etc.) visible
    to spawned AI CLIs even though their HOME is swapped to the per-dev profile.
    Skips entries whose source on host_home doesn't exist.

    Args:
        profile_dir: per-dev profile dir
        host_home: source of truth for shared dotfiles (defaults to ~ — the host
            macOS user, typically `lhtdev` on the shared Mac Studio)
        shared_paths: list of relative paths under host_home to symlink in.
            Defaults to factory.shared_dotfiles config or DEFAULT_SHARED_DOTFILES.
    """
    if host_home is None:
        host_home = Path.home()
    if shared_paths is None:
        shared_paths = _load_shared_paths()

    profile_dir.mkdir(parents=True, exist_ok=True)
    for rel in shared_paths:
        src = host_home / rel
        if not src.exists():
            logger.debug("Skipping shared dotfile %s (not present on host)", rel)
            continue
        dst = profile_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)
        logger.debug("Symlinked %s → %s", dst, src)


def _load_shared_paths() -> list[str]:
    """Read factory.shared_dotfiles from devbrain.yaml; fall back to defaults."""
    try:
        from config import FACTORY_CONFIG

        configured = FACTORY_CONFIG.get("shared_dotfiles")
        if configured:
            return list(configured)
    except ImportError:
        pass
    return list(DEFAULT_SHARED_DOTFILES)
