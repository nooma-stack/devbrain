"""Unit tests for curator strength formula (pure functions)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from curator.strength import (
    PENALTY,
    apply_cascade,
    cascade_penalty,
    freshness_decay,
)


def test_penalty_constants_ordered():
    """supersedes > archived_at > applies_when."""
    assert PENALTY["supersedes"] > PENALTY["archived_at"] > PENALTY["applies_when"]


def test_freshness_decay_at_zero_seconds_is_one():
    assert freshness_decay(0) == 1.0


def test_freshness_decay_at_24h_is_half():
    assert freshness_decay(86400) == pytest.approx(0.5, rel=1e-9)


def test_freshness_decay_at_48h_is_quarter():
    assert freshness_decay(86400 * 2) == pytest.approx(0.25, rel=1e-9)


def test_cascade_penalty_zero_age_supersedes():
    p = cascade_penalty("supersedes", 0)
    assert p == Decimal(str(PENALTY["supersedes"]))


def test_cascade_penalty_decays_with_age():
    p_now = cascade_penalty("supersedes", 0)
    p_24h = cascade_penalty("supersedes", 86400)
    assert p_24h < p_now
    assert p_24h == pytest.approx(p_now / 2, rel=1e-6)


def test_cascade_penalty_unknown_edge_type_raises():
    with pytest.raises(KeyError):
        cascade_penalty("not_an_edge", 0)


def test_apply_cascade_subtracts_penalty():
    new = apply_cascade(Decimal("0.85"), "supersedes", 0)
    assert new == Decimal("0.85") - cascade_penalty("supersedes", 0)


def test_apply_cascade_clamped_at_zero():
    new = apply_cascade(Decimal("0.10"), "supersedes", 0)
    assert new == Decimal("0")
    assert new >= Decimal("0")  # never negative


def test_apply_cascade_strong_memory_survives():
    # 0.85 strength, applies_when (lightest) cascade — should retain meaningful strength
    new = apply_cascade(Decimal("0.85"), "applies_when", 0)
    assert new > Decimal("0.5")
