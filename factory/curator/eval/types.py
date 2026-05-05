"""Versioned Pydantic models for eval findings.

Stable contract consumed by the fix-loop implementer + graduation pipeline.
Bumping a model goes via Pydantic discriminated union on `version`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EvalFinding(BaseModel):
    """A single finding from an eval agent."""

    model_config = ConfigDict(frozen=True)

    rule_id: UUID | None  # NULL if finding is from a heuristic, not a memory row
    severity: Literal["critical", "important", "minor"]
    file: str
    line: int | None
    message: str
    fix_hint: str
    relevant_memory_id: UUID | None  # which memory in brief surfaced this; NULL if missed


class EvalResult(BaseModel):
    """Full result of one eval agent run."""

    model_config = ConfigDict(frozen=True)

    version: Literal["1.0"]
    job_id: UUID
    agent_name: Literal["eval_security", "eval_test"]
    findings: list[EvalFinding]
    elapsed_ms: int
    started_at: datetime
    error: str | None = None  # set if agent failed; findings will be []
