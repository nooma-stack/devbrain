#!/usr/bin/env python3 -u
"""DevBrain ingest — one-shot and watch modes.

Usage:
    python main.py scan              # One-shot: scan all known directories
    python main.py watch             # Continuous: watch for new session files
    python main.py file <path>       # Ingest a single file
    python main.py index <project>   # Index codebase for a project
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

from config import ADAPTER_CONFIG
from pipeline import ingest_file, detect_adapter


# ─── Scan directories ────────────────────────────────────────────────────────


def _resolve_watch_dirs() -> list[Path]:
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


WATCH_DIRS = _resolve_watch_dirs()

# Path components whose presence means "skip" — transient/irrelevant trees that
# generate filesystem churn (npm/codex temp installs) or vcs internals. A file
# under one of these (e.g. a node_modules LICENSE.md created+deleted in the same
# instant) crashed the watcher on 2026-05-18 and stopped all ingestion.
_SKIP_DIRS = {"node_modules", ".tmp", ".git", ".venv", "__pycache__"}


def scan_all():
    """One-shot scan of all known session directories."""
    total = 0
    ingested = 0

    for watch_dir in WATCH_DIRS:
        if not watch_dir.exists():
            print(f"Skipping {watch_dir} (not found)")
            continue

        print(f"\nScanning {watch_dir}...")
        for path in sorted(
            list(watch_dir.rglob("*.jsonl"))
            + list(watch_dir.rglob("session-*.json"))
            + list(watch_dir.rglob("*.md"))
        ):
            # Skip churny/irrelevant trees (npm/codex temp installs, vcs).
            if _SKIP_DIRS & set(path.parts):
                continue
            # Skip tiny files (< 1KB likely empty/corrupt). A transient file may
            # vanish between rglob and stat — don't let that abort the scan.
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < 1024:
                continue

            total += 1
            adapter = detect_adapter(path)
            if adapter is None:
                continue

            print(f"\n[{total}] {path.name} ({size // 1024}KB)")
            if ingest_file(path):
                ingested += 1

    print(f"\n{'='*60}")
    print(f"Scan complete: {ingested} ingested / {total} total files")


# ─── Watch mode ───────────────────────────────────────────────────────────────


class SessionFileHandler(FileSystemEventHandler):
    """Watches for new/modified session files and ingests them."""

    def on_created(self, event: FileCreatedEvent):
        if not isinstance(event, FileCreatedEvent):
            return
        self._handle(Path(event.src_path))

    def on_modified(self, event: FileModifiedEvent):
        if not isinstance(event, FileModifiedEvent):
            return
        self._handle(Path(event.src_path))

    def _handle(self, path: Path):
        # An unhandled exception here kills the watchdog observer thread and
        # silently stops all ingestion (KeepAlive won't fire — the process is
        # still alive, just deaf). So the entire body is exception-safe.
        try:
            if path.suffix not in (".jsonl", ".json", ".md"):
                return
            if path.suffix == ".json" and not path.name.startswith("session-"):
                return
            # Ignore churny/irrelevant trees — npm/codex temp installs create and
            # delete files in the same instant (the 2026-05-18 crash was a
            # vanished node_modules LICENSE.md), and we never want vcs internals.
            if _SKIP_DIRS & set(path.parts):
                return
            try:
                size = path.stat().st_size
            except OSError:
                return  # file vanished before we could stat it (transient temp)
            if size < 1024:
                return

            print(f"\n[watch] Detected: {path.name}")
            try:
                ingest_file(path)
            except Exception as e:
                print(f"[watch] Error ingesting {path.name}: {e}")
        except Exception as e:  # last-resort guard — never kill the observer
            print(f"[watch] Handler error on {path}: {e}")


def watch():
    """Continuously watch session directories for new files."""
    observer = Observer()
    handler = SessionFileHandler()

    for watch_dir in WATCH_DIRS:
        if watch_dir.exists():
            observer.schedule(handler, str(watch_dir), recursive=True)
            print(f"Watching: {watch_dir}")
        else:
            print(f"Skipping: {watch_dir} (not found)")

    observer.start()
    print("\nDevBrain ingest watcher running. Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "scan":
        scan_all()
    elif command == "watch":
        watch()
    elif command == "file" and len(sys.argv) >= 3:
        path = Path(sys.argv[2])
        if not path.exists():
            print(f"File not found: {path}")
            sys.exit(1)
        ingest_file(path, force=True)
    elif command == "index" and len(sys.argv) >= 3:
        project_slug = sys.argv[2]
        from codebase_indexer import index_project
        index_project(project_slug)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
