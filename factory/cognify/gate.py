"""Cheap gate in front of cognify extract.

Extract sends each candidate unit (a ``provenance_id`` group of memory rows:
a raw transcript's chunks, an end_session summary, a breadcrumb chain, ...)
to an LLM. Many units cannot yield anything new: a stored atom, a
machine-written closure summary of a transcript that is ingested anyway, a
subagent transcript, a 150-character breadcrumb, a session with no human
turn, or content the project's atoms already cover. This module decides,
without any LLM call, whether a unit should be extracted, extracted last
("downgrade": only if the pass still has LLM budget), or skipped.

Signals, cheapest first:
  * unit shape: atom-only units, closure-backfill / other listed end_session
    CLIs, raw transcripts under listed path parts (subagents, support runner)
  * size: total source characters
  * raw transcripts: human turns (user messages that are not tool results or
    harness-injected text) and the tool-output share of the text
  * novelty: for up to ``novelty_sample`` embedded source rows, the nearest
    live atom in the project that is older than the unit. A row is "novel"
    when that similarity is below ``novel_below``. A low novel share
    downgrades the unit (it skips only when ``skip_novel_share`` is set).

In "on" mode a skipped unit is not extracted on this run and, because the
pass watermark moves on, not on later runs either unless new source rows
arrive. ``devbrain cognify-reextract`` still processes it on request.

Modes (``cognify.extract_gate.mode`` / ``DEVBRAIN_EXTRACT_GATE``):
  off      — no gate (default)
  dry_run  — evaluate and log what would be skipped/downgraded; extract as usual
  on       — skip / reorder

Read-only: the gate never writes. Decisions are logged and summarized in the
extract pass's cognify_run_log metadata (ids and reasons, no memory text).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MODES = ("off", "dry_run", "on")
ATOM_KINDS = ("decision", "pattern", "issue")

# User-role messages that a person did not type: tool results (both
# adapters prefix them), harness-injected context, slash-command echoes.
_INJECTED_RE = (
    r"^\s*(\[tool_result\]|<command-|<local-command|<system-reminder>"
    r"|<task-notification>|<environment_context>|<user_instructions>"
    r"|<INSTRUCTIONS>|# AGENTS\.md instructions|Caveat: The messages below"
    r"|\[Request interrupted)"
)


@dataclass
class GateConfig:
    mode: str = "off"
    # Units whose source text is shorter than this are skipped.
    min_chars: int = 300
    # Raw transcripts need at least this many human turns.
    min_human_turns: int = 1
    # Raw transcripts whose text is mostly tool output are downgraded.
    max_tool_share: float = 0.85
    # Raw transcripts whose source_path contains one of these are skipped.
    skip_path_parts: list[str] = field(
        default_factory=lambda: ["/subagents/", "support-runner"]
    )
    # end_session summaries written by these CLIs (chunks.metadata.cli) are
    # skipped: closure backfill re-summarizes a transcript that is ingested.
    skip_clis: list[str] = field(default_factory=lambda: ["closure-backfill"])
    novelty: bool = True
    novelty_sample: int = 12
    # A source row is novel when its nearest older atom is below this.
    novel_below: float = 0.80
    # Novel share below this → downgrade. Novelty never skips by default:
    # on the production corpus, summaries whose every row was >= 0.80 to an
    # older atom still yielded atoms that were not near-duplicates (< 0.85)
    # of anything older. Set skip_novel_share (e.g. 0.0) to let it skip.
    downgrade_novel_share: float = 0.25
    skip_novel_share: float | None = None

    @property
    def enabled(self) -> bool:
        return self.mode in ("dry_run", "on")

    @property
    def applies(self) -> bool:
        return self.mode == "on"


def _mode(v: Any) -> str:
    if v is False or v is None:
        return "off"
    if v is True:
        return "on"
    s = str(v).strip().lower().replace("-", "_")
    return s if s in MODES else "off"


def load_config(cfg: dict | None = None) -> GateConfig:
    """yaml ``cognify.extract_gate.*``, env ``DEVBRAIN_EXTRACT_GATE`` for the mode."""
    if cfg is None:
        try:
            from config import load_config as _load  # factory/config.py

            cfg = _load()
        except Exception:  # noqa: BLE001 — config problems mean "off"
            cfg = {}
    g = ((cfg.get("cognify") or {}).get("extract_gate")) or {}
    out = GateConfig()
    out.mode = _mode(g.get("mode", "off"))
    for name in (
        "min_chars",
        "min_human_turns",
        "novelty_sample",
    ):
        if name in g:
            setattr(out, name, int(g[name]))
    for name in (
        "max_tool_share",
        "novel_below",
        "downgrade_novel_share",
    ):
        if name in g:
            setattr(out, name, float(g[name]))
    if "skip_novel_share" in g:
        v = g["skip_novel_share"]
        out.skip_novel_share = None if v is None else float(v)
    if "novelty" in g:
        out.novelty = bool(g["novelty"])
    if "skip_path_parts" in g:
        out.skip_path_parts = [str(x) for x in (g["skip_path_parts"] or [])]
    if "skip_clis" in g:
        out.skip_clis = [str(x) for x in (g["skip_clis"] or [])]
    if (v := os.environ.get("DEVBRAIN_EXTRACT_GATE")) is not None:
        out.mode = _mode(v)
    return out


_CACHED: GateConfig | None = None


def get_config() -> GateConfig:
    global _CACHED
    if _CACHED is None:
        _CACHED = load_config()
    return _CACHED


def reset_config(cfg: GateConfig | None = None) -> None:
    global _CACHED
    _CACHED = cfg


# ── Features ─────────────────────────────────────────────────────────────────


@dataclass
class UnitFeatures:
    unit_id: str
    shape: str = "empty"  # raw | summary | breadcrumbs | atom | mixed | empty
    source_rows: int = 0
    source_chars: int = 0
    kinds: list[str] = field(default_factory=list)
    cli: str | None = None
    source_path: str | None = None
    human_turns: int | None = None
    tool_share: float | None = None
    novel_share: float | None = None
    novelty_checked: int = 0
    first_created: Any = None


@dataclass
class GateDecision:
    action: str  # extract | downgrade | skip
    reasons: list[str]
    features: UnitFeatures


def _source_rows(conn: Any, unit_id: str, project_id: Any) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, length(content), created_at, embedding IS NOT NULL "
            "FROM devbrain.memory "
            "WHERE provenance_id = %s::uuid AND project_id = %s "
            "  AND archived_at IS NULL AND tier <> 'lesson' "
            "ORDER BY created_at",
            (unit_id, project_id),
        )
        return cur.fetchall()


def _end_session_cli(conn: Any, unit_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata->>'cli' FROM devbrain.chunks WHERE id = %s::uuid",
            (unit_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _raw_features(conn: Any, unit_id: str) -> tuple[bool, dict]:
    """(exists, features) for a raw_sessions row, computed in SQL so a
    multi-MB transcript never crosses the wire. Non-JSON raw_content (old
    rows) yields exists=True with empty features."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_path FROM devbrain.raw_sessions WHERE id = %s::uuid",
            (unit_id,),
        )
        row = cur.fetchone()
        if not row:
            return False, {}
        feats: dict = {"source_path": row[0]}
        cur.execute("SAVEPOINT gate_raw")
        try:
            cur.execute(
                "SELECT "
                "  count(*) FILTER (WHERE m->>'role' = 'user' "
                "       AND coalesce(m->>'content', '') !~ %s "
                "       AND length(btrim(coalesce(m->>'content', ''))) > 0), "
                "  coalesce(sum(length(m->>'content')), 0), "
                "  coalesce(sum(length(m->>'content')) "
                "       FILTER (WHERE m->>'content' LIKE '[tool_result]%%'), 0) "
                "FROM devbrain.raw_sessions r "
                "CROSS JOIN LATERAL jsonb_array_elements("
                "  CASE WHEN jsonb_typeof(r.raw_content::jsonb -> 'messages') = 'array' "
                "       THEN r.raw_content::jsonb -> 'messages' ELSE '[]'::jsonb END) m "
                "WHERE r.id = %s::uuid",
                (_INJECTED_RE, unit_id),
            )
            human, total, tool = cur.fetchone()
            cur.execute("RELEASE SAVEPOINT gate_raw")
            feats["human_turns"] = int(human or 0)
            feats["tool_share"] = (float(tool) / float(total)) if total else None
        except Exception as exc:  # noqa: BLE001 — non-JSON transcript
            cur.execute("ROLLBACK TO SAVEPOINT gate_raw")
            logger.debug("extract_gate: raw features unavailable (%s)", exc)
        return True, feats


def _novelty(
    conn: Any, unit_id: str, project_id: Any, cfg: GateConfig, before: Any
) -> tuple[float | None, int]:
    """Share of sampled source rows whose nearest older live atom is below
    ``novel_below``. None when the unit has no embedded rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embedding::text FROM ("
            "  SELECT embedding, row_number() OVER (ORDER BY created_at) rn, "
            "         count(*) OVER () n "
            "  FROM devbrain.memory "
            "  WHERE provenance_id = %s::uuid AND project_id = %s "
            "    AND archived_at IS NULL AND tier <> 'lesson' "
            "    AND embedding IS NOT NULL) s "
            # evenly spaced sample across the unit
            "WHERE n <= %s OR (rn - 1) %% GREATEST(1, n / %s) = 0 " "LIMIT %s",
            (
                unit_id,
                project_id,
                cfg.novelty_sample,
                cfg.novelty_sample,
                cfg.novelty_sample,
            ),
        )
        embs = [r[0] for r in cur.fetchall()]
        if not embs:
            return None, 0
        cur.execute("SAVEPOINT gate_hnsw")
        try:
            cur.execute("SELECT set_config('hnsw.ef_search', '400', true)")
            cur.execute(
                "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"
            )
            cur.execute("RELEASE SAVEPOINT gate_hnsw")
        except Exception:  # noqa: BLE001 — older pgvector
            cur.execute("ROLLBACK TO SAVEPOINT gate_hnsw")
        novel = 0
        for emb in embs:
            cur.execute(
                "SELECT 1 - (embedding <=> %s::vector) FROM devbrain.memory "
                "WHERE project_id = %s AND archived_at IS NULL "
                "  AND embedding IS NOT NULL AND kind = ANY(%s) "
                "  AND created_at < %s "
                "ORDER BY embedding <=> %s::vector LIMIT 1",
                (emb, project_id, list(ATOM_KINDS), before, emb),
            )
            row = cur.fetchone()
            if row is None or row[0] is None or float(row[0]) < cfg.novel_below:
                novel += 1
    return novel / len(embs), len(embs)


def features(
    conn: Any,
    unit_id: str,
    project_id: Any,
    cfg: GateConfig,
    *,
    with_novelty: bool = True,
) -> UnitFeatures:
    f = UnitFeatures(unit_id=str(unit_id))
    rows = _source_rows(conn, unit_id, project_id)
    f.source_rows = len(rows)
    if not rows:
        return f
    f.first_created = rows[0][2]
    f.kinds = sorted({r[0] for r in rows})
    f.source_chars = int(sum(r[1] or 0 for r in rows))
    kinds = set(f.kinds)
    is_raw, raw = _raw_features(conn, unit_id)
    if is_raw:
        f.shape = "raw"
        f.source_path = raw.get("source_path")
        f.human_turns = raw.get("human_turns")
        f.tool_share = raw.get("tool_share")
    elif kinds <= set(ATOM_KINDS):
        f.shape = "atom"
    elif kinds == {"session_summary"}:
        f.shape = "summary"
        f.cli = _end_session_cli(conn, unit_id)
    elif kinds == {"session_breadcrumb"}:
        f.shape = "breadcrumbs"
    else:
        f.shape = "mixed"
    if with_novelty and cfg.novelty and f.shape != "atom":
        f.novel_share, f.novelty_checked = _novelty(
            conn, unit_id, project_id, cfg, f.first_created
        )
    return f


def decide(f: UnitFeatures, cfg: GateConfig) -> GateDecision:
    """Pure decision from features (unit-tested without a DB)."""
    skip: list[str] = []
    down: list[str] = []
    if f.source_rows == 0:
        skip.append("no_source")
    if f.shape == "atom":
        skip.append("atom_source")
    if f.cli and f.cli in cfg.skip_clis:
        skip.append(f"cli:{f.cli}")
    if f.source_path and any(p in f.source_path for p in cfg.skip_path_parts):
        skip.append("path")
    if f.source_rows and f.source_chars < cfg.min_chars:
        skip.append("too_short")
    if (
        f.shape == "raw"
        and f.human_turns is not None
        and f.human_turns < cfg.min_human_turns
    ):
        skip.append("no_human_turns")
    if (
        f.shape == "raw"
        and f.tool_share is not None
        and f.tool_share > cfg.max_tool_share
    ):
        down.append("tool_heavy")
    if f.novel_share is not None:
        if cfg.skip_novel_share is not None and f.novel_share <= cfg.skip_novel_share:
            skip.append("not_novel")
        elif f.novel_share < cfg.downgrade_novel_share:
            down.append("low_novelty")
    if skip:
        return GateDecision("skip", skip + down, f)
    if down:
        return GateDecision("downgrade", down, f)
    return GateDecision("extract", [], f)


def evaluate(conn: Any, unit_id: str, project_id: Any, cfg: GateConfig) -> GateDecision:
    """Features + decision. Never raises: an evaluation error means extract.

    Runs inside a savepoint and never commits or rolls back the caller's
    transaction (the gate is read-only)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT gate_eval")
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_gate: no savepoint (%s); extracting", exc)
        return GateDecision(
            "extract", ["gate_error"], UnitFeatures(unit_id=str(unit_id))
        )
    try:
        f = features(conn, unit_id, project_id, cfg, with_novelty=False)
        d = decide(f, cfg)
        if d.action != "skip" and cfg.novelty and f.shape != "atom":
            # Novelty costs index lookups; only pay it for units that
            # survive the free checks.
            f.novel_share, f.novelty_checked = _novelty(
                conn, unit_id, project_id, cfg, f.first_created
            )
            d = decide(f, cfg)
        with conn.cursor() as cur:
            cur.execute("RELEASE SAVEPOINT gate_eval")
        return d
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "extract_gate: evaluation failed for %s (%s); extracting", unit_id, exc
        )
        try:
            with conn.cursor() as cur:
                cur.execute("ROLLBACK TO SAVEPOINT gate_eval")
        except Exception as exc2:  # noqa: BLE001
            logger.debug("extract_gate: savepoint rollback failed (%s)", exc2)
        return GateDecision(
            "extract", ["gate_error"], UnitFeatures(unit_id=str(unit_id))
        )


class GateLog:
    """Collects decisions for the run-log metadata (ids + reasons only)."""

    MAX_IDS = 50

    def __init__(self, mode: str):
        self.mode = mode
        self.counts: dict[str, int] = {"extract": 0, "downgrade": 0, "skip": 0}
        self.reasons: dict[str, int] = {}
        self.ids: dict[str, list[str]] = {"downgrade": [], "skip": []}

    def add(self, d: GateDecision) -> None:
        self.counts[d.action] = self.counts.get(d.action, 0) + 1
        for r in d.reasons:
            self.reasons[r] = self.reasons.get(r, 0) + 1
        if d.action in self.ids and len(self.ids[d.action]) < self.MAX_IDS:
            self.ids[d.action].append(d.features.unit_id)
        if d.action != "extract":
            verb = "would " + d.action if self.mode == "dry_run" else d.action
            logger.info(
                "extract_gate: %s %s (%s)",
                verb,
                d.features.unit_id,
                ",".join(d.reasons),
            )

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "counts": self.counts,
            "reasons": self.reasons,
            "ids": self.ids,
        }


def recent_units(conn: Any, project_id: Any, limit: int) -> list[str]:
    """The ``limit`` most recent extract units in a project (by the first
    source row), newest first. For ``devbrain cognify-gate`` reports."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provenance_id::text FROM devbrain.memory "
            "WHERE project_id = %s AND provenance_id IS NOT NULL "
            "  AND archived_at IS NULL AND tier <> 'lesson' "
            "GROUP BY provenance_id ORDER BY min(created_at) DESC LIMIT %s",
            (project_id, int(limit)),
        )
        return [r[0] for r in cur.fetchall()]


def atom_counts(conn: Any, unit_ids: list[str]) -> dict[str, int]:
    """Atoms each unit has produced so far (archived included)."""
    if not unit_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT provenance_id::text, count(*) FROM devbrain.memory "
            "WHERE provenance_id = ANY(%s::uuid[]) AND tier = 'lesson' "
            "GROUP BY 1",
            (unit_ids,),
        )
        return {r[0]: int(r[1]) for r in cur.fetchall()}
