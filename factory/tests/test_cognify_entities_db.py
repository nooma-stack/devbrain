"""DB tests for the facts graph (migration 050): the cognify `entities`
writes (merge, retirement, archive) and the devbrain.facts_walk() walk
(multi-hop, alias seeding, retired facts skipped, no-seed, project scope).

The model is never called: extractions are built directly, or a fake LLM
returns fixed JSON for the end-to-end run."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cognify import entities as ent
from cognify.entities import Alias, Edge, Extraction, LLMResult, MemoryRow, Node

pytestmark = pytest.mark.db

T0 = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def graph(conn, project_factory):
    """A project plus helpers; every name carries a per-test token so the
    global entities table is cleaned up exactly."""
    token = "zq" + uuid.uuid4().hex[:8]
    projects = [project_factory("facts")]
    helper = _Graph(conn, token, projects, project_factory)
    yield helper
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.entity_relations WHERE project_id = ANY(%s::uuid[])",
            ([str(p["id"]) for p in projects],),
        )
        cur.execute(
            "DELETE FROM devbrain.cognify_run_log WHERE project_id = ANY(%s::uuid[])",
            ([str(p["id"]) for p in projects],),
        )
        cur.execute(
            "DELETE FROM devbrain.entities WHERE name_norm LIKE %s", (f"%{token}%",)
        )
    conn.commit()


class _Graph:
    def __init__(self, conn, token, projects, project_factory):
        self.conn = conn
        self.token = token
        self.projects = projects
        self._factory = project_factory

    @property
    def project_id(self):
        return self.projects[0]["id"]

    def other_project(self):
        p = self._factory("facts2")
        self.projects.append(p)
        return p["id"]

    def name(self, base: str) -> str:
        return f"{self.token}-{base}"

    def mem(self, days: int = 0, text: str = "") -> MemoryRow:
        m = MemoryRow(
            id=uuid.uuid4(), kind="decision", title="",
            content=text or "fixture memory text that states facts",
            created_at=T0 + timedelta(days=days),
        )
        m.prompt_text = m.stored_text
        return m

    def node(self, base: str, type_: str, mem: MemoryRow, *, desc: str = "",
             retired: bool = False) -> Node:
        name = self.name(base)
        return Node(id=ent.entity_id(type_, name), name=name, type=type_,
                    description=desc, memory=mem, retired=retired)

    def write(self, nodes=(), edges=(), aliases=(), project_id=None):
        ext = Extraction(nodes=list(nodes), edges=list(edges), aliases=list(aliases))
        return ent.write_extraction(self.conn, project_id or self.project_id, ext)

    def walk(self, text: str, *, hops: int = 3, top_k: int = 8, project_id="default"):
        pid = self.project_id if project_id == "default" else project_id
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT source_name, predicate, target_name, depth "
                "FROM devbrain.facts_walk(%s, %s, %s, %s)",
                (pid, text, hops, top_k),
            )
            rows = cur.fetchall()
        self.conn.rollback()
        return [(s.split("-", 1)[1], p, t.split("-", 1)[1], d) for s, p, t, d in rows]


def edge(src: Node, pred: str, tgt: Node, mem: MemoryRow, *, ended=False) -> Edge:
    return Edge(source=src, predicate=pred, target=tgt,
                quote="quoted from the memory", memory=mem, ended=ended)


def chain(g: _Graph):
    """opsdesk -runs_on-> vps -owned_by-> trifekta-ai <-member_of- mark."""
    m = g.mem()
    ops = g.node("opsdesk", "SERVICE", m, desc="devplane fork")
    vps = g.node("vps", "HOST", m, desc="2.24.99.121")
    org = g.node("trifekta-ai", "ORG", m)
    mark = g.node("mark", "PERSON", m)
    g.write(
        nodes=[ops, vps, org, mark],
        edges=[edge(ops, "runs_on", vps, m), edge(vps, "owned_by", org, m),
               edge(mark, "member_of", org, m)],
    )
    return ops, vps, org, mark


# ── Walk ─────────────────────────────────────────────────────────────────


def test_walk_multi_hop_ranked_by_nearness(graph):
    chain(graph)
    q = f"who owns the box {graph.name('opsdesk')} runs on?"
    assert graph.walk(q, hops=3) == [
        ("opsdesk", "runs_on", "vps", 0),
        ("vps", "owned_by", "trifekta-ai", 1),
        ("mark", "member_of", "trifekta-ai", 2),
    ]


def test_walk_respects_hops(graph):
    chain(graph)
    q = f"{graph.name('opsdesk')}"
    assert graph.walk(q, hops=1) == [("opsdesk", "runs_on", "vps", 0)]
    assert len(graph.walk(q, hops=2)) == 2
    assert graph.walk(q, hops=0) == []


def test_walk_top_k(graph):
    chain(graph)
    assert graph.walk(graph.name("opsdesk"), top_k=1) == [("opsdesk", "runs_on", "vps", 0)]


def test_walk_seeds_from_alias(graph):
    ops, vps, _, _ = chain(graph)
    m = graph.mem()
    graph.write(aliases=[Alias(node=vps, alias=f"{graph.token} Trifekta VPS", memory=m)])
    # Only the alias is in the text, in another case.
    rows = graph.walk(f"what runs on the {graph.token} TRIFEKTA vps?", hops=1)
    assert ("opsdesk", "runs_on", "vps", 0) in rows
    assert ("vps", "owned_by", "trifekta-ai", 0) in rows


def test_walk_matches_whole_words_only(graph):
    chain(graph)
    assert graph.walk(f"{graph.name('opsdesk')}x and x{graph.name('opsdesk')}") == []
    # Punctuation around a name is a boundary.
    assert graph.walk(f"({graph.name('opsdesk')}).")


def test_walk_no_seed_returns_nothing(graph):
    chain(graph)
    assert graph.walk("nothing in this text names an entity") == []
    assert graph.walk("") == []


def test_walk_project_scope(graph):
    chain(graph)
    other = graph.other_project()
    assert graph.walk(graph.name("opsdesk"), project_id=other) == []
    assert graph.walk(graph.name("opsdesk"), project_id=None)


def test_walk_dedupes_a_fact_stated_in_two_projects(graph):
    ops, vps, _, _ = chain(graph)
    other = graph.other_project()
    m = graph.mem(days=3)
    graph.write(nodes=[ops, vps], edges=[edge(ops, "runs_on", vps, m)], project_id=other)
    rows = graph.walk(graph.name("opsdesk"), hops=1, project_id=None)
    assert rows.count(("opsdesk", "runs_on", "vps", 0)) == 1


# ── Retirement ───────────────────────────────────────────────────────────


def _relation(conn, src: Node, pred: str, tgt: Node, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, retired_at IS NOT NULL, retired_reason, retired_by, valid_until "
            "FROM devbrain.entity_relations WHERE id = %s",
            (ent.relation_id(src.id, pred, tgt.id, project_id),),
        )
        row = cur.fetchone()
    conn.rollback()
    return row


def test_newer_single_valued_fact_retires_older(graph):
    m1, m2 = graph.mem(days=1), graph.mem(days=5)
    svc = graph.node("devbrain", "SERVICE", m1)
    laptop = graph.node("laptop", "HOST", m1)
    studio = graph.node("studio", "HOST", m2)
    graph.write(nodes=[svc, laptop], edges=[edge(svc, "runs_on", laptop, m1)])
    stats = graph.write(nodes=[svc, studio], edges=[edge(svc, "runs_on", studio, m2)])
    assert stats.relations_retired == 1
    assert graph.walk(graph.name("devbrain"), hops=1) == [("devbrain", "runs_on", "studio", 0)]
    old = _relation(graph.conn, svc, "runs_on", laptop, graph.project_id)
    new = _relation(graph.conn, svc, "runs_on", studio, graph.project_id)
    assert old[1] and old[2] == "superseded" and old[3] == new[0]
    assert old[4] == m2.created_at  # valid until the superseding statement
    assert not new[1]


def test_older_statement_written_later_is_retired_on_arrival(graph):
    m1, m2 = graph.mem(days=1), graph.mem(days=5)
    svc = graph.node("devbrain", "SERVICE", m1)
    laptop = graph.node("laptop", "HOST", m1)
    studio = graph.node("studio", "HOST", m2)
    graph.write(nodes=[svc, studio], edges=[edge(svc, "runs_on", studio, m2)])
    graph.write(nodes=[svc, laptop], edges=[edge(svc, "runs_on", laptop, m1)])
    assert graph.walk(graph.name("devbrain"), hops=1) == [("devbrain", "runs_on", "studio", 0)]


def test_multi_valued_predicate_keeps_both(graph):
    m1, m2 = graph.mem(days=1), graph.mem(days=5)
    folder = graph.node("folder", "PATH", m1)
    gh = graph.node("gh", "ACCOUNT", m1)
    claude = graph.node("claude", "ACCOUNT", m2)
    graph.write(nodes=[folder, gh], edges=[edge(folder, "uses_account", gh, m1)])
    graph.write(nodes=[folder, claude], edges=[edge(folder, "uses_account", claude, m2)])
    assert len(graph.walk(graph.name("folder"), hops=1)) == 2


def test_ended_fact_is_retired_and_restating_revives(graph):
    m1, m2, m3 = graph.mem(days=1), graph.mem(days=2), graph.mem(days=3)
    app = graph.node("app", "SERVICE", m1)
    db = graph.node("db", "SERVICE", m1)
    graph.write(nodes=[app, db], edges=[edge(app, "depends_on", db, m1)])
    stats = graph.write(edges=[edge(app, "depends_on", db, m2, ended=True)])
    assert stats.relations_ended == 1
    assert graph.walk(graph.name("app")) == []
    row = _relation(graph.conn, app, "depends_on", db, graph.project_id)
    assert row[2] == "ended"
    # An OLDER restatement does not revive it; a newer one does.
    graph.write(edges=[edge(app, "depends_on", db, m1)])
    assert graph.walk(graph.name("app")) == []
    stats = graph.write(edges=[edge(app, "depends_on", db, m3)])
    assert stats.relations_revived == 1
    assert graph.walk(graph.name("app")) == [("app", "depends_on", "db", 0)]


def test_ended_before_ever_seen_is_recorded_retired(graph):
    m = graph.mem()
    a = graph.node("a", "SERVICE", m)
    b = graph.node("b", "HOST", m)
    graph.write(nodes=[a, b], edges=[edge(a, "runs_on", b, m, ended=True)])
    row = _relation(graph.conn, a, "runs_on", b, graph.project_id)
    assert row is not None and row[2] == "ended"
    assert graph.walk(graph.name("a")) == []


def test_archived_entity_is_not_seeded_or_walked(graph):
    ops, vps, org, _ = chain(graph)
    later = graph.mem(days=9)
    graph.write(nodes=[graph.node("vps", "HOST", later, retired=True)])
    assert graph.walk(graph.name("vps")) == []
    # Walking from opsdesk stops at the archived host.
    assert graph.walk(graph.name("opsdesk")) == []
    # Named as current again later: back.
    graph.write(nodes=[graph.node("vps", "HOST", graph.mem(days=12))])
    assert graph.walk(graph.name("opsdesk"), hops=1) == [("opsdesk", "runs_on", "vps", 0)]


# ── Merge ────────────────────────────────────────────────────────────────


def test_rewrite_is_idempotent(graph):
    def counts():
        with graph.conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM devbrain.entities WHERE name_norm LIKE %s), "
                "       (SELECT count(*) FROM devbrain.entity_relations WHERE project_id = %s)",
                (f"%{graph.token}%", graph.project_id),
            )
            out = cur.fetchone()
        graph.conn.rollback()
        return out

    chain(graph)
    first = counts()
    m = graph.mem()
    ops = graph.node("opsdesk", "SERVICE", m, desc="devplane fork")
    vps = graph.node("vps", "HOST", m, desc="2.24.99.121")
    stats = graph.write(nodes=[ops, vps], edges=[edge(ops, "runs_on", vps, m)])
    assert counts() == first
    assert stats.entities_new == 0 and stats.relations_new == 0


def test_newest_description_wins(graph):
    m1, m2 = graph.mem(days=1), graph.mem(days=2)
    graph.write(nodes=[graph.node("box", "HOST", m2, desc="new ip")])
    graph.write(nodes=[graph.node("box", "HOST", m1, desc="old ip")])
    with graph.conn.cursor() as cur:
        cur.execute("SELECT description, cardinality(source_refs) FROM devbrain.entities "
                    "WHERE name_norm = %s", (graph.name("box"),))
        desc, refs = cur.fetchone()
    graph.conn.rollback()
    assert desc == "new ip" and refs == 2


# ── End to end: the pass with a fake model ───────────────────────────────


def _insert_memory(conn, project_id, text, created):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, title, content, created_at) "
            "VALUES (%s, 'decision', %s, %s, %s) RETURNING id",
            (project_id, "Deploy note", text, created),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def test_pass_end_to_end_with_quote_check_and_watermark(graph, monkeypatch):
    from cognify.orchestrator import run_pass

    host, site = graph.name("soho-vps"), graph.name("granlee-site")
    text = (f"The {site} container runs on the {host} behind Caddy. "
            f"Deploys go to {host} from GitHub Actions.")
    _insert_memory(graph.conn, graph.project_id, text, T0)

    def fake_llm(system, user):
        assert "[M1]" in user and site in user
        return LLMResult(backend="fake", model="fake", parsed={
            "nodes": [
                {"name": site, "type": "SERVICE", "description": "", "memory": 1, "status": "active"},
                {"name": host, "type": "HOST", "description": "SOHO VPS", "memory": 1, "status": "active"},
                {"name": "Invented Host", "type": "HOST", "description": "", "memory": 1, "status": "active"},
            ],
            "edges": [
                {"source": site, "predicate": "runs_on", "target": host, "memory": 1,
                 "quote": f"{site} container runs on the {host}", "ended": False},
                # Not a span of the memory: dropped.
                {"source": site, "predicate": "deploys_to", "target": host, "memory": 1,
                 "quote": "deployed every night by cron", "ended": False},
            ],
            "aliases": [],
        })

    monkeypatch.setattr(ent, "call_llm", fake_llm)
    monkeypatch.setattr(ent, "usable_backends", lambda: ["fake"])
    res = run_pass(graph.conn, "entities", graph.project_id,
                   pass_kwargs={"since": T0 - timedelta(days=1)})
    assert res.llm_calls == 1 and res.rows_processed == 1
    assert res.metadata["dropped"] == {"node_ungrounded": 1, "unquoted": 1}
    assert res.metadata["watermark"]["created_at"].startswith("2026-09-01")
    assert graph.walk(f"what is on {host}?") == [("granlee-site", "runs_on", "soho-vps", 0)]

    # Next run starts at the watermark: nothing new, no call.
    res2 = run_pass(graph.conn, "entities", graph.project_id)
    assert res2.llm_calls == 0 and res2.metadata["memories_seen"] == 0


def test_pass_stops_without_advancing_on_api_failure(graph, monkeypatch):
    from cognify.orchestrator import run_pass

    _insert_memory(graph.conn, graph.project_id, "x" * 80, T0)
    monkeypatch.setattr(ent, "call_llm",
                        lambda s, u: LLMResult(failure="api", detail="quota"))
    monkeypatch.setattr(ent, "usable_backends", lambda: ["fake"])
    since = T0 - timedelta(days=1)
    res = run_pass(graph.conn, "entities", graph.project_id, pass_kwargs={"since": since})
    assert res.rows_processed == 0 and res.metadata["stopped"] == "api"
    assert res.metadata["watermark"]["created_at"] == since.isoformat()


def test_dry_run_writes_nothing(graph, monkeypatch):
    host = graph.name("host")
    _insert_memory(graph.conn, graph.project_id, f"The api runs on {host} today, behind the usual reverse proxy setup.", T0)
    api = graph.name("api")
    printed: list[str] = []
    llm = lambda s, u: LLMResult(parsed={  # noqa: E731
        "nodes": [{"name": host, "type": "HOST", "description": "", "memory": 1, "status": "active"}],
        "edges": [], "aliases": [{"entity": host, "alias": api, "memory": 1}],
    })
    res = ent.run_entities(graph.conn, graph.project_id, dry_run=True,
                           since=T0 - timedelta(days=1), printer=printed.append, llm=llm)
    assert res.nodes == 1 and any(host in line for line in printed)
    with graph.conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM devbrain.entities WHERE name_norm LIKE %s",
                    (f"%{graph.token}%",))
        assert cur.fetchone()[0] == 0
    graph.conn.rollback()


# ── Latency ──────────────────────────────────────────────────────────────


def test_walk_latency_on_a_larger_graph(graph):
    """~1.5k entities / ~3k relations: the walk stays well inside the 20 ms
    budget (CI machines vary, so the assert is loose; the timing prints)."""
    import random

    rnd = random.Random(7)
    m = graph.mem()
    nodes = [graph.node(f"n{i}", rnd.choice(ent.ENTITY_TYPES), m) for i in range(1500)]
    edges = []
    seen = set()
    while len(edges) < 3000:
        a, b = rnd.sample(nodes, 2)
        p = rnd.choice(["depends_on", "part_of", "uses_account", "serves_domain"])
        if (a.id, p, b.id) in seen:
            continue
        seen.add((a.id, p, b.id))
        edges.append(edge(a, p, b, m))
    graph.write(nodes=nodes, edges=edges)
    with graph.conn.cursor() as cur:
        cur.execute("ANALYZE devbrain.entities")
        cur.execute("ANALYZE devbrain.entity_relations")
    graph.conn.commit()
    text = f"how does {graph.name('n1')} relate to {graph.name('n2')}?"
    graph.walk(text)  # warm
    best = min(_timed(graph, text) for _ in range(5))
    print(f"facts_walk: {best:.1f} ms")
    assert best < 200


def _timed(graph, text) -> float:
    t = time.perf_counter()
    graph.walk(text)
    return (time.perf_counter() - t) * 1000
