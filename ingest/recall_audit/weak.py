"""Calibrate deep_search's weak-match flag.

deep_search marks a response ``weak_match`` when its best result scores
below a threshold, so agents can tell "DevBrain probably doesn't have this"
from a real answer. This module picks the thresholds.

Positives are the gold queries (``gold_set.jsonl``): the answer is in the
DB. Negatives (``negatives.jsonl``) ask about projects, systems or facts
that are not in the DB. Each query runs through the deployed pipeline —
hybrid retrieval, local reranker (top 30, rrf), near-duplicate collapse
(top 10 groups) — in both scopes (project = the item's project, and
cross-project), and the audit records the response's best score:

* ``rerank``: highest reranker score among the returned groups' members
  (what deep_search reports when the reranker answered);
* ``cosine``: highest cosine similarity among them (the fallback when the
  reranker is down or off).

A response is flagged when its best score is below the threshold. The flag
predicts "not in the DB": precision = flagged negatives / flagged, recall =
flagged negatives / negatives. ``false flags`` counts positives flagged,
split by whether the answer was actually in the returned top 10.

Read-only; needs the reranker service. Usage (from ingest/)::

    python -m recall_audit.weak
    python -m recall_audit.weak --show-negatives   # local check that negatives are really absent
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recall_audit.audit import (
    DEFAULT_GOLD,
    DedupParams,
    HybridParams,
    RerankParams,
    _to_vec,
    first_hit_rank_groups,
    load_gold,
    load_keyword_sql,
    load_near_dup_sql,
    run_dedup,
    run_rerank,
    run_search,
)

DEFAULT_NEGATIVES = Path(__file__).resolve().parent / "negatives.jsonl"

# Shipped thresholds (mirror SEARCH_DEFAULTS.weakMatch* in mcp-server/src/hybrid.ts).
WEAK_RERANK_THRESHOLD = 0.88
WEAK_COSINE_THRESHOLD = 0.41

COSINE_SQL = """
SELECT id::text, 1 - (embedding <=> %(vec)s::vector)
FROM devbrain.memory
WHERE id = ANY(%(ids)s::uuid[]) AND embedding IS NOT NULL
"""


@dataclass
class Observation:
    qid: str
    scope: str
    positive: bool
    rerank: float | None
    cosine: float | None
    hit_rank: int | None  # positives only: rank of the first group holding the answer


@dataclass
class Negative:
    id: str
    query: str
    project: str


def load_negatives(path: Path) -> list[Negative]:
    out = [Negative(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    ids = [n.id for n in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate ids")
    return out


def flag_stats(obs: list[Observation], basis: str, threshold: float) -> dict[str, Any]:
    """Precision / recall of "score < threshold" as a predictor of a negative."""

    def score(o: Observation) -> float | None:
        return o.rerank if basis == "rerank" else o.cosine

    usable = [o for o in obs if score(o) is not None]
    flagged = [o for o in usable if score(o) < threshold]  # type: ignore[operator]
    negs = [o for o in usable if not o.positive]
    tp = sum(1 for o in flagged if not o.positive)
    fp_found = sum(1 for o in flagged if o.positive and o.hit_rank is not None and o.hit_rank <= 10)
    fp_missed = sum(1 for o in flagged if o.positive and (o.hit_rank is None or o.hit_rank > 10))
    precision = tp / len(flagged) if flagged else 1.0
    recall = tp / len(negs) if negs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "threshold": threshold,
        "flagged": len(flagged),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_flags_found": fp_found,
        "false_flags_missed": fp_missed,
    }


def pick_threshold(obs: list[Observation], basis: str, grid: list[float], min_precision: float) -> dict[str, Any]:
    """Highest recall with precision >= min_precision; ties go to the lower threshold."""
    best: dict[str, Any] | None = None
    for t in grid:
        st = flag_stats(obs, basis, t)
        if st["precision"] >= min_precision and (best is None or st["recall"] > best["recall"]):
            best = st
    return best or flag_stats(obs, basis, grid[0])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--negatives", type=Path, default=DEFAULT_NEGATIVES)
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--scopes", default="project,cross")
    ap.add_argument("--rerank-url", default=RerankParams().url)
    ap.add_argument("--min-precision", type=float, default=0.9)
    ap.add_argument("--show-negatives", action="store_true", help="print each negative's top titles (local check only)")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    import psycopg2  # noqa: PLC0415

    import config  # noqa: PLC0415
    from embeddings import embed_query  # noqa: PLC0415

    gold = load_gold(args.gold)
    negs = load_negatives(args.negatives)
    hp, dp, rp = HybridParams(), DedupParams(), RerankParams(url=args.rerank_url)
    ksql, dsql = load_keyword_sql(), load_near_dup_sql()

    conn = psycopg2.connect(args.dsn or config.DATABASE_URL)
    conn.set_session(readonly=True, autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT slug, id::text FROM devbrain.projects")
    slug_to_id = dict(cur.fetchall())
    conn.rollback()

    queries = [(g.id, g.query, g.project, True, g.expected_ids) for g in gold] + [
        (n.id, n.query, n.project, False, []) for n in negs
    ]
    obs: list[Observation] = []
    for scope in [s.strip() for s in args.scopes.split(",") if s.strip()]:
        for qid, text, project, positive, expected in queries:
            vec = _to_vec(embed_query(text))
            pid = slug_to_id.get(project) if scope == "project" else None
            pool = run_search(
                cur, vec, pid, "index", 10, 400, True, "relaxed_order",
                pipeline="hybrid", query_text=text, hybrid=hp, keyword_sql=ksql, return_pool=True,
            )
            scores: dict[str, float] = {}
            pool = run_rerank(cur, text, pool, rp, None, scores)
            groups, _ = run_dedup(cur, pool, dp.threshold, 10, dsql, None, dp.representative)
            members = [m for g in groups for m in g.members if not m.startswith("codebase:")]
            cur.execute(COSINE_SQL, {"vec": vec, "ids": members})
            cos = {r[0]: float(r[1]) for r in cur.fetchall()}
            conn.rollback()
            rr = [scores[m] for m in members if m in scores]
            cc = [cos[m] for m in members if m in cos]
            obs.append(
                Observation(
                    qid=qid,
                    scope=scope,
                    positive=positive,
                    rerank=max(rr) if rr else None,
                    cosine=max(cc) if cc else None,
                    hit_rank=first_hit_rank_groups(groups, expected) if positive else None,
                )
            )
            if args.show_negatives and not positive:
                cur.execute(
                    "SELECT left(coalesce(title, content), 110) FROM devbrain.memory WHERE id = ANY(%s::uuid[])",
                    ([g.rep for g in groups[:3]],),
                )
                titles = " | ".join(r[0].replace("\n", " ") for r in cur.fetchall())
                conn.rollback()
                print(f"{scope} {qid} rerank={obs[-1].rerank:.3f} cos={obs[-1].cosine:.3f} :: {titles}", file=sys.stderr)
        print(f"[{scope}] done", file=sys.stderr)
    conn.close()

    lines = [
        f"Positives (gold): {len(gold)}; negatives: {len(negs)}; scopes: {args.scopes}; observations: {len(obs)}",
        "",
        "Best-score distribution (median / p10 / p90):",
    ]

    def q(vals: list[float], p: float) -> float:
        v = sorted(vals)
        return v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))] if v else float("nan")

    for basis in ("rerank", "cosine"):
        for label, sel in (("positives", True), ("negatives", False)):
            vals = [getattr(o, basis) for o in obs if o.positive == sel and getattr(o, basis) is not None]
            lines.append(f"- {basis} {label}: {q(vals, 0.5):.3f} / {q(vals, 0.1):.3f} / {q(vals, 0.9):.3f}")
    grids = {
        "rerank": sorted({*(round(x * 0.01, 2) for x in range(1, 100)), *(round(0.85 + x * 0.005, 3) for x in range(21))}),
        "cosine": [round(0.40 + x * 0.005, 3) for x in range(0, 81)],
    }
    for basis, grid in grids.items():
        lines += [
            "",
            f"{basis}: flag when best {basis} score < threshold",
            "",
            "| threshold | flagged | precision | recall | F1 | false flags (answer in top 10) | false flags (answer missed) |",
            "|---|---|---|---|---|---|---|",
        ]
        shown = sorted({*grid[:: max(1, len(grid) // 12)], pick_threshold(obs, basis, grid, args.min_precision)["threshold"]})
        best_f1 = max((flag_stats(obs, basis, t) for t in grid), key=lambda s: s["f1"])
        shown = sorted({*shown, best_f1["threshold"]})
        for t in shown:
            st = flag_stats(obs, basis, t)
            lines.append(
                f"| {t:g} | {st['flagged']} | {st['precision']:.3f} | {st['recall']:.3f} | {st['f1']:.3f} | "
                f"{st['false_flags_found']} | {st['false_flags_missed']} |"
            )
        pick = pick_threshold(obs, basis, grid, args.min_precision)
        lines.append(
            f"\nPick ({basis}, precision >= {args.min_precision}): threshold {pick['threshold']:g} "
            f"-> precision {pick['precision']:.3f}, recall {pick['recall']:.3f}; best F1 at {best_f1['threshold']:g} "
            f"(P {best_f1['precision']:.3f}, R {best_f1['recall']:.3f})"
        )
        shipped = WEAK_RERANK_THRESHOLD if basis == "rerank" else WEAK_COSINE_THRESHOLD
        st = flag_stats(obs, basis, shipped)
        lines.append(
            f"Shipped ({basis} < {shipped:g}): precision {st['precision']:.3f}, recall {st['recall']:.3f}, "
            f"false flags {st['false_flags_found']} (answer in top 10) + {st['false_flags_missed']} (answer missed)"
        )
        for scope in [s.strip() for s in args.scopes.split(",") if s.strip()]:
            st = flag_stats([o for o in obs if o.scope == scope], basis, shipped)
            lines.append(f"  - {scope}: precision {st['precision']:.3f}, recall {st['recall']:.3f}, flagged {st['flagged']}")
    print("\n".join(lines))
    if args.json:
        args.json.write_text(json.dumps([o.__dict__ for o in obs], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
