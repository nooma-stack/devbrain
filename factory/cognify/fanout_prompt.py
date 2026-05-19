"""Prompt template + project-taxonomy renderer for cognify_fanout.

Phase 8 — see docs/plans/2026-05-11-phase-8-cross-project-fan-out-design.md
§12 for the locked spec.

The classifier is one LLM call per session. The prompt asks the model to
identify topic sections inside the session, score per-section relevance
against each project (within-section threshold 0.75), aggregate to
session-level relevance, and emit a focused per-project summary for
each project that clears 0.30.

The single-call shape keeps cost predictable; the section-aware
prompt structure forces the model to reason about WHICH PARTS of the
session belong to which project rather than treating the whole session
as a soup.
"""
from __future__ import annotations

import json as _json
from typing import Any

# Thresholds (locked in §12.1).
SESSION_RELEVANCE_THRESHOLD = 0.30  # emit fan-out row when ≥ this
WITHIN_SECTION_THRESHOLD = 0.75     # section "belongs to" a project when ≥ this

# Per-project focused-summary length bounds (§12 A8).
SUMMARY_MIN_CHARS = 200
SUMMARY_MAX_CHARS = 800


def render_taxonomy(projects: list[dict[str, Any]]) -> str:
    """Render the project taxonomy for the prompt's SYSTEM block.

    Args:
        projects: list of {"slug": str, "name": str, "description": str|None}.
                  Caller is responsible for filtering to active projects only.

    Output is JSON, intentionally — gives the model a structured anchor
    and lets us prompt-cache the whole block.
    """
    sanitized = [
        {
            "slug": p["slug"],
            "name": p.get("name") or p["slug"],
            "description": (p.get("description") or "").strip() or None,
        }
        for p in projects
    ]
    return _json.dumps(sanitized, ensure_ascii=False, indent=2)


def build_system_prompt(taxonomy_json: str) -> str:
    """Construct the SYSTEM message body for the classifier call.

    Cache this — the taxonomy + instructions are stable across sessions.
    """
    return (
        "You are classifying a developer's work session into the projects "
        "it discussed. Project taxonomy (slug + name + description):\n\n"
        f"{taxonomy_json}\n\n"
        "Your job has four internal steps:\n"
        "  1. Identify topic sections — groups of consecutive turns about "
        "one subject. Could be 1 section or 6; you decide.\n"
        "  2. For each section, score per-project relevance 0.0–1.0. A "
        "section 'belongs to' a project only when its within-section "
        f"score is ≥ {WITHIN_SECTION_THRESHOLD}.\n"
        "  3. Aggregate to session-level per-project relevance = fraction "
        "of session sections that belong to that project. Drop any "
        f"project below {SESSION_RELEVANCE_THRESHOLD}.\n"
        "  4. For each kept project, write a focused summary "
        f"({SUMMARY_MIN_CHARS}–{SUMMARY_MAX_CHARS} chars) of the relevant "
        "sections' content, in the dev's voice (first person; concrete; "
        "name files/functions/decisions where useful).\n\n"
        "Return ONLY JSON. No preamble, no commentary, no markdown fences. "
        "First character must be `{`. Last character must be `}`.\n\n"
        "Schema:\n"
        "{\n"
        '  "sections": [\n'
        '    {"start_turn": int, "end_turn": int, "topic": str,\n'
        '     "project_scores": {"<slug>": 0.0-1.0, ...}}\n'
        "  ],\n"
        '  "per_project": [\n'
        '    {"project_slug": str,\n'
        '     "session_relevance": 0.0-1.0,\n'
        '     "section_count": int,\n'
        '     "focused_summary": str}\n'
        "  ]\n"
        "}\n"
    )


def build_user_message(session_content: str) -> str:
    """User-role payload: the session content the classifier reads.

    Capped at 200K characters — same as cognify_extract's ceiling. Real
    session_summaries top out well below this. If a session exceeds, the
    tail is truncated with a marker so the classifier knows.
    """
    cap = 200_000
    if len(session_content) <= cap:
        body = session_content
    else:
        body = (
            session_content[: cap - 200]
            + "\n\n[...TRUNCATED — session exceeded 200K characters...]"
        )
    return (
        "Classify the following session per the schema. Output ONLY the JSON.\n\n"
        f"<<< SESSION CONTENT >>>\n{body}"
    )


def validate_output(parsed: dict[str, Any], valid_slugs: set[str]) -> dict[str, Any]:
    """Defensive post-parse validation.

    Drops `per_project` entries where:
      * `session_relevance` is missing, non-numeric, or < SESSION_RELEVANCE_THRESHOLD
      * `project_slug` isn't in the live taxonomy (`valid_slugs`)
      * `focused_summary` is missing or empty

    Returns the SAME dict shape (sections + per_project) with the
    invalid entries removed. Does NOT mutate the input.

    Drops sections silently if shape is malformed — sections are
    advisory ("how did the model reason") and not load-bearing for
    the writer in PR 2.
    """
    out_per_project = []
    for entry in (parsed.get("per_project") or []):
        if not isinstance(entry, dict):
            continue
        slug = entry.get("project_slug")
        if slug not in valid_slugs:
            continue
        try:
            relevance = float(entry.get("session_relevance"))
        except (TypeError, ValueError):
            continue
        if relevance < SESSION_RELEVANCE_THRESHOLD:
            continue
        summary = entry.get("focused_summary") or ""
        if not isinstance(summary, str) or not summary.strip():
            continue
        out_per_project.append({
            "project_slug": slug,
            "session_relevance": relevance,
            "section_count": int(entry.get("section_count") or 0),
            "focused_summary": summary.strip(),
        })

    out_sections = []
    for sec in (parsed.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        scores = sec.get("project_scores")
        if not isinstance(scores, dict):
            continue
        # Filter unknown slugs out of section scores too.
        clean_scores = {
            k: float(v) for k, v in scores.items()
            if k in valid_slugs and isinstance(v, (int, float))
        }
        out_sections.append({
            "start_turn": int(sec.get("start_turn") or 0),
            "end_turn": int(sec.get("end_turn") or 0),
            "topic": (sec.get("topic") or "").strip(),
            "project_scores": clean_scores,
        })

    return {"sections": out_sections, "per_project": out_per_project}
