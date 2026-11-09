"""Which folders the ingest watcher reads, and which paths it skips.

Kept free of watchdog/pipeline imports so brain_doctor (which runs in the
repo venv, not ingest/.venv) can use the same rules.
"""

from __future__ import annotations

from pathlib import Path

from config import ADAPTER_CONFIG

# Path components whose presence means "skip" — transient/irrelevant trees that
# generate filesystem churn (npm/codex temp installs) or vcs internals. A file
# under one of these (e.g. a node_modules LICENSE.md created+deleted in the same
# instant) crashed the watcher on 2026-05-18 and stopped all ingestion.
SKIP_DIRS = {"node_modules", ".tmp", ".git", ".venv", "__pycache__"}


def resolve_watch_dirs() -> list[Path]:
    """Resolve watch directories from each enabled adapter's config.

    Reads ingest.adapters.<name>.watch_paths from config/devbrain.yaml.
    Skips adapters where enabled=false. Expands ~ in paths. Expands
    glob wildcards (*, **, ?) via Path.glob() so configs like
    `~/.openclaw/agents/*/sessions` resolve to every matching dir.

    Special case: the markdown_memory adapter uses `memory_dirs`
    (a {dir: project_slug} mapping) instead of `watch_paths`. The
    scanner only needs the directory keys, so we accept either shape.
    """
    import glob as _glob

    dirs: list[Path] = []
    for adapter_name, adapter_cfg in ADAPTER_CONFIG.items():
        if not adapter_cfg.get("enabled", False):
            continue

        # markdown_memory uses memory_dirs={path: slug}, not watch_paths.
        # The adapter is responsible for the project mapping; the
        # scanner just needs the directories to traverse.
        path_strs: list[str] = list(adapter_cfg.get("watch_paths", []))
        for memory_dir in (adapter_cfg.get("memory_dirs") or {}).keys():
            path_strs.append(memory_dir)

        for path_str in path_strs:
            expanded = str(Path(path_str).expanduser())
            if any(ch in expanded for ch in ("*", "?", "[")):
                # Glob pattern — let Path.glob expand it. We treat the
                # last path segment containing a wildcard as a directory
                # boundary and glob from the prefix.
                for match in _glob.glob(expanded):
                    matched = Path(match)
                    if matched.is_dir():
                        dirs.append(matched)
            else:
                dirs.append(Path(expanded))
    return dirs
