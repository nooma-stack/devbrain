#!/usr/bin/env python3
"""Generate per-app instruction files for DevBrain integration.

Creates instruction files that tell each AI app to use DevBrain
for persistent memory, context lookup, and session tracking.
"""

from __future__ import annotations

from pathlib import Path

DEVBRAIN_INSTRUCTIONS = """
## DevBrain — Persistent Memory (IMPORTANT)

You have access to DevBrain, a shared persistent memory system via MCP tools.
DevBrain remembers across sessions and across different AI tools.

### ALWAYS do these things:
1. **Start of session**: Call `get_project_context` to load project context, recent decisions, and known issues
2. **Before starting work**: Call `deep_search` to check for relevant past decisions, patterns, and implementation history — never assume
3. **During work**: Call `store` when you:
   - Make an architecture or design decision (type="decision")
   - Discover a reusable pattern (type="pattern")
   - Fix a bug worth remembering (type="issue")
4. **End of session**: Call `end_session` with a summary of what was accomplished, decisions made, files changed, and next steps

### DevBrain Search Tips:
- `deep_search` with depth="auto" will automatically fetch raw transcript context for high-confidence matches
- Use `get_source_context(chunk_id)` to drill into raw transcripts when you need more detail
- Search cross-project with `cross_project=true` when the question spans multiple projects

### Dev Factory:
- `factory_plan` — Submit a feature for autonomous implementation
- `factory_status` — Check job progress
- `factory_approve` — Approve/reject completed factory jobs
""".strip()


def generate_claude_md(project_slug: str, project_root: str) -> str:
    """Generate CLAUDE.md content for a project."""
    return f"""# DevBrain Integration — {project_slug}

{DEVBRAIN_INSTRUCTIONS}

### Project: {project_slug}
All DevBrain tools default to this project. No need to specify `project` parameter.
"""


def generate_agents_md(project_slug: str) -> str:
    """Generate AGENTS.md content for Codex."""
    return f"""# DevBrain Integration — {project_slug}

{DEVBRAIN_INSTRUCTIONS}
"""


def generate_cursorrules(project_slug: str) -> str:
    """Generate .cursorrules content."""
    return f"""{DEVBRAIN_INSTRUCTIONS}

Project: {project_slug}
"""


def write_instruction_files(project_slug: str, project_root: str) -> list[str]:
    """Write instruction files for all supported apps. Returns list of files written."""
    root = Path(project_root).expanduser()
    written = []

    # CLAUDE.md — check if it exists, append DevBrain section if not present
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text()
        if "DevBrain" not in content:
            with open(claude_md, "a") as f:
                f.write(f"\n\n{DEVBRAIN_INSTRUCTIONS}\n")
            written.append(str(claude_md))
            print(f"  Appended DevBrain instructions to {claude_md}")
        else:
            print(f"  {claude_md} already has DevBrain instructions")
    else:
        claude_md.write_text(generate_claude_md(project_slug, project_root))
        written.append(str(claude_md))
        print(f"  Created {claude_md}")

    # AGENTS.md for Codex
    agents_md = root / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(generate_agents_md(project_slug))
        written.append(str(agents_md))
        print(f"  Created {agents_md}")
    elif "DevBrain" not in agents_md.read_text():
        with open(agents_md, "a") as f:
            f.write(f"\n\n{DEVBRAIN_INSTRUCTIONS}\n")
        written.append(str(agents_md))
        print(f"  Appended DevBrain instructions to {agents_md}")
    else:
        print(f"  {agents_md} already has DevBrain instructions")

    # .cursorrules
    cursorrules = root / ".cursorrules"
    if not cursorrules.exists():
        cursorrules.write_text(generate_cursorrules(project_slug))
        written.append(str(cursorrules))
        print(f"  Created {cursorrules}")

    return written


def main():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "ingest"))
    from db import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT slug, root_path FROM devbrain.projects WHERE root_path IS NOT NULL")
        projects = cur.fetchall()

    for slug, root_path in projects:
        print(f"\n{slug}:")
        write_instruction_files(slug, root_path)


if __name__ == "__main__":
    main()
