"""Unit tests for the curator refinement path (Atlas Step 6d).

Covers factory/curator/refinement.py:

  Pure helpers (no DB):
    * _file_glob_from_path — directory globs, edge cases
    * _extract_keywords — dedupe, length floor, cap at 5

  DB tests (@pytest.mark.db):
    * queue_refinement inserts a row with file_pattern + keywords from
      finding.file/message
    * queue_refinement is a no-op when finding.relevant_memory_id is None
    * refine_applies_when widens applies_when JSONB:
        - preserves existing keys (e.g. category)
        - adds new files + keywords
        - dedupes against existing values
    * refine_applies_when respects project boundaries — queue rows for a
      different project are not processed
    * refine_applies_when failure path — when _widen_applies_when raises,
      applied_at + error are set so the row doesn't retry forever
    * refine_applies_when 7-day window — queue entries queued_at > 7
      days ago are skipped
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from curator.eval.types import EvalFinding
from curator.refinement import (
    _extract_keywords,
    _file_glob_from_path,
    queue_refinement,
    refine_applies_when,
)


def _make_finding(
    *,
    relevant_memory_id,
    file: str = "factory/curator/worker.py",
    message: str = "missing nullcheck on env variable lookup",
):
    return EvalFinding(
        rule_id=relevant_memory_id,
        severity="important",
        file=file,
        line=42,
        message=message,
        fix_hint="add a guard",
        relevant_memory_id=relevant_memory_id,
    )


def _read_queue_row(conn, queue_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, file_pattern, keywords, "
            "       queued_at, applied_at, error "
            "FROM devbrain.refinement_queue WHERE id = %s",
            (queue_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "memory_id": row[0],
        "file_pattern": row[1],
        "keywords": row[2],
        "queued_at": row[3],
        "applied_at": row[4],
        "error": row[5],
    }


def _read_applies_when(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applies_when FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _set_applies_when(conn, memory_id, value):
    """Direct UPDATE of applies_when (memory_factory doesn't take it)."""
    import json

    with conn.cursor() as cur:
        if value is None:
            cur.execute(
                "UPDATE devbrain.memory SET applies_when = NULL WHERE id = %s",
                (memory_id,),
            )
        else:
            cur.execute(
                "UPDATE devbrain.memory SET applies_when = %s::jsonb WHERE id = %s",
                (json.dumps(value), memory_id),
            )
    conn.commit()


def _select_pending_queue_ids(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.refinement_queue "
            "WHERE memory_id = %s AND applied_at IS NULL "
            "ORDER BY queued_at",
            (memory_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _select_all_queue_ids(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.refinement_queue "
            "WHERE memory_id = %s ORDER BY queued_at",
            (memory_id,),
        )
        return [r[0] for r in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no DB)
# ─────────────────────────────────────────────────────────────────────────────


def test_file_glob_from_path_directory_glob():
    """Standard case: file in a directory becomes dir/*.py."""
    assert (
        _file_glob_from_path("factory/curator/worker.py")
        == "factory/curator/*.py"
    )


def test_file_glob_from_path_deep_path():
    """Multiple intermediate directories collapse to last-dir/*.py."""
    assert (
        _file_glob_from_path("a/b/c/d/e.py") == "a/b/c/d/*.py"
    )


def test_file_glob_from_path_no_slash():
    """No slash → return as-is (no directory level to glob over)."""
    assert _file_glob_from_path("README.md") == "README.md"


def test_file_glob_from_path_empty_string():
    """Empty/falsy returns empty string."""
    assert _file_glob_from_path("") == ""


def test_file_glob_from_path_none_safe():
    """None-safe: helper accepts a None-ish (falsy) argument."""
    assert _file_glob_from_path(None) == ""  # type: ignore[arg-type]


def test_extract_keywords_basic():
    """Words >= 4 chars are returned, deduped, lowercased."""
    keywords = _extract_keywords("Missing nullcheck on env variable")
    # "on" is < 4 chars and dropped
    assert "missing" in keywords
    assert "nullcheck" in keywords
    assert "variable" in keywords
    assert "on" not in keywords


def test_extract_keywords_capped_at_five():
    """Result is capped at 5 entries even if input has more eligible words."""
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    keywords = _extract_keywords(text)
    assert len(keywords) == 5
    # First-five-eligible wins, in input order.
    assert keywords == ["alpha", "beta", "gamma", "delta", "epsilon"]


def test_extract_keywords_dedupes():
    """Repeated words appear once."""
    keywords = _extract_keywords("repeat repeat repeat unique unique")
    assert keywords == ["repeat", "unique"]


def test_extract_keywords_empty_or_none():
    """Empty + None inputs return []."""
    assert _extract_keywords("") == []
    assert _extract_keywords(None) == []  # type: ignore[arg-type]


def test_extract_keywords_drops_short_words():
    """Words < 4 chars are excluded."""
    # 'a', 'is', 'the' are all < 4 chars
    keywords = _extract_keywords("a is the four")
    assert keywords == ["four"]


# ─────────────────────────────────────────────────────────────────────────────
# queue_refinement (DB)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_queue_refinement_inserts_row(
    conn, project_factory, memory_factory
):
    """queue_refinement writes one row with derived file_pattern + keywords."""
    project = project_factory("queueref")
    m = memory_factory(project["id"], tier="lesson")

    finding = _make_finding(
        relevant_memory_id=m["id"],
        file="factory/curator/worker.py",
        message="missing nullcheck on env variable",
    )

    queue_refinement(conn, finding)

    pending = _select_pending_queue_ids(conn, m["id"])
    assert len(pending) == 1
    row = _read_queue_row(conn, pending[0])
    assert row["memory_id"] == m["id"]
    assert row["file_pattern"] == "factory/curator/*.py"
    # Keywords are extracted (lower-cased, deduped, ≥4 chars).
    assert "missing" in row["keywords"]
    assert "nullcheck" in row["keywords"]
    assert "variable" in row["keywords"]
    assert row["applied_at"] is None
    assert row["error"] is None


@pytest.mark.db
def test_queue_refinement_noop_when_relevant_memory_id_is_none(
    conn, project_factory
):
    """Heuristic findings (relevant_memory_id=None) don't insert a row."""
    project_factory("queuenoop")  # nothing to look up; just need a clean DB

    finding = EvalFinding(
        rule_id=None,
        severity="minor",
        file="x.py",
        line=1,
        message="heuristic finding with no memory ref",
        fix_hint="...",
        relevant_memory_id=None,
    )

    # Snapshot count before.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM devbrain.refinement_queue")
        before = cur.fetchone()[0]

    queue_refinement(conn, finding)

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM devbrain.refinement_queue")
        after = cur.fetchone()[0]

    assert after == before


# ─────────────────────────────────────────────────────────────────────────────
# refine_applies_when (DB)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_refine_applies_when_widens_jsonb_preserving_existing(
    conn, project_factory, memory_factory
):
    """refine_applies_when merges file_pattern + keywords into applies_when:
      - keeps existing keys (e.g. {"category": "tools"})
      - adds new "files" + "keywords" arrays
      - dedupes against pre-existing values"""
    project = project_factory("widen")
    m = memory_factory(project["id"], tier="lesson")
    # Seed an existing applies_when with a category key + some keywords
    # already present so we can verify dedupe.
    _set_applies_when(
        conn,
        m["id"],
        {
            "category": "tools",
            "files": ["existing/path.py"],
            "keywords": ["nullcheck"],
        },
    )

    finding = _make_finding(
        relevant_memory_id=m["id"],
        file="factory/curator/worker.py",
        message="missing nullcheck guard",
    )
    queue_refinement(conn, finding)

    refine_applies_when(conn, project["id"])

    aw = _read_applies_when(conn, m["id"])
    # category preserved
    assert aw["category"] == "tools"
    # files merged + sorted, includes existing + new pattern
    assert "factory/curator/*.py" in aw["files"]
    assert "existing/path.py" in aw["files"]
    assert aw["files"] == sorted(aw["files"])
    # keywords deduped (nullcheck appears once)
    assert aw["keywords"].count("nullcheck") == 1
    # new keywords added
    assert "missing" in aw["keywords"]
    # sorted for determinism
    assert aw["keywords"] == sorted(aw["keywords"])

    # Queue row marked applied_at and no error.
    all_rows = _select_all_queue_ids(conn, m["id"])
    assert len(all_rows) == 1
    queue_row = _read_queue_row(conn, all_rows[0])
    assert queue_row["applied_at"] is not None
    assert queue_row["error"] is None


@pytest.mark.db
def test_refine_applies_when_treats_null_applies_when_as_empty(
    conn, project_factory, memory_factory
):
    """If applies_when is NULL on the memory row, _widen_applies_when
    treats it as {} and writes a fresh dict containing files + keywords."""
    project = project_factory("widennull")
    m = memory_factory(project["id"], tier="lesson")
    _set_applies_when(conn, m["id"], None)

    finding = _make_finding(
        relevant_memory_id=m["id"],
        file="src/utils.py",
        message="forgot timeout parameter",
    )
    queue_refinement(conn, finding)

    refine_applies_when(conn, project["id"])

    aw = _read_applies_when(conn, m["id"])
    assert aw is not None
    assert "src/*.py" in aw["files"]
    # All keywords should be present
    assert "forgot" in aw["keywords"]
    assert "timeout" in aw["keywords"]


@pytest.mark.db
def test_refine_applies_when_is_project_scoped(
    conn, project_factory, memory_factory
):
    """A queue row for project A's memory is NOT processed when
    refine_applies_when runs against project B."""
    project_a = project_factory("scopedA")
    project_b = project_factory("scopedB")

    m_a = memory_factory(project_a["id"], tier="lesson")
    m_b = memory_factory(project_b["id"], tier="lesson")
    _set_applies_when(conn, m_a["id"], None)
    _set_applies_when(conn, m_b["id"], None)

    queue_refinement(
        conn,
        _make_finding(
            relevant_memory_id=m_a["id"], file="a/x.py", message="alpha bravo"
        ),
    )
    queue_refinement(
        conn,
        _make_finding(
            relevant_memory_id=m_b["id"], file="b/y.py", message="charlie delta"
        ),
    )

    # Run refine for project B only.
    refine_applies_when(conn, project_b["id"])

    # m_a is unchanged — its queue row was NOT processed.
    aw_a = _read_applies_when(conn, m_a["id"])
    assert aw_a is None  # _widen never ran on it
    # m_a queue row is still pending.
    assert len(_select_pending_queue_ids(conn, m_a["id"])) == 1

    # m_b was widened.
    aw_b = _read_applies_when(conn, m_b["id"])
    assert aw_b is not None
    assert "b/*.py" in aw_b["files"]
    # m_b queue row was applied.
    assert len(_select_pending_queue_ids(conn, m_b["id"])) == 0


@pytest.mark.db
def test_refine_applies_when_failure_path_persists_error(
    conn, project_factory, memory_factory, monkeypatch
):
    """When _widen_applies_when raises, the queue row is closed out with
    applied_at + error so it doesn't retry forever."""
    project = project_factory("widenfail")
    m = memory_factory(project["id"], tier="lesson")
    _set_applies_when(conn, m["id"], None)

    queue_refinement(
        conn,
        _make_finding(
            relevant_memory_id=m["id"],
            file="x.py",
            message="some failing finding",
        ),
    )

    # Monkey-patch _widen_applies_when to raise on first call.
    import curator.refinement as refmod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated widen failure")

    monkeypatch.setattr(refmod, "_widen_applies_when", _boom)

    refine_applies_when(conn, project["id"])

    pending = _select_pending_queue_ids(conn, m["id"])
    assert pending == [], "errored row should have applied_at set"

    all_rows = _select_all_queue_ids(conn, m["id"])
    row = _read_queue_row(conn, all_rows[0])
    assert row["applied_at"] is not None
    assert row["error"] is not None
    assert "simulated widen failure" in row["error"]

    # Run again — should NOT process the same row again (it has applied_at).
    refine_applies_when(conn, project["id"])
    row2 = _read_queue_row(conn, all_rows[0])
    # error message stays the same (not retried).
    assert row2["error"] == row["error"]


@pytest.mark.db
def test_refine_applies_when_skips_stale_queue_rows(
    conn, project_factory, memory_factory
):
    """Queue rows older than 7 days are NOT processed (and don't get
    applied_at set on this pass — they're filtered out by the WHERE)."""
    project = project_factory("widenstale")
    m = memory_factory(project["id"], tier="lesson")
    _set_applies_when(conn, m["id"], None)

    # Queue a row, then back-date it past the 7-day window.
    queue_refinement(
        conn,
        _make_finding(
            relevant_memory_id=m["id"],
            file="x.py",
            message="stale finding text",
        ),
    )
    pending = _select_pending_queue_ids(conn, m["id"])
    assert len(pending) == 1
    queue_id = pending[0]

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.refinement_queue "
            "SET queued_at = NOW() - INTERVAL '8 days' WHERE id = %s",
            (queue_id,),
        )
    conn.commit()

    refine_applies_when(conn, project["id"])

    # applies_when should NOT have been widened.
    aw = _read_applies_when(conn, m["id"])
    assert aw is None, "stale queue row must not be processed"

    # Queue row remains untouched (no applied_at).
    row = _read_queue_row(conn, queue_id)
    assert row["applied_at"] is None


@pytest.mark.db
def test_widen_applies_when_no_op_when_memory_id_missing(conn, project_factory):
    """Direct call to _widen_applies_when with a non-existent memory_id
    must early-return without raising (defensive — the JOIN in the
    public dequeue normally filters this out, but the helper is callable
    directly)."""
    from curator.refinement import _widen_applies_when
    project_factory("widenmissingdirect")  # ensure DB connection is alive

    fake = uuid4()
    # Should not raise.
    _widen_applies_when(conn, fake, "x/*.py", ["alpha"])


@pytest.mark.db
def test_refine_applies_when_no_op_when_memory_row_missing(
    conn, project_factory, memory_factory
):
    """If the memory row has been deleted between queue_refinement and
    refine_applies_when, the JOIN in the dequeue filters the row out so
    we don't blow up. (Defensive coverage for the early return in
    _widen_applies_when, which would only fire if the JOIN somehow let
    a stale row through — for example, manual table tampering — but is
    cheap to keep.)"""
    project = project_factory("widenmissing")
    m = memory_factory(project["id"], tier="lesson")

    queue_refinement(
        conn,
        _make_finding(
            relevant_memory_id=m["id"],
            file="x.py",
            message="finding text here",
        ),
    )

    # Delete the memory row directly. ON DELETE CASCADE will remove the
    # queue row too — refine should run cleanly with nothing to do.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.memory WHERE id = %s", (m["id"],))
    conn.commit()

    # Should not raise.
    refine_applies_when(conn, project["id"])
