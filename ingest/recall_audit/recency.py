"""Audit deep_search's recency-neighbor strategies: exact scan vs bounded HNSW.

deep_search annotates each result with up to k newer memories on the same
topic (``recency_neighbors``, mcp-server/src/recency.ts) and, on request,
the earliest one (``earliest_on_topic``). Both run the shared SQL in
mcp-server/sql/. Left to the planner, that SQL is answered by an exact
scan per result (seconds on a large project). The ``hnsw`` strategy runs it
under ``SET LOCAL enable_sort = off`` and a bounded ``hnsw.max_scan_tuples``
so the HNSW index answers it.

The primaries are deep_search's actual top results (hybrid + near-duplicate
collapse, the deployed defaults) for each gold query. For each strategy the
audit reports latency, how often the neighbor set is identical to the exact
one, coverage (results that get any neighbor), agreement of the
neighbor-driven recency_warning trigger (similarity >= 0.55 and >= 7 days
newer), and, for neighbors that differ, whether they are still valid:

* similarity to the primary of the neighbors HNSW picked instead, vs the
  exact ones they replaced;
* their exact similarity rank among all qualifying newer rows (rank <= the
  overfetch means the exact scan considered them too; they lost only on
  "most recent");
* how much older the newest HNSW neighbor is than the newest exact one.

Read-only. Usage (from ingest/)::

    python -m recall_audit.recency --max-scan-tuples 1000,2000,5000
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recall_audit.audit import (
    DEFAULT_GOLD,
    KEYWORD_SQL_PATH,
    DedupParams,
    HybridParams,
    _to_vec,
    load_gold,
    load_keyword_sql,
    load_near_dup_sql,
    percentile,
    run_dedup,
    run_search,
)

SQL_DIR = KEYWORD_SQL_PATH.parent
NEIGHBORS_SQL_PATH = SQL_DIR / "recency_neighbors.sql"
EARLIEST_SQL_PATH = SQL_DIR / "earliest_on_topic.sql"

# Same constants deep_search passes (index.ts / recency.ts).
K = 2
MIN_AGE_DAYS = 3
FLOOR = 0.4
NEIGHBOR_OVERFETCH = 20  # recency.ts: max(20, k * 5)
EARLIEST_MIN_AGE_DAYS = 0  # DEFAULT_EARLIEST_OPTIONS in recency.ts
EARLIEST_FLOOR = 0.5
EARLIEST_OVERFETCH = 50

# Exact similarity rank of one row among a primary's qualifying newer rows.
RANK_SQL = """
SELECT count(*) + 1
FROM devbrain.memory m, devbrain.memory p, devbrain.memory h
WHERE p.id = %(pid)s AND h.id = %(hid)s
  AND m.archived_at IS NULL AND m.embedding IS NOT NULL
  AND m.project_id = p.project_id AND m.id <> p.id
  AND m.created_at > p.created_at + make_interval(days => %(days)s)
  AND (m.embedding <=> p.embedding) < (h.embedding <=> p.embedding)
"""


@dataclass
class Neighbor:
    id: str
    similarity: float
    age_delta_days: float


@dataclass
class StrategyRun:
    name: str
    ms: list[float] = field(default_factory=list)
    neighbors: dict[tuple[str, str], list[Neighbor]] = field(default_factory=dict)
    earliest: dict[tuple[str, str], str | None] = field(default_factory=dict)


def first_k(rows: list[tuple], k: int) -> dict[str, list[Neighbor]]:
    """recency.ts: rows arrive ordered by (primary, created_at DESC); keep k per primary."""
    out: dict[str, list[Neighbor]] = {}
    for r in rows:
        lst = out.setdefault(r[0], [])
        if len(lst) < k:
            lst.append(Neighbor(r[1], float(r[5]), float(r[6])))
    return out


def first_one(rows: list[tuple]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in rows:
        out.setdefault(r[0], r[1])
    return out


def warning_b(ns: list[Neighbor]) -> bool:
    return any(n.similarity >= 0.55 and n.age_delta_days >= 7 for n in ns)


def apply_strategy(cur: Any, name: str) -> None:
    if name == "exact":
        cur.execute("SET LOCAL enable_indexscan = off")
        return
    tuples = int(name.split(":")[1])
    cur.execute("SET LOCAL hnsw.ef_search = 400")
    cur.execute("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)")
    cur.execute("SET LOCAL enable_sort = off")
    cur.execute("SELECT set_config('hnsw.max_scan_tuples', %s, true)", (str(tuples),))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--scopes", default="project,cross")
    ap.add_argument(
        "--max-scan-tuples",
        default="2000",
        help="comma list; one hnsw strategy per value",
    )
    ap.add_argument(
        "--no-earliest",
        action="store_true",
        help="skip the earliest_on_topic comparison",
    )
    ap.add_argument(
        "--no-ranks",
        action="store_true",
        help="skip exact ranks of changed neighbors (slow)",
    )
    args = ap.parse_args(argv)

    import psycopg2  # noqa: PLC0415

    import config  # noqa: PLC0415
    from embeddings import embed_query  # noqa: PLC0415

    items = load_gold(args.gold)
    nsql = load_keyword_sql(NEIGHBORS_SQL_PATH)  # same $N -> %(pN)s rules
    esql = load_keyword_sql(EARLIEST_SQL_PATH)
    ksql = load_keyword_sql()
    dsql = load_near_dup_sql()
    hp, dp = HybridParams(), DedupParams()
    strategies = ["exact"] + [
        f"hnsw:{int(t)}" for t in args.max_scan_tuples.split(",") if t.strip()
    ]

    conn = psycopg2.connect(args.dsn or config.DATABASE_URL)
    conn.set_session(readonly=True, autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT slug, id::text FROM devbrain.projects")
    slug_to_id = dict(cur.fetchall())
    conn.rollback()

    runs = {s: StrategyRun(s) for s in strategies}
    primaries_seen = 0
    for scope in [s.strip() for s in args.scopes.split(",") if s.strip()]:
        for it in items:
            vec = _to_vec(embed_query(it.query))
            pid = slug_to_id[it.project] if scope == "project" else None
            pool = run_search(
                cur,
                vec,
                pid,
                "index",
                10,
                400,
                True,
                "relaxed_order",
                pipeline="hybrid",
                query_text=it.query,
                hybrid=hp,
                keyword_sql=ksql,
                return_pool=True,
            )
            groups, _ = run_dedup(
                cur, pool, dp.threshold, 10, dsql, None, dp.representative
            )
            prim = [g.rep for g in groups if not g.rep.startswith("codebase:")]
            primaries_seen += len(prim)
            for s in strategies:
                apply_strategy(cur, s)
                t0 = time.monotonic()
                cur.execute(
                    nsql,
                    {
                        "p1": prim,
                        "p2": str(MIN_AGE_DAYS),
                        "p3": NEIGHBOR_OVERFETCH,
                        "p4": FLOOR,
                    },
                )
                rows = cur.fetchall()
                runs[s].ms.append((time.monotonic() - t0) * 1000)
                conn.rollback()
                got = first_k(rows, K)
                for p in prim:
                    runs[s].neighbors[(scope + ":" + it.id, p)] = got.get(p, [])
                if not args.no_earliest:
                    apply_strategy(cur, s)
                    cur.execute(
                        esql,
                        {
                            "p1": prim,
                            "p2": str(EARLIEST_MIN_AGE_DAYS),
                            "p3": EARLIEST_OVERFETCH,
                            "p4": EARLIEST_FLOOR,
                        },
                    )
                    e = first_one(cur.fetchall())
                    conn.rollback()
                    for p in prim:
                        runs[s].earliest[(scope + ":" + it.id, p)] = e.get(p)
        print(f"[{scope}] done", file=sys.stderr)

    exact = runs["exact"]
    lines = [
        f"Gold queries: {len(items)}; primaries (deep_search top results, all scopes): {primaries_seen}",
        "",
        "| strategy | p50 ms | p95 ms | max ms | identical neighbor sets | primaries with neighbors | warning (b) agrees | earliest agrees |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in strategies:
        r = runs[s]
        keys = list(exact.neighbors)
        same = sum(
            1
            for k in keys
            if {n.id for n in r.neighbors[k]} == {n.id for n in exact.neighbors[k]}
        )
        cov = sum(1 for k in keys if r.neighbors[k])
        warn = sum(
            1
            for k in keys
            if warning_b(r.neighbors[k]) == warning_b(exact.neighbors[k])
        )
        ea = (
            f"{sum(1 for k in keys if r.earliest.get(k) == exact.earliest.get(k))}/{len(keys)}"
            if not args.no_earliest
            else "-"
        )
        lines.append(
            f"| {s} | {percentile(r.ms, 50):.0f} | {percentile(r.ms, 95):.0f} | {max(r.ms):.0f} | "
            f"{same}/{len(keys)} | {cov} (exact {sum(1 for k in keys if exact.neighbors[k])}) | {warn}/{len(keys)} | {ea} |"
        )

    for s in strategies[1:]:
        r = runs[s]
        new_sims, old_sims, ranks, newest_gap = [], [], [], []
        changed = 0
        for k, ex in exact.neighbors.items():
            hn = r.neighbors[k]
            ex_ids, hn_ids = {n.id for n in ex}, {n.id for n in hn}
            if ex_ids == hn_ids:
                continue
            changed += 1
            new_sims += [n.similarity for n in hn if n.id not in ex_ids]
            old_sims += [n.similarity for n in ex if n.id not in hn_ids]
            if ex and hn:
                newest_gap.append(
                    max(n.age_delta_days for n in hn)
                    - max(n.age_delta_days for n in ex)
                )
            if not args.no_ranks:
                for n in hn:
                    if n.id in ex_ids:
                        continue
                    cur.execute("SET LOCAL enable_indexscan = off")
                    cur.execute(
                        RANK_SQL, {"pid": k[1], "hid": n.id, "days": MIN_AGE_DAYS}
                    )
                    ranks.append(int(cur.fetchone()[0]))
                    conn.rollback()
        lines += ["", f"Changed neighbor sets under {s}: {changed}"]
        if new_sims or old_sims:
            lines.append(
                f"- similarity to primary: HNSW-only picks median {statistics.median(new_sims) if new_sims else float('nan'):.3f} "
                f"(min {min(new_sims) if new_sims else float('nan'):.3f}, n={len(new_sims)}); "
                f"exact-only picks median {statistics.median(old_sims) if old_sims else float('nan'):.3f} (n={len(old_sims)})"
            )
        if ranks:
            within = sum(1 for x in ranks if x <= NEIGHBOR_OVERFETCH)
            within2 = sum(1 for x in ranks if x <= 2 * NEIGHBOR_OVERFETCH)
            lines.append(
                f"- exact similarity rank of HNSW-only picks among qualifying newer rows: median {statistics.median(ranks):.0f}, "
                f"<= {NEIGHBOR_OVERFETCH}: {within}/{len(ranks)}, <= {2 * NEIGHBOR_OVERFETCH}: {within2}/{len(ranks)}, max {max(ranks)}"
            )
        if newest_gap:
            lines.append(
                f"- newest HNSW neighbor minus newest exact neighbor (days newer than primary): "
                f"median {statistics.median(newest_gap):+.1f}, p10 {sorted(newest_gap)[len(newest_gap) // 10]:+.1f}"
            )
    conn.close()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
