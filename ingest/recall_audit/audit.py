"""Recall audit for deep_search: recall@5, recall@10 and MRR over a gold set.

Each gold item (``gold_set.jsonl``) is a developer-style question plus the
devbrain.memory id(s) that answer it. ``expected_ids`` lists the primary
record first, followed by near-duplicate rows of the same fact (cognify
re-extracts the same atom many times); finding ANY of them counts as a hit,
so recall@k here is the single-relevant hit rate at k. The gold file holds
ids and our own query text only — never memory content (the repo is public).

What is measured, and why both ways
-----------------------------------
The SQL below mirrors ``deep_search`` in mcp-server/src/index.ts (default
call shape: no source_types filter): the devbrain.memory query (same JOINs,
``archived_at IS NULL``, optional project filter, cosine order), the legacy
``codebase`` chunks query, then a merge on score. That is the ``vector``
pipeline. The ``hybrid`` pipeline adds deep_search's keyword channel — the
exact SQL file the MCP runs (mcp-server/sql/keyword_search.sql) — and fuses
the two ranked lists with weighted Reciprocal Rank Fusion (``rrf_fuse``,
mirror of ``rrfFuse`` in mcp-server/src/hybrid.ts). ``--dedup on`` adds deep_search's near-duplicate
collapse (mcp-server/sql/near_duplicates.sql + ``collapse_near_duplicates``,
mirror of collapseNearDuplicates): a hit counts when the expected id is the
group's representative or one of its ``similar_ids``; the table also gives
rep-only metrics and the duplicate slots freed in the un-collapsed top 10.
Each configuration is run twice:

* ``index`` — through the HNSW index at ``hnsw.ef_search`` (default 400 +
  ``hnsw.iterative_scan=relaxed_order``, what the MCP pool sets per
  connection);
* ``exact`` — ``enable_indexscan = off`` inside the same read-only
  transaction, forcing a sequential scan, i.e. the true top-k.

``exact`` isolates ranking quality (embedding + query text); ``index`` minus
``exact`` is what the ANN index loses. Two scopes: ``project`` (deep_search
with ``project=<gold project>``, the usual call) and ``cross``
(``cross_project=true``).

Everything runs in READ ONLY transactions, one query at a time.

Usage (from ingest/)::

    python -m recall_audit                      # prefix off vs on, both scopes
    python -m recall_audit --prefix-modes on --scopes project --modes index
    python -m recall_audit --json /tmp/audit.json --per-item
    python -m recall_audit --modes index --ef-search 400          # ANN tuning
    python -m recall_audit --modes index --iterative-scan off
    python -m recall_audit --prefix-modes on --modes index --pipelines vector,hybrid
    python -m recall_audit --pipelines hybrid --rrf-k 30 --keyword-weight 0.5
    python -m recall_audit --pipelines hybrid --dedup off,on --dedup-thresholds 0.92,0.94,0.96

Connection: ``--dsn`` > ``DEVBRAIN_DATABASE_URL`` > config/devbrain.yaml;
Ollama: ``DEVBRAIN_OLLAMA_URL`` > yaml. libpq env vars (PGPASSWORD, ...) work
with a keyword DSN such as ``host=127.0.0.1 port=25433 user=devbrain dbname=devbrain``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_GOLD = Path(__file__).resolve().parent / "gold_set.jsonl"

# Mirrors the memory query in deep_search (mcp-server/src/index.ts). Keep in
# sync if that SQL changes. {project_filter} is "" or "AND m.project_id = %(pid)s".
MEMORY_SQL = """
SELECT
  m.id::text AS memory_id,
  1 - (m.embedding <=> %(vec)s::vector) AS score
FROM devbrain.memory m
JOIN devbrain.projects p ON m.project_id = p.id
LEFT JOIN devbrain.chunks c
  ON m.kind = 'chunk' AND c.id = m.provenance_id
  AND (c.source_type IS NULL OR c.source_type NOT IN
       ('decision','pattern','issue','session_summary'))
WHERE m.embedding IS NOT NULL
  AND m.archived_at IS NULL
  {project_filter}
ORDER BY m.embedding <=> %(vec)s::vector
LIMIT %(limit)s
"""

# Mirrors deep_search's legacy codebase fallback (devbrain.chunks).
CODEBASE_SQL = """
SELECT
  'codebase:' || c.id::text AS memory_id,
  1 - (c.embedding <=> %(vec)s::vector) AS score
FROM devbrain.chunks c
JOIN devbrain.projects p ON c.project_id = p.id
WHERE c.embedding IS NOT NULL
  AND c.source_type = 'codebase'
  {project_filter}
ORDER BY c.embedding <=> %(vec)s::vector
LIMIT %(limit)s
"""


# deep_search's keyword channel. Read from the MCP's own SQL file so the
# audit cannot drift from what ships.
KEYWORD_SQL_PATH = Path(__file__).resolve().parents[2] / "mcp-server" / "sql" / "keyword_search.sql"


def load_keyword_sql(path: Path = KEYWORD_SQL_PATH) -> str:
    """The keyword SQL with node-pg ``$N`` placeholders as psycopg2 ``%(pN)s``."""
    text = path.read_text()
    if "%" in text:
        raise ValueError(f"{path}: '%' would break psycopg2 parameter parsing")
    return re.sub(r"\$(\d+)", r"%(p\1)s", text)


# deep_search's near-duplicate pair query (same loader rules as above).
NEAR_DUP_SQL_PATH = KEYWORD_SQL_PATH.with_name("near_duplicates.sql")


def load_near_dup_sql(path: Path = NEAR_DUP_SQL_PATH) -> str:
    return load_keyword_sql(path)


@dataclass
class Group:
    """One collapsed result: the representative plus its near-duplicates."""

    rep: str
    similar: list[str] = field(default_factory=list)

    @property
    def members(self) -> list[str]:
        return [self.rep, *self.similar]


def collapse_near_duplicates(
    ranked: list[str],
    pairs: Iterable[tuple[str, str]],
    limit: int,
    created: dict[str, float] | None = None,
) -> list[Group]:
    """Greedy collapse in rank order. Mirror of collapseNearDuplicates in
    mcp-server/src/hybrid.ts.

    Walk the ranked pool; a key similar (>= threshold, i.e. listed in
    ``pairs``) to an already-kept representative joins the best-ranked such
    representative, otherwise it becomes a new representative while fewer
    than ``limit`` exist. The walk continues past ``limit`` so every copy in
    the pool is attached. No chaining: similarity is to the representative.

    With ``created`` (id -> created_at epoch), each group's representative is
    then swapped for its newest member; the group keeps its rank.
    """
    adj: dict[str, set[str]] = {}
    for a, b in pairs:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    groups: list[Group] = []
    seen: set[str] = set()
    for key in ranked:
        if key in seen:
            continue
        seen.add(key)
        near = adj.get(key, set())
        home = next((g for g in groups if g.rep in near), None)
        if home is not None:
            home.similar.append(key)
        elif len(groups) < limit:
            groups.append(Group(key))
    if created:
        for g in groups:
            newest = max(g.members, key=lambda k: (created.get(k, float("-inf")), k == g.rep))
            if newest != g.rep:
                g.similar = [g.rep, *(m for m in g.similar if m != newest)]
                g.rep = newest
    return groups


def first_hit_rank_groups(groups: list[Group], expected: Iterable[str], rep_only: bool = False) -> int | None:
    """1-based rank of the first group holding an expected id (any member,
    or only the representative when ``rep_only``)."""
    want = set(expected)
    for i, g in enumerate(groups, 1):
        if (g.rep in want) if rep_only else any(m in want for m in g.members):
            return i
    return None


def freed_slots(groups_of_top: list[Group]) -> int:
    """Duplicate entries absorbed when collapsing a top-k list."""
    return sum(len(g.similar) for g in groups_of_top)


@dataclass(frozen=True)
class HybridParams:
    """Defaults mirror SEARCH_DEFAULTS in mcp-server/src/hybrid.ts."""

    rrf_k: float = 60.0
    keyword_weight: float = 1.0
    candidates: int = 50
    keyword_max_df: float = 0.002
    keyword_row_budget: float = 3000.0
    keyword_max_terms: int = 12
    keyword_identifiers_only: bool = True


@dataclass(frozen=True)
class DedupParams:
    """Defaults mirror SEARCH_DEFAULTS.dedup* in mcp-server/src/hybrid.ts."""

    enabled: bool = True
    threshold: float = 0.93
    representative: str = "newest"  # newest | best


@dataclass
class GoldItem:
    id: str
    query: str
    project: str
    kind: str
    expected_ids: list[str]
    note: str | None = None
    # e.g. ["keyword"]: the question carries an exact identifier, file name
    # or error string from the answer (reported as its own subset).
    tags: list[str] = field(default_factory=list)


@dataclass
class Metrics:
    n: int
    recall_at_5: float
    recall_at_10: float
    mrr: float


@dataclass
class RunResult:
    prefix_mode: str
    scope: str
    mode: str
    pipeline: str = "vector"
    ranks: dict[str, int | None] = field(default_factory=dict)
    seconds: float = 0.0
    # per-query SQL wall time (ms) per channel: "vector", "keyword", "dedup"
    timings: dict[str, list[float]] = field(default_factory=dict)
    dedup: str = "off"
    # with dedup: rank of the first group whose representative is expected
    rep_ranks: dict[str, int | None] = field(default_factory=dict)
    # with dedup: duplicate entries in the un-collapsed top 10 (slots freed)
    freed: dict[str, int] = field(default_factory=dict)


def load_gold(path: Path) -> list[GoldItem]:
    items: list[GoldItem] = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not raw.get("expected_ids"):
            raise ValueError(f"{path}:{lineno}: expected_ids is empty")
        items.append(GoldItem(**raw))
    ids = [i.id for i in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate gold item ids")
    return items


def merge_candidates(
    memory_rows: Iterable[tuple[str, float]],
    codebase_rows: Iterable[tuple[str, float]],
    limit: int,
) -> list[str]:
    """deep_search's merge: concat, stable sort by score desc, slice."""
    cands = list(memory_rows) + list(codebase_rows)
    cands.sort(key=lambda r: r[1], reverse=True)
    return [mid for mid, _ in cands[:limit]]


def rrf_fuse(
    lists: Iterable[Iterable[str]],
    weights: Iterable[float],
    k: float,
    limit: int,
    tiebreak: dict[str, float] | None = None,
) -> list[str]:
    """Weighted Reciprocal Rank Fusion: score(d) = sum_l w_l / (k + rank_l(d)).

    Ranks are 1-based; a key repeated inside one list counts once (its best
    rank). Equal fused scores go to the higher ``tiebreak`` value (cosine),
    then first appearance. Mirror of rrfFuse in mcp-server/src/hybrid.ts.
    """
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    for ranked, w in zip(lists, weights):
        seen: set[str] = set()
        for rank, key in enumerate(ranked, 1):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + w / (k + rank)
            first_seen.setdefault(key, len(first_seen))
    tb = tiebreak or {}
    order = sorted(scores, key=lambda key: (-scores[key], -tb.get(key, 0.0), first_seen[key]))
    return order[:limit]


def percentile(values: Iterable[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100); 0.0 for no values."""
    vs = sorted(values)
    if not vs:
        return 0.0
    idx = max(0, min(len(vs) - 1, math.ceil(pct / 100 * len(vs)) - 1))
    return vs[idx]


def first_hit_rank(ranked_ids: list[str], expected: Iterable[str]) -> int | None:
    """1-based rank of the first expected id, or None if absent."""
    want = set(expected)
    for i, mid in enumerate(ranked_ids, 1):
        if mid in want:
            return i
    return None


def summarize(ranks: Iterable[int | None]) -> Metrics:
    rs = list(ranks)
    n = len(rs)
    if n == 0:
        return Metrics(0, 0.0, 0.0, 0.0)
    r5 = sum(1 for r in rs if r is not None and r <= 5) / n
    r10 = sum(1 for r in rs if r is not None and r <= 10) / n
    mrr = sum(1.0 / r for r in rs if r is not None) / n
    return Metrics(n, r5, r10, mrr)


def index_loss(exact: dict[str, int | None], index: dict[str, int | None], k: int = 10) -> list[str]:
    """Items the exact scan ranks in top-k but the index misses from top-k."""
    return sorted(
        gid
        for gid, er in exact.items()
        if er is not None and er <= k and (index.get(gid) is None or index[gid] > k)
    )


def _to_vec(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def run_search(
    cur: Any,
    vec: str,
    project_id: str | None,
    mode: str,
    depth: int,
    ef_search: int,
    include_codebase: bool,
    iterative_scan: str | None = None,
    *,
    pipeline: str = "vector",
    query_text: str = "",
    hybrid: HybridParams | None = None,
    keyword_sql: str | None = None,
    timings: dict[str, list[float]] | None = None,
    return_pool: bool = False,
) -> list[str]:
    """One deep_search-equivalent query in its own read-only transaction.

    ``pipeline="hybrid"`` widens the vector fetch to max(depth, candidates)
    (as deep_search does), runs the keyword SQL, and RRF-fuses both lists.
    ``return_pool`` returns the whole fused candidate pool instead of the
    top ``depth`` (the near-duplicate collapse walks the pool).
    """
    if mode == "exact":
        cur.execute("SET LOCAL enable_indexscan = off")
    else:
        cur.execute("SET LOCAL hnsw.ef_search = %s", (ef_search,))
        if iterative_scan:
            # pgvector >= 0.8: keep walking the graph until LIMIT rows pass
            # the WHERE filter (project scope) instead of filtering a fixed
            # ef_search-sized candidate list (deep_search sets relaxed_order).
            cur.execute("SELECT set_config('hnsw.iterative_scan', %s, true)", (iterative_scan,))
    hp = hybrid or HybridParams()
    fetch = max(depth, hp.candidates) if (pipeline == "hybrid" or return_pool) else depth
    params = {"vec": vec, "limit": fetch, "pid": project_id}
    mem_filter = "AND m.project_id = %(pid)s" if project_id else ""
    t0 = time.monotonic()
    cur.execute(MEMORY_SQL.format(project_filter=mem_filter), params)
    mem_rows = [(r[0], float(r[1])) for r in cur.fetchall()]
    cb_rows: list[tuple[str, float]] = []
    if include_codebase:
        cb_filter = "AND c.project_id = %(pid)s" if project_id else ""
        cur.execute(CODEBASE_SQL.format(project_filter=cb_filter), params)
        cb_rows = [(r[0], float(r[1])) for r in cur.fetchall()]
    if timings is not None:
        timings.setdefault("vector", []).append((time.monotonic() - t0) * 1000)
    vector_ranked = merge_candidates(mem_rows, cb_rows, fetch)
    if pipeline != "hybrid":
        cur.connection.rollback()  # end the read-only txn; nothing to keep
        return vector_ranked if return_pool else vector_ranked[:depth]

    t1 = time.monotonic()
    cur.execute(
        keyword_sql or load_keyword_sql(),
        {
            "p1": query_text,
            "p2": hp.keyword_max_df,
            "p3": hp.keyword_row_budget,
            "p4": hp.candidates,
            "p5": vec,
            "p6": project_id,
            "p7": None,
            "p8": None,
            "p9": hp.keyword_max_terms,
            "p10": hp.keyword_identifiers_only,
        },
    )
    kw_rows = [(str(r[0]), float(r[3])) for r in cur.fetchall()]
    if timings is not None:
        timings.setdefault("keyword", []).append((time.monotonic() - t1) * 1000)
    cur.connection.rollback()
    cosine = dict(mem_rows + cb_rows)
    for mid, sc in kw_rows:
        cosine.setdefault(mid, sc)
    pool = vector_ranked + [mid for mid, _ in kw_rows]
    return rrf_fuse(
        [vector_ranked, [mid for mid, _ in kw_rows]],
        [1.0, hp.keyword_weight],
        hp.rrf_k,
        len(pool) if return_pool else depth,
        cosine,
    )


def run_dedup(
    cur: Any,
    pool: list[str],
    threshold: float,
    depth: int,
    sql: str | None = None,
    timings: dict[str, list[float]] | None = None,
    representative: str = "best",
) -> tuple[list[Group], int]:
    """deep_search's collapse over a fused pool: (groups[:depth], freed slots in top 10)."""
    ids = [k for k in pool if not k.startswith("codebase:")]
    t0 = time.monotonic()
    cur.execute(sql or load_near_dup_sql(), {"p1": ids, "p2": threshold})
    rows = cur.fetchall()
    pairs = [(r[0], r[1]) for r in rows]
    created = {r[0]: r[3] for r in rows} | {r[1]: r[4] for r in rows}
    cur.connection.rollback()
    if timings is not None:
        timings.setdefault("dedup", []).append((time.monotonic() - t0) * 1000)
    groups = collapse_near_duplicates(pool, pairs, depth, created if representative == "newest" else None)
    top10 = collapse_near_duplicates(pool[:10], pairs, 10)
    return groups, freed_slots(top10)


def _fmt(m: Metrics) -> str:
    return f"{m.recall_at_5:.3f} | {m.recall_at_10:.3f} | {m.mrr:.3f}"


def _subsets(items: list[GoldItem]) -> list[tuple[str, list[GoldItem]]]:
    tags = sorted({t for i in items for t in i.tags})
    out = [("all", items)]
    for t in tags:
        out.append((t, [i for i in items if t in i.tags]))
        out.append((f"not {t}", [i for i in items if t not in i.tags]))
    return out


def render_table(results: list[RunResult], items: list[GoldItem]) -> str:
    lines = [
        f"Gold items: {len(items)}",
        "",
        "| query prefix | scope | search | pipeline | dedup | items | recall@5 | recall@10 | MRR |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        for name, subset in _subsets(items):
            m = summarize(r.ranks[i.id] for i in subset)
            lines.append(
                f"| {r.prefix_mode} | {r.scope} | {r.mode} | {r.pipeline} | {r.dedup} | {name} ({m.n}) | {_fmt(m)} |"
            )
    dd = [r for r in results if r.rep_ranks]
    if dd:
        lines += [
            "",
            "Near-duplicate collapse: rep-only = hit only when the representative itself is expected;",
            "freed = duplicate entries absorbed from the un-collapsed top 10 (mean per query, and queries with any).",
            "",
            "| scope | search | pipeline | dedup | rep-only R@5 | rep-only R@10 | rep-only MRR | freed / query | queries with freed slots |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in dd:
            m = summarize(r.rep_ranks[i.id] for i in items)
            fr = [r.freed[i.id] for i in items]
            lines.append(
                f"| {r.scope} | {r.mode} | {r.pipeline} | {r.dedup} | {_fmt(m).replace(' | ', ' | ')} | "
                f"{sum(fr) / len(fr):.2f} | {sum(1 for f in fr if f)} |"
            )
    lines += [
        "",
        "| scope | search | pipeline | dedup | channel | p50 ms | p95 ms | max ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        for ch, ts in sorted(r.timings.items()):
            lines.append(
                f"| {r.scope} | {r.mode} | {r.pipeline} | {r.dedup} | {ch} | {percentile(ts, 50):.1f} | "
                f"{percentile(ts, 95):.1f} | {max(ts):.1f} |"
            )
    by_key = {(r.prefix_mode, r.scope, r.mode, r.pipeline, r.dedup): r for r in results}
    loss_lines = []
    for (p, s, mode, pl, dd_), r in by_key.items():
        if not mode.startswith("index") or (p, s, "exact", pl, dd_) not in by_key:
            continue
        lost = index_loss(by_key[(p, s, "exact", pl, dd_)].ranks, r.ranks)
        loss_lines.append(f"- prefix {p}, scope {s}, {mode}, {pl}, dedup {dd_}: lost {len(lost)} exact top-10 hit(s) {lost}")
    if loss_lines:
        lines += ["", "Index loss (exact top-10 hits missing from index top-10):", *loss_lines]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--dsn", default=None, help="Postgres DSN (default: config DATABASE_URL)")
    ap.add_argument("--prefix-modes", default="off,on", help="comma list of off,on")
    ap.add_argument("--prefix", default=None, help="prefix used for 'on' (default: configured EMBED_QUERY_PREFIX, or 'query: ' if that is empty)")
    ap.add_argument("--scopes", default="project,cross", help="comma list of project,cross")
    ap.add_argument("--modes", default="index,exact", help="comma list of index,exact")
    ap.add_argument("--depth", type=int, default=50, help="results fetched per query (MRR cutoff)")
    ap.add_argument("--ef-search", type=int, default=400, help="index mode: hnsw.ef_search (deep_search: 400)")
    ap.add_argument(
        "--iterative-scan",
        choices=["relaxed_order", "strict_order", "off"],
        default="relaxed_order",
        help="index mode only: hnsw.iterative_scan (deep_search: relaxed_order)",
    )
    ap.add_argument("--pipelines", default="vector,hybrid", help="comma list of vector,hybrid")
    hd = HybridParams()
    ap.add_argument("--rrf-k", type=float, default=hd.rrf_k)
    ap.add_argument("--keyword-weight", type=float, default=hd.keyword_weight)
    ap.add_argument("--candidates", type=int, default=hd.candidates, help="per-channel candidate depth")
    ap.add_argument("--keyword-max-df", type=float, default=hd.keyword_max_df)
    ap.add_argument("--keyword-row-budget", type=float, default=hd.keyword_row_budget)
    ap.add_argument("--keyword-max-terms", type=int, default=hd.keyword_max_terms)
    ddp = DedupParams()
    ap.add_argument(
        "--dedup",
        default="off,on" if ddp.enabled else "off",
        help="comma list of off,on: near-duplicate collapse after ranking",
    )
    ap.add_argument(
        "--dedup-thresholds",
        default=str(ddp.threshold),
        help="comma list of cosine thresholds for dedup=on (e.g. 0.92,0.94,0.96)",
    )
    ap.add_argument("--dedup-representative", choices=["best", "newest"], default=ddp.representative)
    ap.add_argument(
        "--keyword-sql",
        type=Path,
        default=KEYWORD_SQL_PATH,
        help="experiments only: run a variant of the keyword SQL (default: the file deep_search runs)",
    )
    ap.add_argument(
        "--keyword-terms",
        choices=["identifiers", "all"],
        default="identifiers" if hd.keyword_identifiers_only else "all",
        help="which query tokens feed the keyword channel",
    )
    ap.add_argument("--no-codebase", action="store_true", help="skip the legacy codebase chunks query")
    ap.add_argument("--json", type=Path, default=None, help="write per-item ranks here")
    ap.add_argument("--per-item", action="store_true", help="print per-item ranks")
    args = ap.parse_args(argv)

    import psycopg2  # noqa: PLC0415 — keep metric helpers importable without a DB driver

    import config  # noqa: PLC0415
    from embeddings import embed_query  # noqa: PLC0415

    items = load_gold(args.gold)
    on_prefix = args.prefix if args.prefix is not None else (config.EMBED_QUERY_PREFIX or "query: ")
    prefixes = {"off": "", "on": on_prefix}
    prefix_modes = [p.strip() for p in args.prefix_modes.split(",") if p.strip()]
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    pipelines = [p.strip() for p in args.pipelines.split(",") if p.strip()]
    iterative = None if args.iterative_scan == "off" else args.iterative_scan
    hybrid = HybridParams(
        rrf_k=args.rrf_k,
        keyword_weight=args.keyword_weight,
        candidates=args.candidates,
        keyword_max_df=args.keyword_max_df,
        keyword_row_budget=args.keyword_row_budget,
        keyword_max_terms=args.keyword_max_terms,
        keyword_identifiers_only=args.keyword_terms == "identifiers",
    )
    keyword_sql = load_keyword_sql(args.keyword_sql) if "hybrid" in pipelines else None
    dedup_modes: list[tuple[str, float | None]] = []
    for d in (x.strip() for x in args.dedup.split(",") if x.strip()):
        if d == "off":
            dedup_modes.append(("off", None))
        else:
            for t in (float(x) for x in args.dedup_thresholds.split(",") if x.strip()):
                dedup_modes.append((f"{t:g}", t))
    near_dup_sql = load_near_dup_sql() if any(t is not None for _, t in dedup_modes) else None

    conn = psycopg2.connect(args.dsn or config.DATABASE_URL)
    conn.set_session(readonly=True, autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT slug, id::text FROM devbrain.projects")
    slug_to_id = dict(cur.fetchall())
    conn.rollback()
    missing = sorted({i.project for i in items} - slug_to_id.keys())
    if missing:
        print(f"unknown projects in gold set: {missing}", file=sys.stderr)
        return 2

    results: list[RunResult] = []
    for pm in prefix_modes:
        prefix = prefixes[pm]
        vecs = {i.id: _to_vec(embed_query(i.query, prefix=prefix)) for i in items}
        for scope in scopes:
            for mode in modes:
                label = mode
                if mode == "index" and iterative:
                    label = f"index+{iterative}"
                for pipeline in pipelines:
                    runs = [
                        RunResult(
                            prefix_mode=f"{pm} ({prefix!r})", scope=scope, mode=label, pipeline=pipeline, dedup=dname
                        )
                        for dname, _ in dedup_modes
                    ]
                    t0 = time.monotonic()
                    for it in items:
                        pid = slug_to_id[it.project] if scope == "project" else None
                        pool = run_search(
                            cur,
                            vecs[it.id],
                            pid,
                            mode,
                            args.depth,
                            args.ef_search,
                            not args.no_codebase,
                            iterative,
                            pipeline=pipeline,
                            query_text=it.query,
                            hybrid=hybrid,
                            keyword_sql=keyword_sql,
                            timings=runs[0].timings,
                            return_pool=True,
                        )
                        for rr, (_, thr) in zip(runs, dedup_modes):
                            if thr is None:
                                rr.ranks[it.id] = first_hit_rank(pool[: args.depth], it.expected_ids)
                                continue
                            groups, freed = run_dedup(
                                cur, pool, thr, args.depth, near_dup_sql, rr.timings, args.dedup_representative
                            )
                            rr.ranks[it.id] = first_hit_rank_groups(groups, it.expected_ids)
                            rr.rep_ranks[it.id] = first_hit_rank_groups(groups, it.expected_ids, rep_only=True)
                            rr.freed[it.id] = freed
                    secs = time.monotonic() - t0
                    for rr in runs:
                        rr.seconds = secs
                        results.append(rr)
                        m = summarize(rr.ranks.values())
                        print(
                            f"[{rr.prefix_mode} {scope} {mode} {pipeline} dedup={rr.dedup}] R@5={m.recall_at_5:.3f} "
                            f"R@10={m.recall_at_10:.3f} MRR={m.mrr:.3f} ({rr.seconds:.1f}s)",
                            file=sys.stderr,
                        )
    conn.close()

    print(render_table(results, items))
    if args.per_item:
        header = "item | " + " | ".join(f"{r.prefix_mode}/{r.scope}/{r.mode}/{r.pipeline}/{r.dedup}" for r in results)
        print("\n" + header)
        for it in items:
            cells = " | ".join(str(r.ranks[it.id] or f">{args.depth}") for r in results)
            print(f"{it.id} {it.project}/{it.kind} | {cells}")
    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "prefix": r.prefix_mode,
                        "scope": r.scope,
                        "mode": r.mode,
                        "pipeline": r.pipeline,
                        "dedup": r.dedup,
                        "rep_ranks": r.rep_ranks,
                        "freed": r.freed,
                        "seconds": r.seconds,
                        "ranks": r.ranks,
                        "timings_ms": r.timings,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
