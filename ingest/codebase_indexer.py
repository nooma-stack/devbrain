#!/usr/bin/env python3
"""DevBrain codebase indexer.

Scans project directories, extracts file summaries and import graphs,
embeds them, and stores in devbrain.codebase_index for semantic code search.

Usage:
    python codebase_indexer.py                    # Index all projects
    python codebase_indexer.py --project brightbot # Index one project
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

from config import ADAPTER_CONFIG
from db import get_connection, get_project_id
from embeddings import embed

# File types to index
INDEXABLE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".sh",
    ".yaml", ".yml", ".json", ".toml", ".md",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules", ".venv", "__pycache__", "dist", "build",
    ".next", ".git", ".openclaw", ".claude", "vendor",
    "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

# Max file size to index (skip huge generated files)
MAX_FILE_SIZE = 100_000  # 100KB


def get_projects() -> list[dict]:
    """Get all projects with root_path from DB."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, slug, root_path FROM devbrain.projects WHERE root_path IS NOT NULL"
        )
        return [{"id": str(r[0]), "slug": r[1], "root_path": r[2]} for r in cur.fetchall()]


def get_last_commit(project_path: str) -> str | None:
    """Get HEAD commit SHA for a project."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def get_indexed_files(project_id: str) -> dict[str, str]:
    """Get currently indexed files and their last_commit."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT file_path, last_commit FROM devbrain.codebase_index WHERE project_id = %s",
            (project_id,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def get_changed_files_since(project_path: str, since_commit: str) -> set[str]:
    """Get files changed since a commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", since_commit, "HEAD"],
            cwd=project_path, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        pass
    return set()


def should_index(path: Path) -> bool:
    """Check if a file should be indexed."""
    if path.suffix not in INDEXABLE_EXTENSIONS:
        return False
    if any(skip in path.parts for skip in SKIP_DIRS):
        return False
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return False
    except OSError:
        return False
    return True


def extract_python_info(content: str) -> tuple[list[str], list[str], str]:
    """Extract imports, exports, and summary from a Python file."""
    imports = []
    exports = []

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("import ") or line.startswith("from "):
            imports.append(line)
        elif line.startswith("def ") or line.startswith("class "):
            name = re.match(r"(?:def|class)\s+(\w+)", line)
            if name:
                exports.append(name.group(1))
        elif line.startswith("__all__"):
            # Extract __all__ list
            match = re.search(r"__all__\s*=\s*\[([^\]]+)\]", content)
            if match:
                exports = [s.strip().strip("\"'") for s in match.group(1).split(",")]

    # Build a brief summary
    docstring = ""
    match = re.search(r'^"""(.*?)"""', content, re.DOTALL)
    if match:
        docstring = match.group(1).strip().split("\n")[0]

    summary = docstring or f"{len(exports)} exports, {len(imports)} imports"
    return imports, exports, summary


def extract_typescript_info(content: str) -> tuple[list[str], list[str], str]:
    """Extract imports, exports, and summary from a TypeScript/JS file."""
    imports = []
    exports = []

    for line in content.splitlines():
        line = line.strip()
        if line.startswith("import "):
            imports.append(line)
        elif "export " in line:
            match = re.match(
                r"export\s+(?:default\s+)?(?:function|class|const|let|var|type|interface|enum)\s+(\w+)",
                line,
            )
            if match:
                exports.append(match.group(1))

    summary = f"{len(exports)} exports, {len(imports)} imports"
    return imports, exports, summary


def extract_file_info(path: Path, content: str) -> tuple[list[str], list[str], str]:
    """Extract imports, exports, and summary based on file type."""
    if path.suffix == ".py":
        return extract_python_info(content)
    elif path.suffix in (".ts", ".tsx", ".js", ".jsx"):
        return extract_typescript_info(content)
    else:
        return [], [], f"{path.suffix} file, {len(content)} chars"


def upsert_file_index(
    project_id: str,
    file_path: str,
    file_type: str,
    summary: str,
    imports: list,
    exports: list,
    embedding: list[float],
    last_commit: str,
) -> None:
    """Upsert a file into the codebase index."""
    import json
    vector_str = f"[{','.join(str(v) for v in embedding)}]"

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.codebase_index
                (project_id, file_path, file_type, summary, imports, exports, embedding, last_commit, last_indexed)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::vector, %s, now())
            ON CONFLICT (project_id, file_path) DO UPDATE SET
                summary = EXCLUDED.summary,
                imports = EXCLUDED.imports,
                exports = EXCLUDED.exports,
                embedding = EXCLUDED.embedding,
                last_commit = EXCLUDED.last_commit,
                last_indexed = now()
            """,
            (
                project_id, file_path, file_type, summary,
                json.dumps(imports), json.dumps(exports),
                vector_str, last_commit,
            ),
        )
        conn.commit()


def index_project(project: dict, full: bool = False) -> int:
    """Index a single project. Returns number of files indexed."""
    root = Path(project["root_path"]).expanduser()
    if not root.exists():
        print(f"  Skipping {project['slug']}: {root} not found")
        return 0

    project_id = project["id"]
    current_commit = get_last_commit(str(root))

    if not full and current_commit:
        indexed = get_indexed_files(project_id)
        # Find a common last_commit to diff against
        last_commits = set(indexed.values())
        if last_commits and current_commit in last_commits:
            print(f"  {project['slug']}: already indexed at {current_commit[:8]}")
            return 0

        # Get changed files
        if last_commits:
            oldest_commit = min(last_commits)
            changed = get_changed_files_since(str(root), oldest_commit)
            if changed:
                print(f"  {project['slug']}: {len(changed)} files changed since {oldest_commit[:8]}")
            else:
                full = True  # Can't diff, do full scan
        else:
            full = True  # No prior index, do full scan

    # Collect files to index
    files_to_index = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if not should_index(path):
            continue
        rel_path = str(path.relative_to(root))

        if not full:
            if rel_path not in changed:
                continue

        files_to_index.append((path, rel_path))

    if not files_to_index:
        print(f"  {project['slug']}: no files to index")
        return 0

    print(f"  {project['slug']}: indexing {len(files_to_index)} files...")

    indexed_count = 0
    for path, rel_path in files_to_index:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            imports, exports, summary = extract_file_info(path, content)

            # Build embedding text: summary + exports + first 500 chars of file
            embed_text = f"{rel_path}\n{summary}\n{' '.join(exports)}\n{content[:500]}"
            embedding = embed(embed_text)

            upsert_file_index(
                project_id=project_id,
                file_path=rel_path,
                file_type=path.suffix.lstrip("."),
                summary=summary,
                imports=imports[:50],  # Cap imports list
                exports=exports[:50],
                embedding=embedding,
                last_commit=current_commit or "",
            )
            indexed_count += 1

            if indexed_count % 50 == 0:
                print(f"    ...{indexed_count}/{len(files_to_index)}")

        except Exception as e:
            print(f"    Error indexing {rel_path}: {e}")
            continue

    print(f"  {project['slug']}: indexed {indexed_count} files")
    return indexed_count


def main():
    parser = argparse.ArgumentParser(description="DevBrain codebase indexer")
    parser.add_argument("--project", help="Index a specific project slug")
    parser.add_argument("--full", action="store_true", help="Full re-index (ignore git diff)")
    args = parser.parse_args()

    projects = get_projects()

    if args.project:
        projects = [p for p in projects if p["slug"] == args.project]
        if not projects:
            print(f"Project '{args.project}' not found")
            return

    total = 0
    for project in projects:
        print(f"\nIndexing {project['slug']}...")
        count = index_project(project, full=args.full)
        total += count

    print(f"\n{'='*60}")
    print(f"Indexing complete: {total} files indexed across {len(projects)} projects")


if __name__ == "__main__":
    main()
