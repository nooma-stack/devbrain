"""DB-backed tests for the write-time relation check (cognify/relate.py).

The model is stubbed (``chat=``); embeddings are hand-built unit vectors so
cosine similarities are exact. Requires Postgres with migrations through
048 applied (the pytest-db CI job).
"""

from __future__ import annotations

import json
import math

import pytest
from cognify import relate
from cognify.relate import RelateConfig

pytestmark = pytest.mark.db

DIM = 1024


def vec(cos: float) -> str:
    """Unit vector at cosine ``cos`` from the base vector e0."""
    v = [0.0] * DIM
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return "[" + ",".join(str(x) for x in v) + "]"


BASE = vec(1.0)


def chat_returning(relation: str, match: int = 1, confidence: float = 0.95):
    calls = []

    def fn(cfg, messages):
        calls.append(messages)
        return json.dumps(
            {"relation": relation, "match": match, "confidence": confidence}
        )

    fn.calls = calls
    return fn


def cfg(mode: str = "on") -> RelateConfig:
    return RelateConfig(mode=mode, threshold=0.80, edge_min_similarity=0.85)


def insert_atom(
    conn,
    project_id,
    *,
    emb: str,
    kind="decision",
    title="t",
    content="c",
    strength=0.5,
    archived=False,
    created_offset_s=0,
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, embedding, strength, tier, archived_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s::vector, %s, 'lesson', "
            "        CASE WHEN %s THEN now() END, now() + make_interval(secs => %s)) "
            "RETURNING id",
            (
                project_id,
                kind,
                title,
                content,
                emb,
                strength,
                archived,
                created_offset_s,
            ),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def log_rows(conn, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, matched_memory_id, relation, model_relation, mode, "
            "       applied, review_status, source "
            "FROM devbrain.memory_relation_log WHERE project_id = %s ORDER BY id",
            (project_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def edges(conn, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.from_memory_id, d.to_memory_id, d.edge_type, d.created_by "
            "FROM devbrain.memory_dependencies d "
            "JOIN devbrain.memory m ON m.id = d.from_memory_id "
            "WHERE m.project_id = %s",
            (project_id,),
        )
        return cur.fetchall()


def row(conn, mid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength::float, last_hit, archived_at, hit_count "
            "FROM devbrain.memory WHERE id = %s",
            (mid,),
        )
        return cur.fetchone()


def precheck(conn, project_id, c, chat, emb=None):
    return relate.precheck(
        conn,
        c,
        project_id=project_id,
        kind="decision",
        title="new",
        content="new body",
        embedding_literal=emb or vec(0.95),
        source="extract",
        source_ref="sess-1",
        chat=chat,
    )


def write_new(conn, project_id, pre, c, emb=None):
    """What the extract path does after a non-skipping precheck."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, embedding, tier) "
            "VALUES (%s, 'decision', 'new', 'new body', %s::vector, 'lesson') RETURNING id",
            (project_id, emb or vec(0.95)),
        )
        mid = cur.fetchone()[0]
    pre.finish(
        conn,
        project_id=project_id,
        new_id=mid,
        cfg=c,
        source="extract",
        source_ref="sess-1",
    )
    conn.commit()
    return mid


# ── sync path (extract) ─────────────────────────────────────────────────────


def test_same_skips_insert_and_reinforces(conn, project_factory):
    p = project_factory("rel_same")
    old = insert_atom(conn, p["id"], emb=BASE, strength=0.5)
    pre = precheck(conn, p["id"], cfg(), chat_returning("same"))
    assert pre.skip_insert and pre.existing_id == old
    strength, last_hit, archived, hits = row(conn, old)
    assert strength == pytest.approx(0.6)
    assert last_hit is not None and archived is None
    assert hits == 0  # hit_count is graduation's violation signal; untouched
    [log] = log_rows(conn, p["id"])
    assert log["memory_id"] is None and log["matched_memory_id"] == old
    assert log["relation"] == "same" and log["applied"] is True and log["mode"] == "on"


def test_refines_writes_with_refined_by_edge(conn, project_factory):
    p = project_factory("rel_ref")
    old = insert_atom(conn, p["id"], emb=BASE)
    c = cfg()
    pre = precheck(conn, p["id"], c, chat_returning("refines"))
    assert not pre.skip_insert
    new = write_new(conn, p["id"], pre, c)
    assert edges(conn, p["id"]) == [(old, new, "refined_by", "relate:extract")]
    [log] = log_rows(conn, p["id"])
    assert log["memory_id"] == new and log["relation"] == "refines" and log["applied"]
    assert log["review_status"] is None


def test_contradicts_supersedes_flags_and_cascades(conn, project_factory):
    p = project_factory("rel_con")
    old = insert_atom(conn, p["id"], emb=BASE)
    # A row that depends on the old one gets queued for re-evaluation.
    dependent = insert_atom(conn, p["id"], emb=vec(0.1), title="dep")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies (from_memory_id, to_memory_id, edge_type) "
            "VALUES (%s, %s, 'depends_on')",
            (dependent, old),
        )
    conn.commit()
    c = cfg()
    pre = precheck(conn, p["id"], c, chat_returning("contradicts"))
    new = write_new(conn, p["id"], pre, c)
    assert (new, old, "supersedes", "relate:extract") in edges(conn, p["id"])
    [log] = log_rows(conn, p["id"])
    assert log["relation"] == "contradicts" and log["review_status"] == "pending"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s AND edge_type = 'supersedes'",
            (old,),
        )
        assert [r[0] for r in cur.fetchall()] == [dependent]


def test_shadow_logs_but_never_acts(conn, project_factory):
    p = project_factory("rel_shadow")
    old = insert_atom(conn, p["id"], emb=BASE, strength=0.5)
    c = cfg("shadow")
    pre = precheck(conn, p["id"], c, chat_returning("same"))
    assert not pre.skip_insert
    write_new(conn, p["id"], pre, c)
    assert row(conn, old)[0] == pytest.approx(0.5)
    assert edges(conn, p["id"]) == []
    [log] = log_rows(conn, p["id"])
    assert (
        log["relation"] == "same"
        and log["applied"] is False
        and log["mode"] == "shadow"
    )


def test_below_threshold_no_model_call(conn, project_factory):
    p = project_factory("rel_far")
    insert_atom(conn, p["id"], emb=BASE)
    chat = chat_returning("same")
    c = cfg()
    pre = precheck(conn, p["id"], c, chat, emb=vec(0.5))
    assert not pre.skip_insert and chat.calls == []
    write_new(conn, p["id"], pre, c, emb=vec(0.5))
    [log] = log_rows(conn, p["id"])
    assert log["relation"] == "new" and log["matched_memory_id"] is None


def test_candidates_are_project_scoped_and_live(conn, project_factory):
    p = project_factory("rel_iso_a")
    other = project_factory("rel_iso_b")
    insert_atom(conn, other["id"], emb=BASE)  # other project
    insert_atom(conn, p["id"], emb=BASE, archived=True)  # archived
    insert_atom(conn, p["id"], emb=BASE, kind="session_summary")  # not an atom
    chat = chat_returning("same")
    pre = precheck(conn, p["id"], cfg(), chat)
    assert not pre.skip_insert and chat.calls == []


def test_model_failure_is_plain_write(conn, project_factory):
    p = project_factory("rel_fail")
    insert_atom(conn, p["id"], emb=BASE)

    def fail(cfg, messages):
        raise TimeoutError("slow")

    c = cfg()
    pre = precheck(conn, p["id"], c, fail)
    assert not pre.skip_insert
    write_new(conn, p["id"], pre, c)
    [log] = log_rows(conn, p["id"])
    assert log["relation"] == "error" and log["applied"] is False
    assert edges(conn, p["id"]) == []


# ── async path (store / end_session) ────────────────────────────────────────


def test_relate_existing_same_archives_new_row(conn, project_factory):
    p = project_factory("rel_async")
    old = insert_atom(conn, p["id"], emb=BASE, strength=0.5, created_offset_s=-60)
    new = insert_atom(conn, p["id"], emb=vec(0.95))
    v = relate.relate_existing(
        conn, cfg(), new, source="mcp:store", chat=chat_returning("same")
    )
    assert v.relation == "same" and v.match.id == old
    assert row(conn, new)[2] is not None  # archived, never deleted
    assert row(conn, old)[0] == pytest.approx(0.6)
    [log] = log_rows(conn, p["id"])
    assert log["memory_id"] == new and log["source"] == "mcp:store" and log["applied"]
    # Idempotent: a checked row is not classified again.
    assert (
        relate.relate_existing(
            conn, cfg(), new, source="mcp:store", chat=chat_returning("same")
        )
        is None
    )


def test_relate_existing_only_compares_older_rows(conn, project_factory):
    p = project_factory("rel_order")
    first = insert_atom(conn, p["id"], emb=BASE, created_offset_s=-60)
    insert_atom(conn, p["id"], emb=vec(0.99))  # a later duplicate of `first`
    chat = chat_returning("same")
    v = relate.relate_existing(conn, cfg(), first, source="mcp:store", chat=chat)
    assert v.relation == "new" and chat.calls == []
    assert row(conn, first)[2] is None


def test_relate_existing_dry_run_writes_nothing(conn, project_factory):
    p = project_factory("rel_dry")
    insert_atom(conn, p["id"], emb=BASE, created_offset_s=-60)
    new = insert_atom(conn, p["id"], emb=vec(0.95))
    v = relate.relate_existing(
        conn, cfg(), new, source="cli", chat=chat_returning("same"), dry_run=True
    )
    assert v.relation == "same"
    assert row(conn, new)[2] is None
    assert log_rows(conn, p["id"]) == []


def test_sweep_checks_unchecked_rows_oldest_first(conn, project_factory):
    p = project_factory("rel_sweep")
    a = insert_atom(conn, p["id"], emb=BASE, created_offset_s=-120)
    b = insert_atom(conn, p["id"], emb=vec(0.97), created_offset_s=-60)
    counts = relate.sweep(conn, cfg(), project_id=p["id"], chat=chat_returning("same"))
    # a has nothing older → new; b duplicates a → archived.
    assert counts == {"new": 1, "same": 1}
    assert row(conn, b)[2] is not None and row(conn, a)[2] is None
    # Everything is checked now; a second sweep finds nothing.
    assert (
        relate.sweep(conn, cfg(), project_id=p["id"], chat=chat_returning("same")) == {}
    )


def test_sweep_off_is_noop(conn, project_factory):
    p = project_factory("rel_sweep_off")
    insert_atom(conn, p["id"], emb=BASE)
    assert relate.sweep(conn, cfg("off"), project_id=p["id"]) == {}
    assert log_rows(conn, p["id"]) == []


# ── extract integration ─────────────────────────────────────────────────────


def test_extract_upsert_dedupes_across_sessions(conn, project_factory, monkeypatch):
    import uuid

    import cognify.extract as ex

    p = project_factory("rel_extract")
    monkeypatch.setattr(ex, "embed_text", lambda text: [1.0] + [0.0] * (DIM - 1))
    monkeypatch.setattr(
        relate,
        "_ollama_chat",
        lambda c, m: json.dumps({"relation": "same", "match": 1, "confidence": 0.95}),
    )
    relate.reset_config(cfg())
    try:
        first, was_new = ex._upsert_memory(
            conn,
            project_id=p["id"],
            kind="decision",
            title="Use X",
            content="We use X.",
            session_id=str(uuid.uuid4()),
        )
        assert was_new
        second, was_new = ex._upsert_memory(
            conn,
            project_id=p["id"],
            kind="decision",
            title="Adopt X",
            content="X is what we use.",
            session_id=str(uuid.uuid4()),
        )
        assert not was_new and second == first
    finally:
        relate.reset_config(None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory WHERE project_id = %s AND archived_at IS NULL",
            (p["id"],),
        )
        assert cur.fetchone()[0] == 1
    logs = log_rows(conn, p["id"])
    assert [r["relation"] for r in logs] == ["new", "same"]


def test_extract_upsert_unchanged_when_off(conn, project_factory, monkeypatch):
    import uuid

    import cognify.extract as ex

    p = project_factory("rel_extract_off")
    monkeypatch.setattr(ex, "embed_text", lambda text: [1.0] + [0.0] * (DIM - 1))
    relate.reset_config(cfg("off"))
    try:
        for title in ("Use X", "Adopt X"):
            _, was_new = ex._upsert_memory(
                conn,
                project_id=p["id"],
                kind="decision",
                title=title,
                content="We use X.",
                session_id=str(uuid.uuid4()),
            )
            assert was_new
    finally:
        relate.reset_config(None)
    assert log_rows(conn, p["id"]) == []


def test_relate_existing_keeps_rows_the_writer_linked(conn, project_factory):
    """A store() call that attached its own edges linked the row on purpose;
    a "same" verdict is logged but the row is not archived."""
    p = project_factory("rel_linked")
    old = insert_atom(conn, p["id"], emb=BASE, created_offset_s=-60)
    other = insert_atom(conn, p["id"], emb=vec(0.1), created_offset_s=-120)
    new = insert_atom(conn, p["id"], emb=vec(0.95))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'supersedes', 'mcp:store')",
            (new, other),
        )
    conn.commit()
    v = relate.relate_existing(
        conn, cfg(), new, source="mcp:store", chat=chat_returning("same")
    )
    assert v.relation == "same" and v.match.id == old
    assert row(conn, new)[2] is None
    [log] = log_rows(conn, p["id"])
    assert log["relation"] == "same" and log["applied"] is False
