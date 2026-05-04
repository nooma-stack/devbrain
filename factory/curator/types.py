"""Versioned Pydantic models for the curator agent.

Stable contract consumed by Step 6 eval agents and (eventually) Phase 6
cognify. Bumping a model goes via Pydantic discriminated union on the
`version` Literal — adding "1.1" is a clean fork; old code keeps reading
v1.0 rows from factory_jobs.curator_brief unchanged.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MemoryRef(BaseModel):
    """A single memory included in the curator brief."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: Literal["chunk", "decision", "pattern", "issue", "session_summary"]
    title: str | None
    content_excerpt: str  # first ~500 chars of content
    tier: Literal["memory", "lesson", "rule"]
    strength: Decimal
    last_cascade_at: datetime | None


class CascadeNote(BaseModel):
    """Surfaces a recent cascade event the planner should be aware of."""

    model_config = ConfigDict(frozen=True)

    affected_memory_id: UUID
    cascade_source_id: UUID
    edge_type: Literal["supersedes", "archived_at", "applies_when"]
    occurred_at: datetime
    summary: str  # human-readable: "Rule R6 was superseded 2h ago"


class CuratorBrief(BaseModel):
    """The brief handed from the curator to a factory job's planner.

    Cached on `devbrain.factory_jobs.curator_brief` JSONB so every phase
    (planner, implementer, reviewer, QA) reads the identical snapshot.
    """

    model_config = ConfigDict(frozen=True)

    version: Literal["1.0"]
    job_id: UUID
    project_id: UUID
    rules: list[MemoryRef]              # tier='rule', compliance-profile-filtered
    lessons: list[MemoryRef]            # tier='lesson', strength-ranked
    relevant_decisions: list[MemoryRef] # tier='memory', applies_when matched
    recent_cascade_signals: list[CascadeNote]
    generated_at: datetime
