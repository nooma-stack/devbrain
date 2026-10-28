"""DB tests for the outdated-memory sweep (cognify/sweep.py, migration 052).

The model is stubbed; embeddings are hand-built unit vectors so cosine
similarities are exact. Each "topic" lives in its own 2-D plane so atoms of
different topics are orthogonal.
"""

from __future__ import annotations

import json
import math
import uuid

import pytest
from cognify import relate, sweep
from cognify.orchestrator import run_pass
from cognify.relate import RelateConfig
from cognify.sweep import SweepConfig

pytestmark = pytest.mark.db

DIM = 1024


def vec(cos: float = 1.0, topic: int = 0, side: int = 1) -> str:
    """Unit vector at cosine ``cos`` from the topic's base axis. ``side``
    (+1/-1) picks which way it leans, so two vectors can both be close to
    the base but not to each other."""
    v = [0.0] * DIM
    v[2 * topic] = cos
    v[2 * topic + 1] = side * math.sqrt(max(0.0, 1.0 - cos * cos))
    return "[" + ",".join(str(x) for x in v) + "]"


def insert(conn, project_id, *, emb, age_days, kind="decision", title="t",
           prov=None, archived=False):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, tier, "
            " embedding, provenance_id, created_at, archived_at) "
            "VALUES (%s, %s, %s, %s, 'lesson', %s::vector, %s::uuid, "
            "        now() - make_interval(days => %s), "
            "        CASE WHEN %s THEN now() END) RETURNING id",
            (project_id, kind, title, f"content of {title}", emb,
             prov or str(uuid.uuid4()), age_days, archived),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def chat_answering(verdict="replaces", confidence=0.95):
    calls = []

    def fn(cfg, messages):
        calls.append(messages)
        return json.dumps({"verdict": verdict, "confidence": confidence})

    fn.calls = calls
    return fn


def run(conn, pid, *, chat, **cfg_kw):
    cfg = SweepConfig(**{"mode": "shadow", "lookback_days": None, **cfg_kw})
    return sweep.run_sweep(conn, pid, cfg, chat=chat, rel_cfg=RelateConfig())


def log_rows(conn, pid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT older_memory_id, newer_memory_id, verdict, mode, applied, "
            "       edge_type, review_status, round(similarity::numeric, 2)::float8 "
            "FROM devbrain.memory_sweep_log WHERE project_id = %s ORDER BY id",
            (pid,),
        )
        return cur.fetchall()


def edges(conn, pid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.from_memory_id, d.to_memory_id, d.edge_type, d.created_by "
            "FROM devbrain.memory_dependencies d "
            "JOIN devbrain.memory m ON m.id = d.from_memory_id "
            "WHERE m.project_id = %s ORDER BY d.id",
            (pid,),
        )
        return cur.fetchall()


def test_shadow_logs_pairs_and_writes_no_edges(conn, project_factory):
    p = project_factory("sweep_shadow")["id"]
    old = insert(conn, p, emb=vec(0.9), age_days=10, title="old")
    new = insert(conn, p, emb=vec(1.0), age_days=1, title="new")
    insert(conn, p, emb=vec(1.0, topic=1), age_days=5, title="other topic")
    chat = chat_answering("replaces", 0.95)
    res = run(conn, p, chat=chat)
    assert res.llm_calls == 1
    assert log_rows(conn, p) == [(old, new, "replaces", "shadow", False, None, None,
                                  pytest.approx(0.9))]
    assert edges(conn, p) == []
    md = res.metadata
    assert md["verdicts"] == {"replaces": 1}
    assert md["edges_earned"] == {"supersedes": 1}
    assert md["edges_written"] == {}
    # Memory text reaches the model, never the run log.
    assert "content of old" in chat.calls[0][1]["content"]
    assert "content of" not in json.dumps(md)


def test_on_mode_writes_supersedes_and_contradicts(conn, project_factory):
    p = project_factory("sweep_on")["id"]
    a_old = insert(conn, p, emb=vec(0.9), age_days=10, title="alpha-old")
    a_new = insert(conn, p, emb=vec(1.0), age_days=1, title="alpha-new")
    b_old = insert(conn, p, emb=vec(0.95, topic=1), age_days=10, title="beta-old")
    b_new = insert(conn, p, emb=vec(1.0, topic=1), age_days=1, title="beta-new")
    # A row that depends on the outdated one is queued for re-evaluation.
    dep = insert(conn, p, emb=vec(1.0, topic=5), age_days=20, title="dependent")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies (from_memory_id, to_memory_id, "
            "edge_type, created_by) VALUES (%s, %s, 'depends_on', 'test')",
            (dep, a_old),
        )
    conn.commit()

    def chat(cfg, messages):
        verdict = "replaces" if "alpha-old" in messages[1]["content"] else "contradicts"
        return json.dumps({"verdict": verdict, "confidence": 0.9})

    res = run(conn, p, chat=chat, mode="on")
    got = {(e[0], e[1], e[2]) for e in edges(conn, p) if e[3] == "cognify_sweep"}
    assert got == {(a_new, a_old, "supersedes"), (b_new, b_old, "contradicts")}
    assert res.metadata["edges_written"] == {"supersedes": 1, "contradicts": 1}
    assert res.rows_processed == 2
    rows = {(r[0], r[1]): r for r in log_rows(conn, p)}
    assert rows[(b_old, b_new)][6] == "pending"
    assert rows[(a_old, a_new)][4] is True
    assert rows[(a_old, a_new)][5] == "supersedes"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id FROM devbrain.curator_re_eval_queue "
            "WHERE cascade_source_id = %s",
            (a_old,),
        )
        assert [r[0] for r in cur.fetchall()] == [dep]
        # The edges went through the ledger trigger (subject = from side):
        # one INSERT row for each new atom plus one per edge.
        cur.execute(
            "SELECT count(*) FROM devbrain.memory_ledger "
            "WHERE memory_id = ANY(%s::uuid[])",
            ([str(a_new), str(b_new)],),
        )
        assert cur.fetchone()[0] == 4
        # Nothing archived or deleted.
        cur.execute(
            "SELECT count(*) FROM devbrain.memory WHERE project_id = %s "
            "AND archived_at IS NULL",
            (p,),
        )
        assert cur.fetchone()[0] == 5
    # A second run writes nothing new: the pairs are judged and linked.
    res2 = run(conn, p, chat=chat, mode="on")
    assert res2.llm_calls == 0


def test_low_confidence_and_refines_are_log_only(conn, project_factory):
    p = project_factory("sweep_lowconf")["id"]
    insert(conn, p, emb=vec(0.9), age_days=10)
    insert(conn, p, emb=vec(1.0), age_days=1)
    res = run(conn, p, chat=chat_answering("replaces", 0.6), mode="on")
    assert edges(conn, p) == []
    assert res.metadata["below_confidence"] == 1
    assert log_rows(conn, p)[0][4] is False


def test_pair_rules(conn, project_factory):
    """Below threshold, same session, archived and already-linked pairs are
    never judged; a pair is judged once per mode."""
    p = project_factory("sweep_rules")["id"]
    session = str(uuid.uuid4())
    new = insert(conn, p, emb=vec(1.0), age_days=1, prov=session, title="new")
    insert(conn, p, emb=vec(0.80), age_days=10, title="far")  # below 0.85
    insert(conn, p, emb=vec(0.97), age_days=10, prov=session, title="sibling")
    insert(conn, p, emb=vec(0.96), age_days=10, archived=True, title="archived")
    linked = insert(conn, p, emb=vec(0.95), age_days=10, title="linked")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies (from_memory_id, to_memory_id, "
            "edge_type, created_by) VALUES (%s, %s, 'refined_by', 'test')",
            (linked, new),
        )
    conn.commit()
    good = insert(conn, p, emb=vec(0.90), age_days=10, title="good")
    chat = chat_answering("unrelated", 0.9)
    run(conn, p, chat=chat, top_k=10)
    # (The older atoms also pair among themselves; only `new` matters here.)
    def pairs_of_new(mode):
        return [(r[0], r[1]) for r in log_rows(conn, p) if r[1] == new and r[3] == mode]

    assert pairs_of_new("shadow") == [(good, new)]
    # Second run: nothing new to judge.
    chat2 = chat_answering("unrelated", 0.9)
    res = run(conn, p, chat=chat2, top_k=10)
    assert chat2.calls == [] and res.metadata["pairs_found"] == 0
    # A different mode judges again.
    run(conn, p, chat=chat_answering("unrelated", 0.9), mode="on", top_k=10)
    assert pairs_of_new("on") == [(good, new)]


def test_model_errors_are_logged_and_retried(conn, project_factory):
    p = project_factory("sweep_err")["id"]
    insert(conn, p, emb=vec(0.9), age_days=10)
    insert(conn, p, emb=vec(1.0), age_days=1)

    def down(cfg, messages):
        raise ConnectionError("ollama down")

    res = run(conn, p, chat=down, mode="on")
    assert res.metadata["verdicts"] == {"error": 1}
    assert log_rows(conn, p)[0][2] == "error"
    chat = chat_answering("refines", 0.9)
    run(conn, p, chat=chat, mode="on")
    assert len(chat.calls) == 1


def test_dry_run_counts_pairs_without_model_or_writes(conn, project_factory):
    p = project_factory("sweep_dry")["id"]
    insert(conn, p, emb=vec(0.9), age_days=10)
    insert(conn, p, emb=vec(1.0), age_days=1)
    chat = chat_answering()
    cfg = SweepConfig(mode="on", lookback_days=None)
    res = sweep.run_sweep(conn, p, cfg, dry_run=True, chat=chat, rel_cfg=RelateConfig())
    assert res.metadata["pairs_found"] == 1 and res.llm_calls == 0
    assert chat.calls == [] and log_rows(conn, p) == []
    assert res.metadata["cursors"] == {}


def test_lookback_limits_first_run_anchors(conn, project_factory):
    p = project_factory("sweep_lookback")["id"]
    insert(conn, p, emb=vec(0.9), age_days=100)
    insert(conn, p, emb=vec(1.0), age_days=60)  # anchor outside 30 days
    chat = chat_answering()
    res = run(conn, p, chat=chat, lookback_days=30)
    assert res.metadata["anchors_scanned"] == 0 and chat.calls == []


def test_cap_and_cursor_resume_through_the_orchestrator(
    conn, project_factory, monkeypatch
):
    """With a cap of one call, a run stops inside an anchor's pairs; the
    cursor stays before that anchor and the next run resumes there."""
    p = project_factory("sweep_cursor")["id"]
    # o1 and o2 are both 0.9 from n1 but only ~0.62 from each other.
    o1 = insert(conn, p, emb=vec(0.9, side=1), age_days=30)
    m_old = insert(conn, p, emb=vec(0.9, topic=1), age_days=30)
    o2 = insert(conn, p, emb=vec(0.9, side=-1), age_days=29)
    n1 = insert(conn, p, emb=vec(1.0), age_days=5)
    m_new = insert(conn, p, emb=vec(1.0, topic=1), age_days=4)
    chat = chat_answering("refines", 0.9)
    monkeypatch.setattr(sweep, "_ollama_judge", chat)
    monkeypatch.setattr(
        sweep, "load_config", lambda: SweepConfig(mode="shadow", lookback_days=None)
    )
    monkeypatch.setattr(relate, "load_config", lambda: RelateConfig())

    first = run_pass(conn, "sweep", p, max_llm_calls=1)
    assert (first.llm_calls, first.metadata["capped"]) == (1, True)
    assert first.metadata["cursors"][str(p)]["id"] == str(o2)  # n1 unfinished

    second = run_pass(conn, "sweep", p, max_llm_calls=1)
    assert (second.llm_calls, second.metadata["capped"]) == (1, True)
    assert second.metadata["cursors"][str(p)]["id"] == str(n1)

    third = run_pass(conn, "sweep", p, max_llm_calls=5)
    assert (third.llm_calls, third.metadata["capped"]) == (1, False)
    assert third.metadata["cursors"][str(p)]["id"] == str(m_new)

    fourth = run_pass(conn, "sweep", p, max_llm_calls=5)
    assert fourth.llm_calls == 0
    assert fourth.metadata["cursors"][str(p)]["id"] == str(m_new)

    assert {(r[0], r[1]) for r in log_rows(conn, p)} == {
        (o1, n1), (o2, n1), (m_old, m_new),
    }
    assert len(chat.calls) == 3

    # A dry run never moves (or forgets) the cursor.
    run_pass(conn, "sweep", p, dry_run=True)
    assert sweep._last_cursor(conn, p, "shadow")[1] == str(m_new)


def test_no_project_sweeps_every_project(conn, project_factory):
    """The nightly job runs without --project: every project with atoms is
    swept, each with its own cursor, under one shared cap."""
    p = project_factory("sweep_all_a")["id"]
    q = project_factory("sweep_all_b")["id"]
    pairs = {}
    for pid in (p, q):
        old = insert(conn, pid, emb=vec(0.9, topic=7), age_days=10)
        new = insert(conn, pid, emb=vec(1.0, topic=7), age_days=1)
        pairs[pid] = (old, new)
    res = sweep.run_sweep(
        conn, None, SweepConfig(mode="shadow", lookback_days=None, max_pairs=500),
        chat=chat_answering("refines", 0.9), rel_cfg=RelateConfig(),
    )
    for pid in (p, q):
        assert [(r[0], r[1]) for r in log_rows(conn, pid)] == [pairs[pid]]
        assert res.metadata["cursors"][str(pid)]["id"] == str(pairs[pid][1])
    # Atoms of different projects are never paired, however similar.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory_sweep_log l "
            "JOIN devbrain.memory o ON o.id = l.older_memory_id "
            "JOIN devbrain.memory n ON n.id = l.newer_memory_id "
            "WHERE o.project_id <> n.project_id"
        )
        assert cur.fetchone()[0] == 0


def test_migration_052_shape(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'devbrain' "
            "AND tablename = 'memory_sweep_log'"
        )
        names = {r[0] for r in cur.fetchall()}
    assert {"uq_memory_sweep_pair", "idx_memory_sweep_newer",
            "idx_memory_sweep_project", "idx_memory_sweep_review"} <= names
