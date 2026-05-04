"""Round-trip + validation tests for factory.curator.types."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from curator.types import (
    CascadeNote,
    CuratorBrief,
    MemoryRef,
)


def _ref(**overrides):
    base = dict(
        id=uuid4(),
        kind="decision",
        title="Use additive penalty",
        content_excerpt="Cascade penalty is bounded so multi-hop converges.",
        tier="memory",
        strength=Decimal("0.85"),
        last_cascade_at=None,
    )
    return MemoryRef(**{**base, **overrides})


def _note(**overrides):
    base = dict(
        affected_memory_id=uuid4(),
        cascade_source_id=uuid4(),
        edge_type="supersedes",
        occurred_at=datetime.now(timezone.utc),
        summary="Rule R6 was superseded 2h ago",
    )
    return CascadeNote(**{**base, **overrides})


def test_memory_ref_roundtrip():
    ref = _ref()
    serialized = ref.model_dump_json()
    restored = MemoryRef.model_validate_json(serialized)
    assert restored == ref


def test_cascade_note_roundtrip():
    note = _note()
    serialized = note.model_dump_json()
    restored = CascadeNote.model_validate_json(serialized)
    assert restored == note


def test_curator_brief_roundtrip():
    brief = CuratorBrief(
        version="1.0",
        job_id=uuid4(),
        project_id=uuid4(),
        rules=[_ref(tier="rule")],
        lessons=[_ref(tier="lesson")],
        relevant_decisions=[_ref()],
        recent_cascade_signals=[_note()],
        generated_at=datetime.now(timezone.utc),
    )
    restored = CuratorBrief.model_validate_json(brief.model_dump_json())
    assert restored == brief


def test_curator_brief_rejects_unknown_version():
    with pytest.raises(ValidationError):
        CuratorBrief(
            version="2.0",  # not a valid Literal value
            job_id=uuid4(),
            project_id=uuid4(),
            rules=[],
            lessons=[],
            relevant_decisions=[],
            recent_cascade_signals=[],
            generated_at=datetime.now(timezone.utc),
        )


def test_memory_ref_rejects_invalid_tier():
    with pytest.raises(ValidationError):
        _ref(tier="bogus")


def test_cascade_note_rejects_invalid_edge_type():
    with pytest.raises(ValidationError):
        _note(edge_type="not_an_edge")
