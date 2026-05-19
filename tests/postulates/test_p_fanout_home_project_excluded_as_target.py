"""P_fanout_home_project_excluded_as_target: home-* projects never
appear in the classifier's taxonomy (they're canonical-only fallbacks,
not fan-out targets).

Per §12 design A3, home projects exist so dev-attribution-less sessions
have somewhere to live as canonical. They are NOT discoverable subjects
of cross-project discussion — fanning OUT into a home project would be
a degenerate self-reference for sessions that already canonical-own
that home project, and noise for any other session.

Enforcement: _load_active_taxonomy in factory/cognify/fanout.py adds
`AND slug NOT LIKE 'home-%'` to the projects SELECT.
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_p_fanout_home_project_excluded_as_target(conn):
    """The taxonomy fetcher excludes every home-* project."""
    from cognify.fanout import _load_active_taxonomy

    taxonomy = _load_active_taxonomy(conn)
    slugs = [p["slug"] for p in taxonomy]

    # No home-* in classifier targets.
    home_slugs = [s for s in slugs if s.startswith("home-")]
    assert home_slugs == [], (
        f"home-* projects must not be fan-out targets; found: {home_slugs}"
    )

    # Sanity: at least one real project exists (otherwise the test is
    # vacuously true and would mask a regression).
    assert len(slugs) > 0, "expected at least one non-home project in taxonomy"
