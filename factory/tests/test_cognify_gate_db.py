"""DB-backed tests for the extract gate (cognify/gate.py) and the extract
pass wiring, plus the _sessions_since re-extraction loop fix."""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC

import pytest
from cognify import gate
from cognify.gate import GateConfig

pytestmark = pytest.mark.db

DIM = 1024


def vec(cos: float) -> str:
    v = [0.0] * DIM
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return "[" + ",".join(str(x) for x in v) + "]"


@pytest.fixture
def raw_session(conn):
    """Insert raw_sessions rows (project_id NULL keeps project cleanup
    simple); deleted at teardown."""
    made: list = []

    def make(messages: list[dict], *, path: str = "/home/u/.claude/projects/p/s.jsonl"):
        rid = uuid.uuid4()
        raw = json.dumps({"usf_version": "1.0", "messages": messages})
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.raw_sessions "
                "(id, source_app, source_path, source_hash, raw_content) "
                "VALUES (%s, 'test', %s, %s, %s)",
                (rid, path, uuid.uuid4().hex, raw),
            )
        conn.commit()
        made.append(rid)
        return rid

    yield make
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.raw_sessions WHERE id = ANY(%s::uuid[])",
            ([str(m) for m in made],),
        )
    conn.commit()


def add_row(
    conn,
    project_id,
    provenance,
    *,
    kind="chunk",
    content="x" * 800,
    emb=None,
    tier="memory",
    created_offset_s=0,
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, embedding, tier, provenance_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s::vector, %s, %s, now() + make_interval(secs => %s)) "
            "RETURNING id",
            (
                project_id,
                kind,
                None if kind == "chunk" else f"t-{uuid.uuid4().hex[:6]}",
                content,
                emb,
                tier,
                str(provenance),
                created_offset_s,
            ),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def chat(role, content):
    return {"role": role, "content": content, "tool_calls": []}


def cfg(**kw):
    base = {"mode": "on"}
    base.update(kw)
    return GateConfig(**base)


# ── features / shapes ───────────────────────────────────────────────────────


def test_raw_transcript_turns_and_tool_share(conn, project_factory, raw_session):
    p = project_factory("gate_raw")
    rid = raw_session(
        [
            chat("user", "<environment_context>cwd</environment_context>"),
            chat("user", "Please fix the flaky test."),
            chat("assistant", "Looking."),
            chat("user", "[tool_result] " + "y" * 300),
            chat("user", "Thanks, ship it."),
        ]
    )
    add_row(conn, p["id"], rid)
    d = gate.evaluate(conn, str(rid), p["id"], cfg(novelty=False))
    assert d.features.shape == "raw"
    assert d.features.human_turns == 2
    assert 0.5 < d.features.tool_share < 0.9
    assert d.action == "extract"


def test_raw_without_human_turns_is_skipped(conn, project_factory, raw_session):
    p = project_factory("gate_nohuman")
    rid = raw_session(
        [
            chat("assistant", "Message Type: NEW_TASK"),
            chat("user", "[tool_result] ok"),
        ]
    )
    add_row(conn, p["id"], rid)
    d = gate.evaluate(conn, str(rid), p["id"], cfg(novelty=False))
    assert d.action == "skip" and d.reasons == ["no_human_turns"]


def test_subagent_path_is_skipped(conn, project_factory, raw_session):
    p = project_factory("gate_path")
    rid = raw_session(
        [chat("user", "do it")], path="/h/.claude/projects/p/abc/subagents/a.jsonl"
    )
    add_row(conn, p["id"], rid)
    assert "path" in gate.evaluate(conn, str(rid), p["id"], cfg(novelty=False)).reasons


def test_non_json_transcript_has_no_turn_verdict(conn, project_factory):
    p = project_factory("gate_txt")
    rid = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.raw_sessions (id, source_app, source_path, source_hash, raw_content) "
            "VALUES (%s, 'test', '/x.jsonl', %s, 'plain text transcript')",
            (rid, uuid.uuid4().hex),
        )
    conn.commit()
    try:
        add_row(conn, p["id"], rid)
        d = gate.evaluate(conn, str(rid), p["id"], cfg(novelty=False))
        assert d.features.shape == "raw" and d.features.human_turns is None
        assert d.action == "extract"
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM devbrain.memory WHERE provenance_id = %s", (rid,))
            cur.execute("DELETE FROM devbrain.raw_sessions WHERE id = %s", (rid,))
        conn.commit()


def test_closure_backfill_summary_is_skipped(conn, project_factory):
    p = project_factory("gate_closure")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.chunks (project_id, source_type, content, metadata) "
            "VALUES (%s, 'session_summary', 'summary', %s) RETURNING id",
            (p["id"], json.dumps({"cli": "closure-backfill"})),
        )
        chunk_id = cur.fetchone()[0]
    conn.commit()
    try:
        add_row(conn, p["id"], chunk_id, kind="session_summary")
        d = gate.evaluate(conn, str(chunk_id), p["id"], cfg(novelty=False))
        assert d.features.shape == "summary" and d.features.cli == "closure-backfill"
        assert d.action == "skip" and d.reasons == ["cli:closure-backfill"]
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.memory WHERE provenance_id = %s", (chunk_id,)
            )
            cur.execute("DELETE FROM devbrain.chunks WHERE id = %s", (chunk_id,))
        conn.commit()


def test_atom_unit_and_short_breadcrumbs(conn, project_factory):
    p = project_factory("gate_shapes")
    atom_unit = uuid.uuid4()
    add_row(conn, p["id"], atom_unit, kind="decision")
    crumbs = uuid.uuid4()
    add_row(conn, p["id"], crumbs, kind="session_breadcrumb", content="Intent: fix it.")
    assert gate.evaluate(conn, str(atom_unit), p["id"], cfg()).reasons == [
        "atom_source"
    ]
    d = gate.evaluate(conn, str(crumbs), p["id"], cfg(novelty=False))
    assert d.features.shape == "breadcrumbs" and d.reasons == ["too_short"]


def test_novelty_against_older_atoms_only(conn, project_factory):
    p = project_factory("gate_novel")
    unit = uuid.uuid4()
    add_row(conn, p["id"], unit, kind="session_summary", emb=vec(1.0))
    # An older atom covering the unit, and a newer one that must be ignored.
    add_row(
        conn,
        p["id"],
        uuid.uuid4(),
        kind="decision",
        emb=vec(0.97),
        tier="lesson",
        created_offset_s=-3600,
    )
    d = gate.evaluate(conn, str(unit), p["id"], cfg())
    assert d.features.novel_share == 0.0 and d.action == "downgrade"
    assert d.reasons == ["low_novelty"]

    p2 = project_factory("gate_novel2")
    unit2 = uuid.uuid4()
    add_row(conn, p2["id"], unit2, kind="session_summary", emb=vec(1.0))
    add_row(
        conn,
        p2["id"],
        uuid.uuid4(),
        kind="decision",
        emb=vec(0.97),
        tier="lesson",
        created_offset_s=3600,
    )  # newer than the unit
    d2 = gate.evaluate(conn, str(unit2), p2["id"], cfg())
    assert d2.features.novel_share == 1.0 and d2.action == "extract"


def test_evaluate_leaves_callers_transaction_alone(conn, project_factory):
    """The gate is read-only and uses a savepoint: uncommitted caller work
    survives an evaluation, including one that errors."""
    p = project_factory("gate_txn")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE gate_probe (x int)")
        cur.execute("INSERT INTO gate_probe VALUES (1)")
    gate.evaluate(conn, str(uuid.uuid4()), p["id"], cfg())
    d = gate.evaluate(conn, "not-a-uuid", p["id"], cfg())  # SQL error inside
    assert d.action == "extract" and d.reasons == ["gate_error"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM gate_probe")
        assert cur.fetchone()[0] == 1
    conn.rollback()


# ── extract pass wiring ─────────────────────────────────────────────────────


def _run_pass(conn, project_id, monkeypatch, mode):
    import cognify.extract as ex

    calls: list[str] = []

    def fake_extract(conn_, session_id, project_id_, *, max_llm_calls=20, **kw):
        calls.append(session_id)
        return ex.ExtractResult(session_id=session_id, llm_calls=1)

    monkeypatch.setattr(ex, "extract_from_session", fake_extract)
    gate.reset_config(cfg(mode=mode, novelty=True))
    try:
        result = ex.ExtractPass().run(conn, project_id)
    finally:
        gate.reset_config(None)
    return calls, result


def _three_units(conn, p):
    atom_unit = uuid.uuid4()
    add_row(conn, p["id"], atom_unit, kind="decision")
    low = uuid.uuid4()  # downgrade: covered by an older atom
    add_row(
        conn,
        p["id"],
        uuid.uuid4(),
        kind="decision",
        emb=vec(0.99),
        tier="lesson",
        created_offset_s=-3600,
    )
    add_row(conn, p["id"], low, kind="session_summary", emb=vec(1.0))
    good = uuid.uuid4()
    add_row(conn, p["id"], good, kind="session_summary", emb=vec(0.0))
    return str(atom_unit), str(low), str(good)


def test_pass_on_skips_and_defers(conn, project_factory, monkeypatch):
    p = project_factory("gate_pass_on")
    atom_unit, low, good = _three_units(conn, p)
    calls, result = _run_pass(conn, p["id"], monkeypatch, "on")
    assert atom_unit not in calls
    assert calls.index(good) < calls.index(low)  # downgraded unit runs last
    g = result.metadata["gate"]
    assert (
        g["mode"] == "on" and g["counts"]["skip"] >= 1 and g["counts"]["downgrade"] == 1
    )
    assert atom_unit in g["ids"]["skip"] and low in g["ids"]["downgrade"]


def test_pass_dry_run_extracts_everything(conn, project_factory, monkeypatch):
    p = project_factory("gate_pass_dry")
    atom_unit, low, good = _three_units(conn, p)
    calls, result = _run_pass(conn, p["id"], monkeypatch, "dry_run")
    assert {atom_unit, low, good} <= set(calls)
    assert result.metadata["gate"]["counts"]["skip"] >= 1


def test_pass_off_has_no_gate_metadata(conn, project_factory, monkeypatch):
    p = project_factory("gate_pass_off")
    _three_units(conn, p)
    _, result = _run_pass(conn, p["id"], monkeypatch, "off")
    assert result.metadata["gate"] is None


# ── loop fix ────────────────────────────────────────────────────────────────


def test_extracted_atoms_do_not_requalify_their_unit(conn, project_factory):
    """Atoms carry the unit's provenance_id; they must not make the unit a
    candidate again (the re-extraction loop behind the duplicate atoms)."""
    from datetime import datetime, timedelta

    from cognify.extract import _sessions_since

    p = project_factory("gate_loop")
    unit = uuid.uuid4()
    add_row(conn, p["id"], unit, kind="session_breadcrumb", created_offset_s=-7200)
    since = datetime.now(UTC) - timedelta(seconds=60)
    add_row(conn, p["id"], unit, kind="decision", tier="lesson")  # extracted atom
    assert str(unit) not in _sessions_since(conn, p["id"], since)
    assert str(unit) in _sessions_since(conn, p["id"], None)
    # New source content (another breadcrumb) still makes it a candidate.
    add_row(conn, p["id"], unit, kind="session_breadcrumb")
    assert str(unit) in _sessions_since(conn, p["id"], since)
