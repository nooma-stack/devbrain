"""P_recency — deep_search recency-neighbor expansion + supersedes auto-walk.

POSTULATE
---------
The deep_search response, when given a set of primary memory_ids on
an active topic, must surface:

1. The K most-recent memory rows whose embedding similarity to each
   primary exceeds a floor AND that are at least N days newer
   ("recency neighbors").

2. The latest memory row reachable from a primary via a chain of
   `supersedes` edges in devbrain.memory_dependencies ("auto-walk").

These two signals close the gap that triggered the feature: a 04-30
morning Vertex-quota issue chunk surfaced as current state when the
same evening's resolution had already been recorded — but the writer
forgot to mark the supersedes edge, AND the resolution chunk ranked
lower on the original query than the issue chunk.

This postulate exercises the same SQL the TS deep_search runs, against
real Postgres + pgvector, so a regression in either query path is
caught here even if the MCP wrapper is fine on its own.

Source under test
-----------------
mcp-server/src/recency.ts
    expandRecencyNeighbors() / findSupersedingMemories()
"""
from __future__ import annotations


def _embed(values: dict[int, float]) -> str:
    """Construct a synthetic 1024-dim vector as the pgvector text format.

    `values` is a sparse map of dim → value; all unset dims default to 0.
    For our tests, we set dim 0 high for "topic A" and dim 100 high for
    "topic B" so cosine similarity has a known shape:

        - two "topic A" vectors with similar magnitudes → similarity ≈ 1.0
        - "topic A" vector vs "topic B" vector → similarity ≈ 0.0
    """
    arr = [0.0] * 1024
    for k, v in values.items():
        arr[k] = v
    return "[" + ",".join(f"{x}" for x in arr) + "]"


def _insert_memory_with_embedding(
    conn,
    *,
    project_id: str,
    kind: str = "decision",
    content: str = "",
    embedding: str,
    created_at: str | None = None,
) -> str:
    """Insert a devbrain.memory row with a vector embedding + optional
    backdated created_at. Returns the new id."""
    with conn.cursor() as cur:
        if created_at is None:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, title, content, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector) RETURNING id",
                (project_id, kind, "p_recency_title", content, embedding),
            )
        else:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, title, content, embedding, created_at) "
                "VALUES (%s, %s, %s, %s, %s::vector, %s) RETURNING id",
                (
                    project_id,
                    kind,
                    "p_recency_title",
                    content,
                    embedding,
                    created_at,
                ),
            )
        row = cur.fetchone()
    conn.commit()
    return str(row[0])


# ─── Recency neighbors ──────────────────────────────────────────────────────


_RECENCY_SQL = """
WITH primaries AS (
    SELECT id, created_at, embedding, project_id
      FROM devbrain.memory
     WHERE id = ANY(%(ids)s::uuid[])
       AND archived_at IS NULL
       AND embedding IS NOT NULL
)
SELECT
    p.id::text AS primary_id,
    n.id::text AS neighbor_id,
    1 - (n.embedding <=> p.embedding) AS similarity,
    EXTRACT(EPOCH FROM (n.created_at - p.created_at)) / 86400.0
        AS age_delta_days
  FROM primaries p
  CROSS JOIN LATERAL (
        SELECT m.id, m.created_at, m.embedding
          FROM devbrain.memory m
         WHERE m.id <> p.id
           AND m.archived_at IS NULL
           AND m.embedding IS NOT NULL
           AND m.project_id = p.project_id
           AND m.created_at > p.created_at + (%(min_age_days)s || ' days')::interval
         ORDER BY m.embedding <=> p.embedding
         LIMIT %(overfetch)s
       ) n
 WHERE 1 - (n.embedding <=> p.embedding) >= %(sim_floor)s
 ORDER BY p.id, n.created_at DESC
"""


def test_recency_neighbor_surfaces_newer_similar_chunk(conn, project_factory):
    project = project_factory("recency")
    topic_a = _embed({0: 1.0})
    topic_a_strong = _embed({0: 1.0, 1: 0.1})
    topic_b = _embed({100: 1.0})

    primary_id = _insert_memory_with_embedding(
        conn,
        project_id=project["id"],
        content="topic A v1 — 2026-04-30 morning issue",
        embedding=topic_a,
        created_at="2026-04-30T09:00:00+00",
    )
    # Neighbor that should surface: same topic, newer by ≥ 3 days.
    resolution_id = _insert_memory_with_embedding(
        conn,
        project_id=project["id"],
        content="topic A v2 — 2026-05-08 resolution",
        embedding=topic_a_strong,
        created_at="2026-05-08T10:00:00+00",
    )
    # Should NOT surface: too recent — only 1 day newer.
    _insert_memory_with_embedding(
        conn,
        project_id=project["id"],
        content="topic A near-dup — 2026-05-01 same chunking pass",
        embedding=topic_a,
        created_at="2026-05-01T09:00:00+00",
    )
    # Should NOT surface: unrelated topic.
    _insert_memory_with_embedding(
        conn,
        project_id=project["id"],
        content="topic B — completely different subject",
        embedding=topic_b,
        created_at="2026-05-08T10:00:00+00",
    )

    with conn.cursor() as cur:
        cur.execute(
            _RECENCY_SQL,
            {
                "ids": [primary_id],
                "min_age_days": "3",
                "overfetch": 20,
                "sim_floor": 0.4,
            },
        )
        rows = cur.fetchall()

    assert len(rows) == 1, f"expected 1 neighbor, got {len(rows)}: {rows}"
    pid, nid, sim, age_days = rows[0]
    assert pid == primary_id
    assert nid == resolution_id
    assert sim >= 0.4
    assert age_days >= 3.0


def test_recency_neighbor_respects_project_scope(conn, project_factory):
    a = project_factory("recency_a")
    b = project_factory("recency_b")
    topic = _embed({0: 1.0})

    primary_id = _insert_memory_with_embedding(
        conn,
        project_id=a["id"],
        content="topic A in project a",
        embedding=topic,
        created_at="2026-01-01T00:00:00+00",
    )
    # Same topic, newer, but in a DIFFERENT project — must not surface.
    _insert_memory_with_embedding(
        conn,
        project_id=b["id"],
        content="topic A in project b — newer but cross-project",
        embedding=topic,
        created_at="2026-05-08T00:00:00+00",
    )

    with conn.cursor() as cur:
        cur.execute(
            _RECENCY_SQL,
            {
                "ids": [primary_id],
                "min_age_days": "3",
                "overfetch": 20,
                "sim_floor": 0.4,
            },
        )
        rows = cur.fetchall()

    assert rows == []


def test_recency_neighbor_skips_archived(conn, project_factory):
    project = project_factory("recency_archived")
    topic = _embed({0: 1.0})

    primary_id = _insert_memory_with_embedding(
        conn,
        project_id=project["id"],
        content="topic A — primary",
        embedding=topic,
        created_at="2026-01-01T00:00:00+00",
    )
    archived_id = _insert_memory_with_embedding(
        conn,
        project_id=project["id"],
        content="topic A — archived newer",
        embedding=topic,
        created_at="2026-05-08T00:00:00+00",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = now() WHERE id = %s",
            (archived_id,),
        )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            _RECENCY_SQL,
            {
                "ids": [primary_id],
                "min_age_days": "3",
                "overfetch": 20,
                "sim_floor": 0.4,
            },
        )
        rows = cur.fetchall()

    assert rows == [], "archived chunk must not surface as recency neighbor"


# ─── Supersedes auto-walk ───────────────────────────────────────────────────


_SUPERSEDES_SQL = """
WITH RECURSIVE chain AS (
    SELECT
        md.to_memory_id   AS primary_id,
        md.from_memory_id AS replacement_id,
        1 AS depth
      FROM devbrain.memory_dependencies md
      JOIN devbrain.memory m ON m.id = md.from_memory_id
     WHERE md.to_memory_id = ANY(%(ids)s::uuid[])
       AND md.edge_type = 'supersedes'
       AND m.archived_at IS NULL
    UNION
    SELECT
        chain.primary_id,
        md.from_memory_id,
        chain.depth + 1
      FROM chain
      JOIN devbrain.memory_dependencies md
        ON md.to_memory_id = chain.replacement_id
       AND md.edge_type = 'supersedes'
      JOIN devbrain.memory m ON m.id = md.from_memory_id
     WHERE chain.depth < 10
       AND m.archived_at IS NULL
)
SELECT DISTINCT ON (primary_id)
    primary_id::text, replacement_id::text, depth
  FROM chain
 ORDER BY primary_id, depth DESC
"""


def _insert_supersedes_edge(conn, *, new_id: str, old_id: str) -> None:
    """Edge schema: from_memory_id 'supersedes' to_memory_id —
    `from` is the newer replacement, `to` is the older row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'supersedes', 'postulate-test')",
            (new_id, old_id),
        )
    conn.commit()


def test_supersedes_walk_returns_direct_replacement(conn, project_factory):
    project = project_factory("supers_direct")
    topic = _embed({0: 1.0})

    old_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="old", embedding=topic,
    )
    new_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="new", embedding=topic,
    )
    _insert_supersedes_edge(conn, new_id=new_id, old_id=old_id)

    with conn.cursor() as cur:
        cur.execute(_SUPERSEDES_SQL, {"ids": [old_id]})
        rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] == old_id
    assert rows[0][1] == new_id
    assert rows[0][2] == 1


def test_supersedes_walk_returns_deepest_in_chain(conn, project_factory):
    project = project_factory("supers_chain")
    topic = _embed({0: 1.0})

    a_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="A oldest", embedding=topic,
    )
    b_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="B middle", embedding=topic,
    )
    c_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="C latest", embedding=topic,
    )
    _insert_supersedes_edge(conn, new_id=b_id, old_id=a_id)
    _insert_supersedes_edge(conn, new_id=c_id, old_id=b_id)

    with conn.cursor() as cur:
        cur.execute(_SUPERSEDES_SQL, {"ids": [a_id]})
        rows = cur.fetchall()

    assert len(rows) == 1
    primary_id, replacement_id, depth = rows[0]
    assert primary_id == a_id
    assert replacement_id == c_id
    assert depth == 2


def test_supersedes_walk_skips_archived_replacement(conn, project_factory):
    project = project_factory("supers_archived")
    topic = _embed({0: 1.0})

    old_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="old", embedding=topic,
    )
    new_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="archived new", embedding=topic,
    )
    _insert_supersedes_edge(conn, new_id=new_id, old_id=old_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = now() WHERE id = %s",
            (new_id,),
        )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(_SUPERSEDES_SQL, {"ids": [old_id]})
        rows = cur.fetchall()

    assert rows == [], "archived superseder must not be returned as the walk endpoint"


def test_supersedes_walk_returns_nothing_for_primary_with_no_edge(
    conn, project_factory,
):
    project = project_factory("supers_none")
    topic = _embed({0: 1.0})
    only_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="standalone", embedding=topic,
    )

    with conn.cursor() as cur:
        cur.execute(_SUPERSEDES_SQL, {"ids": [only_id]})
        rows = cur.fetchall()

    assert rows == []


# ─── Earliest-on-topic ──────────────────────────────────────────────────────


_EARLIEST_SQL = """
WITH primaries AS (
    SELECT id, created_at, embedding, project_id
      FROM devbrain.memory
     WHERE id = ANY(%(ids)s::uuid[])
       AND archived_at IS NULL
       AND embedding IS NOT NULL
)
SELECT
    p.id::text AS primary_id,
    n.id::text AS neighbor_id,
    1 - (n.embedding <=> p.embedding) AS similarity,
    EXTRACT(EPOCH FROM (p.created_at - n.created_at)) / 86400.0 AS age_delta_days
  FROM primaries p
  CROSS JOIN LATERAL (
        SELECT m.id, m.created_at, m.embedding
          FROM devbrain.memory m
         WHERE m.id <> p.id
           AND m.archived_at IS NULL
           AND m.embedding IS NOT NULL
           AND m.project_id = p.project_id
           AND m.created_at < p.created_at - (%(min_age_days)s || ' days')::interval
         ORDER BY m.embedding <=> p.embedding
         LIMIT %(overfetch)s
       ) n
 WHERE 1 - (n.embedding <=> p.embedding) >= %(sim_floor)s
 ORDER BY p.id, n.created_at ASC
"""


def test_earliest_on_topic_returns_oldest_similar(conn, project_factory):
    project = project_factory("earliest_basic")
    topic = _embed({0: 1.0})

    oldest_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="A oldest framing",
        embedding=topic, created_at="2026-01-01T00:00:00+00",
    )
    _insert_memory_with_embedding(
        conn, project_id=project["id"], content="A middle revision",
        embedding=topic, created_at="2026-03-01T00:00:00+00",
    )
    primary_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="A current state",
        embedding=topic, created_at="2026-05-08T00:00:00+00",
    )

    with conn.cursor() as cur:
        cur.execute(
            _EARLIEST_SQL,
            {
                "ids": [primary_id],
                "min_age_days": "0",
                "overfetch": 50,
                "sim_floor": 0.5,
            },
        )
        rows = cur.fetchall()

    # Two candidates qualify; the SQL returns BOTH ordered earliest-
    # first. The TS layer takes only the first via Map insertion order
    # — assert that ordering here.
    assert len(rows) == 2
    assert rows[0][1] == oldest_id


def test_earliest_on_topic_skips_below_similarity_floor(
    conn, project_factory,
):
    project = project_factory("earliest_sim")
    topic_a = _embed({0: 1.0})
    topic_b = _embed({100: 1.0})

    primary_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="A current",
        embedding=topic_a, created_at="2026-05-08T00:00:00+00",
    )
    # Older but on a different topic — should NOT qualify.
    _insert_memory_with_embedding(
        conn, project_id=project["id"], content="B unrelated old",
        embedding=topic_b, created_at="2026-01-01T00:00:00+00",
    )

    with conn.cursor() as cur:
        cur.execute(
            _EARLIEST_SQL,
            {
                "ids": [primary_id],
                "min_age_days": "0",
                "overfetch": 50,
                "sim_floor": 0.5,
            },
        )
        rows = cur.fetchall()

    assert rows == []


def test_earliest_on_topic_respects_project_scope(conn, project_factory):
    a = project_factory("earliest_a")
    b = project_factory("earliest_b")
    topic = _embed({0: 1.0})

    primary_id = _insert_memory_with_embedding(
        conn, project_id=a["id"], content="A in project a",
        embedding=topic, created_at="2026-05-08T00:00:00+00",
    )
    # Same topic, older, but in a DIFFERENT project — must not surface.
    _insert_memory_with_embedding(
        conn, project_id=b["id"], content="A in project b",
        embedding=topic, created_at="2026-01-01T00:00:00+00",
    )

    with conn.cursor() as cur:
        cur.execute(
            _EARLIEST_SQL,
            {
                "ids": [primary_id],
                "min_age_days": "0",
                "overfetch": 50,
                "sim_floor": 0.5,
            },
        )
        rows = cur.fetchall()

    assert rows == []


# ─── primary_age_days ───────────────────────────────────────────────────────


def test_primary_age_days_is_nonnegative(conn, project_factory):
    project = project_factory("age_days")
    topic = _embed({0: 1.0})
    fresh_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="just now",
        embedding=topic,
    )
    backdated_id = _insert_memory_with_embedding(
        conn, project_id=project["id"], content="40 days ago",
        embedding=topic,
        created_at="2026-03-29T00:00:00+00",
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, "
            "EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0 AS age_days "
            "FROM devbrain.memory WHERE id = ANY(%s::uuid[])",
            ([fresh_id, backdated_id],),
        )
        rows = {row[0]: float(row[1]) for row in cur.fetchall()}

    assert rows[fresh_id] >= 0
    assert rows[fresh_id] < 1.0  # inserted moments ago
    # Backdated row: created_at is 2026-03-29 — at any test runtime
    # after 2026-04-29, this should be > 30 days. Be tolerant of clock
    # skew in CI.
    assert rows[backdated_id] >= 30.0
