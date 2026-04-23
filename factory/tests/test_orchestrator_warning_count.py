"""Tests for warning_count plumbing in factory_artifacts."""
import pytest

from state_machine import FactoryDB, JobStatus
from orchestrator import _count_warning, _extract_warning_items

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


# Autouse cleanup: every integration test in this directory that creates
# factory_jobs rows should pair with a teardown that deletes them by
# title prefix, or each CI run accumulates dozens of stale rows in the
# dev DB (observed 2026-04-23 — this test file's own pre-cleanup version
# contributed to a 90-row pollution cleanup). Mirrors the pattern already
# used in test_orchestrator_branch_setup.py / test_cleanup_agent.py /
# test_blocked_resolution_flow.py.
_TEST_TITLE_PREFIX = "warning_count plumbing test"


@pytest.fixture(autouse=True)
def _cleanup_test_rows(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.factory_jobs WHERE title LIKE %s",
            (f"{_TEST_TITLE_PREFIX}%",),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()


def test_count_warning_matches_common_markers():
    """_count_warning detects the same list/markdown shapes as its BLOCKING twin."""
    text = """
1. WARNING: missing docstring
- WARNING: consider caching result
**WARNING**: shadowed variable
warning: lower-case still counts
"""
    assert _count_warning(text) == 4


def test_extract_warning_items_splits_on_markers_and_stops_at_next_severity():
    """Each WARNING item should be its own string, truncated at the next severity marker."""
    text = """
1. WARNING: first warning item with detail
   spans a second line
2. BLOCKING: a blocking issue
3. WARNING: second warning
4. NIT: style nitpick
"""
    items = _extract_warning_items(text)
    assert len(items) == 2
    assert "first warning item" in items[0]
    assert "spans a second line" in items[0]
    assert "BLOCKING" not in items[0]
    assert "second warning" in items[1]
    assert "NIT" not in items[1]


def test_store_artifact_persists_warning_count(db):
    """store_artifact accepts warning_count and round-trips it via get_artifacts."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="warning_count plumbing test (store)",
        spec="Test warning_count persists on insert and read-back.",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(
        job_id=job_id,
        phase="review",
        artifact_type="arch_review",
        content="WARNING: x\nWARNING: y\nBLOCKING: z",
        findings_count=3,
        blocking_count=1,
        warning_count=2,
    )

    artifacts = db.get_artifacts(job_id, phase="review")
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["warning_count"] == 2
    assert art["blocking_count"] == 1
    assert art["findings_count"] == 3
    # Other fields still deserialize correctly after index shift
    assert art["artifact_type"] == "arch_review"
    assert art["metadata"] == {}
    assert art["created_at"] is not None


def test_warning_count_defaults_to_zero_when_omitted(db):
    """Callers that don't pass warning_count (QA, fix sites) still work and get 0."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="warning_count plumbing test (default)",
        spec="Test warning_count defaults to 0 when caller omits it.",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(
        job_id=job_id,
        phase="planning",
        artifact_type="plan",
        content="a plan",
    )

    artifacts = db.get_artifacts(job_id, phase="planning")
    assert len(artifacts) == 1
    assert artifacts[0]["warning_count"] == 0
