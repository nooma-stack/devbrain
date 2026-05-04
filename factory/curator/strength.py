"""Cascade strength formula — pure functions, no DB dependency.

Forward-compat: callable from Phase 6 cognify offline for batch reweighting
(playbook §9). Do NOT add DB calls here. Do NOT add LLM calls here.

The formula:
    new_strength = max(0, old_strength - cascade_penalty(edge_type, age_seconds))

where cascade_penalty is the per-edge-type base penalty modulated by a 24h
half-life freshness decay. Bounded subtraction keeps multi-hop cascades
from compounding to zero through dep depth alone — a 4-hop cascade
through bounded penalties stays sensible.
"""
from __future__ import annotations

from decimal import Decimal

# Per-edge-type base penalty. Tuned conservatively — penalties are deliberately
# small so a single cascade rarely zeroes out a memory. The end_session
# judgment agent provides additional adjustments on top.
PENALTY: dict[str, float] = {
    "supersedes": 0.40,    # upstream replaced — strongest signal
    "archived_at": 0.25,   # upstream archived — moderate
    "applies_when": 0.10,  # upstream's context changed — light touch
}


def freshness_decay(age_seconds: float) -> float:
    """Penalty fades with time. Half-life = 24 hours.

    At 0s: 1.0 (full penalty)
    At 24h: 0.5 (half penalty)
    At 48h: 0.25
    At 1 week: ~0.0014
    """
    return 0.5 ** (age_seconds / 86400)


def cascade_penalty(edge_type: str, age_seconds: float) -> Decimal:
    """Base penalty modulated by freshness decay.

    Raises KeyError if edge_type is not in PENALTY.
    """
    base = PENALTY[edge_type]
    return Decimal(str(base * freshness_decay(age_seconds)))


def apply_cascade(
    strength: Decimal, edge_type: str, age_seconds: float
) -> Decimal:
    """Apply a cascade penalty, clamped at zero.

    Pure function — call this from anywhere (worker, cognify, postulate test).
    """
    new = strength - cascade_penalty(edge_type, age_seconds)
    return max(Decimal("0"), new)
