"""DB tests for content-derived atom identity in cognify extract.

A new extracted atom's id is its content key (uuid5 over project, kind and
normalized title); the key is also stored in applies_when.content_key with
the verified quote. Re-runs and re-observations merge instead of adding
rows. Requires Postgres with migrations applied (pytest-db CI job).
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import cognify.extract as ex
import pytest
from cognify import backends, relate
from cognify.relate import RelateConfig

pytestmark = pytest.mark.db

QUOTE = "We keep one advisory lock per pass and project."


@pytest.fixture(autouse=True)
def _no_side_services(monkeypatch):
    monkeypatch.setattr(ex, "embed_text", lambda text: None)
    relate.reset_config(RelateConfig(mode="off"))
    yield
    relate.reset_config(None)


def _row(conn, mid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, provenance_id, strength, applies_when, archived_at, "
            "       extraction_version "
            "FROM devbrain.memory WHERE id = %s",
            (mid,),
        )
        return cur.fetchone()


def _live_atoms(conn, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory WHERE project_id = %s "
            "AND tier = 'lesson' AND archived_at IS NULL",
            (project_id,),
        )
        return cur.fetchone()[0]


def _upsert(conn, project_id, session_id, title, report=None, **kw):
    return ex._upsert_memory(
        conn, project_id=project_id, kind="lesson", title=title,
        content=kw.pop("content", "body"), session_id=session_id,
        quote=kw.pop("quote", QUOTE), report=report, **kw,
    )


def test_new_row_id_is_content_key_and_quote_is_stored(conn, project_factory):
    p = project_factory("ckey_new")
    s = str(uuid.uuid4())
    report: dict = {}
    mid, was_new = _upsert(conn, p["id"], s, "Advisory locks per pass", report)
    assert was_new and report["outcome"] == "created"
    key = ex.content_key(p["id"], "pattern", "Advisory locks per pass", "body")
    assert str(mid) == str(key)
    row = _row(conn, mid)
    assert row[3]["quote"] == QUOTE
    assert row[3]["content_key"] == str(key)
    assert row[3]["source_session"] == s
    assert row[5] == ex.CURRENT_EXTRACTION_VERSION


def test_rerun_with_reworded_title_is_a_duplicate(conn, project_factory):
    p = project_factory("ckey_rerun")
    s = str(uuid.uuid4())
    mid, _ = _upsert(conn, p["id"], s, "Use UUID5 ids!")
    report: dict = {}
    mid2, was_new = _upsert(conn, p["id"], s, "use  uuid5 IDs", report)
    assert (mid2, was_new) == (mid, False)
    assert report["outcome"] == "duplicate"
    assert _row(conn, mid)[2] == 1.0  # a re-run is not a new observation
    assert _live_atoms(conn, p["id"]) == 1


def test_same_atom_from_another_session_merges(conn, project_factory):
    p = project_factory("ckey_merge")
    s1, s2 = str(uuid.uuid4()), str(uuid.uuid4())
    mid, _ = _upsert(conn, p["id"], s1, "Pin the codex model")
    with conn.cursor() as cur:
        cur.execute("UPDATE devbrain.memory SET strength = 0.5 WHERE id = %s", (mid,))
    conn.commit()
    report: dict = {}
    mid2, was_new = _upsert(conn, p["id"], s2, "Pin the Codex model.", report)
    assert (mid2, was_new) == (mid, False)
    assert report["outcome"] == "merged"
    row = _row(conn, mid)
    assert float(row[2]) == pytest.approx(0.5 + relate.REINFORCE_STRENGTH_BUMP)
    assert row[3]["also_seen_in"] == [s2]
    assert str(row[1]) == s1  # provenance stays with the first session
    # A second merge from the same session does not append twice.
    _upsert(conn, p["id"], s2, "Pin the codex model")
    assert _row(conn, mid)[3]["also_seen_in"] == [s2]
    assert _live_atoms(conn, p["id"]) == 1


def test_kinds_and_projects_do_not_merge(conn, project_factory):
    p, q = project_factory("ckey_a"), project_factory("ckey_b")
    s = str(uuid.uuid4())
    a, _ = _upsert(conn, p["id"], s, "Shared title")
    b, new_b = _upsert(conn, q["id"], str(uuid.uuid4()), "Shared title")
    c, new_c = ex._upsert_memory(
        conn, project_id=p["id"], kind="decision", title="Shared title",
        content="body", session_id=s, quote=QUOTE,
    )
    assert new_b and new_c
    assert len({str(a), str(b), str(c)}) == 3


def test_archived_holder_of_the_key_is_left_alone(conn, project_factory):
    """An archived row (re-extraction, gc) keeps the key as its id. A later
    session's atom with the same key gets a random id, is not merged into
    the archived row, and is itself found by key afterwards."""
    p = project_factory("ckey_arch")
    s1, s2, s3 = (str(uuid.uuid4()) for _ in range(3))
    old, _ = _upsert(conn, p["id"], s1, "Archive then re-extract")
    assert ex._archive_prior_extracts(conn, s1, p["id"]) == 1
    new, was_new = _upsert(conn, p["id"], s2, "Archive then re-extract",
                           reextract=True, reextract_meta=str(old))
    assert was_new and str(new) != str(old)
    assert _row(conn, old)[4] is not None  # still archived
    new_row = _row(conn, new)
    assert new_row[3]["content_key"] == str(old)
    assert new_row[3]["reextracted_from"] == str(old)
    # Another session's observation finds the live row through applies_when.
    report: dict = {}
    hit, was_new = _upsert(conn, p["id"], s3, "archive then re-extract", report)
    assert (str(hit), was_new, report["outcome"]) == (str(new), False, "merged")


def test_same_session_title_held_by_archived_row_is_quiet(conn, project_factory):
    """Migration 037's (session, kind, title) index also covers archived
    rows, so re-extracting a session cannot re-insert an identical title.
    That used to raise; it is now reported as a duplicate."""
    p = project_factory("ckey_arch_same")
    s = str(uuid.uuid4())
    _upsert(conn, p["id"], s, "Same title again")
    ex._archive_prior_extracts(conn, s, p["id"])
    report: dict = {}
    _, was_new = _upsert(conn, p["id"], s, "Same title again", report)
    assert was_new is False and report["outcome"] == "duplicate"


def _seed_session(conn, project_id, text):
    sid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'session_summary', 'Summary', %s, %s::uuid)",
            (project_id, text, sid),
        )
    conn.commit()
    return sid


def test_extract_from_session_end_to_end(conn, project_factory):
    p = project_factory("ckey_e2e")
    text = (
        "We keep one advisory lock per pass and project.\n"
        "Codex must be called with an explicit -m flag."
    )
    s1 = _seed_session(conn, p["id"], text)
    s2 = _seed_session(conn, p["id"], text)
    payload = {
        "lessons": [
            {"title": "One lock per pass", "content": "c", "quote": QUOTE},
            {"title": "Made up", "content": "c", "quote": "Nobody said this."},
        ],
        "decisions": [
            {"title": "Explicit codex model", "content": "c",
             "quote": "Codex must be called\nwith an explicit -m flag."},
            {"title": "No evidence", "content": "c"},
        ],
        "_usage": {},
    }
    backends.reset(frozenset({"anthropic"}), selected="anthropic")
    with patch("cognify.extract._llm_extract", return_value=dict(payload)):
        first = ex.extract_from_session(conn, s1, p["id"])
        again = ex.extract_from_session(conn, s1, p["id"])
        other = ex.extract_from_session(conn, s2, p["id"])
    assert (first.lessons_created, first.decisions_created) == (1, 1)
    assert first.item_outcomes == {"created": 2, "quote_not_found": 1, "no_quote": 1}
    assert again.item_outcomes == {"duplicate": 2, "quote_not_found": 1, "no_quote": 1}
    assert again.zero_yield_reason == "all_known"
    assert other.item_outcomes == {"merged": 2, "quote_not_found": 1, "no_quote": 1}
    assert _live_atoms(conn, p["id"]) == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applies_when->>'quote' FROM devbrain.memory "
            "WHERE project_id = %s AND kind = 'decision' AND tier = 'lesson'",
            (p["id"],),
        )
        assert cur.fetchone()[0] == "Codex must be called with an explicit -m flag."
