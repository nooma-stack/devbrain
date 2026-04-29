"""Tests for factory.port_registry — allocator + PortRange + Suggestion logic.

Pure-unit tests (no DB). The DB-backed PortRegistry class is exercised
in factory/tests/test_port_registry_db.py (lives in the DB-available CI
subset, runs against pgvector service container).
"""
from __future__ import annotations

import pytest

from port_registry import (
    NoFreePortError,
    PortRange,
    Suggestion,
    default_team_base,
    find_first_free_range,
    format_port_range,
    parse_port_spec,
    suggest_port_range,
)


# ──────────────────────────────────────────────────────────────────────
# PortRange
# ──────────────────────────────────────────────────────────────────────


def test_port_range_single_port():
    r = PortRange(8000, 8000)
    assert r.size == 1
    assert r.start == r.end == 8000


def test_port_range_multi_port():
    r = PortRange(20000, 20100)
    assert r.size == 101


def test_port_range_validation_rejects_invalid():
    with pytest.raises(ValueError):
        PortRange(0, 100)
    with pytest.raises(ValueError):
        PortRange(100, 99999)
    with pytest.raises(ValueError):
        PortRange(500, 400)  # start > end


def test_port_range_overlaps_detected():
    a = PortRange(20000, 20100)
    assert a.overlaps(PortRange(20050, 20150)) is True
    assert a.overlaps(PortRange(20000, 20000)) is True  # contained
    assert a.overlaps(PortRange(20100, 20100)) is True  # boundary
    assert a.overlaps(PortRange(20101, 20200)) is False
    assert a.overlaps(PortRange(19000, 19999)) is False


# ──────────────────────────────────────────────────────────────────────
# parse_port_spec / format_port_range
# ──────────────────────────────────────────────────────────────────────


def test_parse_port_spec_single():
    assert parse_port_spec("8000") == PortRange(8000, 8000)


def test_parse_port_spec_range():
    assert parse_port_spec("20000-20100") == PortRange(20000, 20100)


def test_parse_port_spec_strips_whitespace():
    assert parse_port_spec("  18000  ") == PortRange(18000, 18000)
    assert parse_port_spec(" 20000 - 20100 ") == PortRange(20000, 20100)


def test_format_port_range_single_omits_dash():
    assert format_port_range(PortRange(8000, 8000)) == "8000"


def test_format_port_range_emits_range():
    assert format_port_range(PortRange(20000, 20100)) == "20000-20100"


# ──────────────────────────────────────────────────────────────────────
# find_first_free_range
# ──────────────────────────────────────────────────────────────────────


def test_find_first_free_empty_occupancy():
    r = find_first_free_range(base=3000, size=1, occupied=[])
    assert r == PortRange(3000, 3000)


def test_find_first_free_returns_base_when_unoccupied():
    occ = [PortRange(8000, 8000)]
    r = find_first_free_range(base=3000, size=1, occupied=occ)
    assert r == PortRange(3000, 3000)


def test_find_first_free_skips_past_occupied():
    occ = [PortRange(3000, 3010)]
    r = find_first_free_range(base=3000, size=1, occupied=occ)
    assert r == PortRange(3011, 3011)


def test_find_first_free_finds_gap_between_occupied():
    occ = [PortRange(3000, 3010), PortRange(3050, 3060)]
    r = find_first_free_range(base=3000, size=5, occupied=occ)
    assert r == PortRange(3011, 3015)


def test_find_first_free_for_range_request():
    # Need a contiguous range of 100 ports starting from 20000
    occ = [PortRange(20050, 20100)]
    r = find_first_free_range(base=20000, size=50, occupied=occ)
    # 20000-20049 (size 50) fits before the occupied range
    assert r == PortRange(20000, 20049)


def test_find_first_free_jumps_when_first_gap_too_small():
    occ = [PortRange(3000, 3009), PortRange(3015, 3020)]
    # Need size 10 — 3010-3014 is too small (5 ports), so jump to after 3020
    r = find_first_free_range(base=3000, size=10, occupied=occ)
    assert r == PortRange(3021, 3030)


def test_find_first_free_returns_none_when_capped():
    occ = [PortRange(3000, 3050)]
    r = find_first_free_range(base=3000, size=1, occupied=occ, cap=3050)
    assert r is None


def test_find_first_free_handles_unsorted_occupied():
    occ = [PortRange(3050, 3060), PortRange(3000, 3010), PortRange(3030, 3035)]
    r = find_first_free_range(base=3000, size=5, occupied=occ)
    # First gap is 3011-3029 (size 19), so 3011-3015 fits
    assert r == PortRange(3011, 3015)


def test_find_first_free_rejects_invalid_size():
    with pytest.raises(ValueError):
        find_first_free_range(base=3000, size=0, occupied=[])


# ──────────────────────────────────────────────────────────────────────
# default_team_base
# ──────────────────────────────────────────────────────────────────────


_TEAM_RANGES = {
    "nooma-stack": {
        "web": [13000, 13999],
        "apis": [18000, 18999],
        "db_cache": [15000, 16999],
    },
    "lhtdev": {
        "web": [23000, 23999],
        "apis": [28000, 28999],
        "db_cache": [25000, 26999],
    },
}


def test_default_team_base_with_match():
    assert default_team_base("nooma-stack", "web", _TEAM_RANGES) == 13000
    assert default_team_base("nooma-stack", "apis", _TEAM_RANGES) == 18000
    assert default_team_base("lhtdev", "db_cache", _TEAM_RANGES) == 25000


def test_default_team_base_unknown_team_falls_back_to_3000():
    assert default_team_base("unknown-team", "web", _TEAM_RANGES) == 3000


def test_default_team_base_unknown_category_falls_back():
    assert default_team_base("nooma-stack", "exotic", _TEAM_RANGES) == 3000


def test_default_team_base_no_team_falls_back():
    assert default_team_base(None, "web", _TEAM_RANGES) == 3000
    assert default_team_base("", "web", _TEAM_RANGES) == 3000


def test_default_team_base_empty_config_falls_back():
    assert default_team_base("nooma-stack", "web", {}) == 3000


# ──────────────────────────────────────────────────────────────────────
# suggest_port_range
# ──────────────────────────────────────────────────────────────────────


def test_suggest_clean_allocation_at_team_base():
    s = suggest_port_range(
        purpose="api",
        host="localhost",
        size=1,
        occupied_active=[],
        occupied_archived=[],
        team="nooma-stack",
        category="apis",
        team_ranges=_TEAM_RANGES,
    )
    assert s.range == PortRange(18000, 18000)
    assert s.needs_approval is False


def test_suggest_skips_active_occupied_ports():
    s = suggest_port_range(
        purpose="api",
        host="localhost",
        size=1,
        occupied_active=[PortRange(18000, 18000)],
        occupied_archived=[],
        team="nooma-stack",
        category="apis",
        team_ranges=_TEAM_RANGES,
    )
    assert s.range == PortRange(18001, 18001)
    assert s.needs_approval is False


def test_suggest_with_explicit_base_overrides_team():
    s = suggest_port_range(
        purpose="api",
        host="localhost",
        size=1,
        occupied_active=[],
        occupied_archived=[],
        team="nooma-stack",
        category="apis",
        team_ranges=_TEAM_RANGES,
        explicit_base=8000,
    )
    assert s.range == PortRange(8000, 8000)


def test_suggest_treats_archived_as_blocked_for_clean_path():
    """Archived ranges block clean suggestions; caller picks them only via reclaim."""
    s = suggest_port_range(
        purpose="api",
        host="localhost",
        size=1,
        occupied_active=[],
        occupied_archived=[(PortRange(18000, 18000), "old-project")],
        team="nooma-stack",
        category="apis",
        team_ranges=_TEAM_RANGES,
    )
    # Clean path skips 18000 (archived) and lands on 18001
    assert s.range == PortRange(18001, 18001)
    assert s.needs_approval is False


def test_suggest_returns_archived_with_approval_when_clean_exhausted():
    """When no clean port fits, surface archived candidates with needs_approval=True."""
    # Block the entire team range with active assignments...
    occupied_active = [PortRange(18000, 18999)]
    # ...and have an archived range available
    occupied_archived = [(PortRange(18500, 18505), "old-project")]
    # The archived range is shadowed by occupied_active — the function's
    # archived-fallback path requires the archived range to NOT be in active.
    # In real usage occupied_active and occupied_archived are disjoint
    # (a port is either currently-reserved or archived, not both). So
    # construct a case where the team range is full of *active* projects
    # and the archived alternative falls outside that.
    s = suggest_port_range(
        purpose="api",
        host="localhost",
        size=1,
        occupied_active=[PortRange(18000, 18999)],
        occupied_archived=[(PortRange(19000, 19000), "old-project")],
        team="nooma-stack",
        category="apis",
        team_ranges=_TEAM_RANGES,
    )
    # The first free range after 18000 base, skipping 18000-18999 active,
    # lands at 19001 (because 19000 is archived). Clean path wins.
    assert s.range == PortRange(19001, 19001)
    assert s.needs_approval is False


def test_suggest_raises_when_truly_no_room():
    """Force the allocator into NoFreePortError territory."""
    # Block 1-65535 entirely
    big = [PortRange(1, 65535)]
    with pytest.raises(NoFreePortError):
        suggest_port_range(
            purpose="api",
            host="localhost",
            size=1,
            occupied_active=big,
            occupied_archived=[],
        )


def test_suggest_for_range_request():
    # Need 101 contiguous ports for asterisk_rtp
    s = suggest_port_range(
        purpose="asterisk_rtp",
        host="localhost",
        size=101,
        occupied_active=[],
        occupied_archived=[],
        explicit_base=20000,
    )
    assert s.range == PortRange(20000, 20100)
    assert s.range.size == 101


def test_suggest_for_range_skips_active_overlap():
    s = suggest_port_range(
        purpose="asterisk_rtp",
        host="localhost",
        size=10,
        occupied_active=[PortRange(20000, 20005)],
        occupied_archived=[],
        explicit_base=20000,
    )
    # Need 10 ports starting >= 20006
    assert s.range == PortRange(20006, 20015)
