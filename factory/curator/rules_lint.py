"""Lint compliance-profile-tagged rules.

Contract enforced
-----------------
Every devbrain.memory row with tier='rule' AND non-empty
compliance_profiles MUST ship with a postulate test in
tests/postulates/ that mentions either:
  - the rule's UUID (string form), or
  - a slugified version of its title.

Rationale: profile-tagged rules are auto-applied by the curator brief
to projects that enable matching profiles (P7). Auto-application
without a verification gate lets a buggy or stale rule silently
contaminate downstream agents. The postulate is the gate.

The discovery here is a heuristic — it doesn't prove the postulate
verifies the right thing, only that *some* postulate file references
the rule. Phase 3.x can replace it with a structured rule->postulate
mapping (e.g., a rule_postulates table or a metadata field on the
rule). Until then, "the postulate file mentions the rule" is the
contract.

Public API
----------
- find_unverified_rules(conn) -> list[UnverifiedRule]
- run_lint(conn) -> int  # exit code, 0 if clean, 1 if violations

CLI entry: `python -m curator.rules_lint`
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID


# Path to postulates dir — resolved relative to this module's repo location.
# factory/curator/rules_lint.py -> ../../tests/postulates
_POSTULATES_DIR = Path(__file__).parent.parent.parent / "tests" / "postulates"


@dataclass(frozen=True)
class UnverifiedRule:
    """A profile-tagged rule with no matching postulate test."""

    id: UUID
    title: str
    compliance_profiles: list[str]


def find_unverified_rules(conn: Any) -> list[UnverifiedRule]:
    """Return profile-tagged rules with no matching postulate test."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, compliance_profiles "
            "FROM devbrain.memory "
            "WHERE tier = 'rule' "
            "  AND compliance_profiles IS NOT NULL "
            "  AND array_length(compliance_profiles, 1) > 0 "
            "  AND archived_at IS NULL"
        )
        rows = cur.fetchall()

    if not rows:
        return []

    postulate_corpus = _read_postulates_corpus()
    unverified: list[UnverifiedRule] = []
    for rule_id, title, profiles in rows:
        if _has_matching_postulate(rule_id, title, postulate_corpus):
            continue
        unverified.append(
            UnverifiedRule(
                id=rule_id,
                title=title,
                compliance_profiles=list(profiles),
            )
        )
    return unverified


def run_lint(conn: Any) -> int:
    """CLI entry point. Exit 0 if clean, 1 if violations found."""
    unverified = find_unverified_rules(conn)
    if not unverified:
        print("[rules lint] All profile-tagged rules have postulate tests. OK")
        return 0

    print(
        f"[rules lint] Found {len(unverified)} profile-tagged rules without "
        f"matching postulate tests:",
        file=sys.stderr,
    )
    for r in unverified:
        print(
            f"  - {r.id}  profiles={r.compliance_profiles}  "
            f"title={r.title!r}",
            file=sys.stderr,
        )
    print(
        "\nEvery rule with non-empty compliance_profiles MUST ship with a "
        "postulate test in tests/postulates/ that references either the "
        "rule's UUID or a slugified version of its title.",
        file=sys.stderr,
    )
    return 1


def _read_postulates_corpus() -> str:
    """Concatenate all postulate test source for heuristic matching."""
    if not _POSTULATES_DIR.exists():
        return ""
    chunks: list[str] = []
    for path in _POSTULATES_DIR.rglob("test_*.py"):
        try:
            chunks.append(path.read_text())
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(chunks)


def _has_matching_postulate(rule_id: UUID, title: str, corpus: str) -> bool:
    """Check whether the corpus references the rule by UUID or title slug."""
    if str(rule_id) in corpus:
        return True
    slug = _slugify(title)
    if not slug:
        return False
    return slug in corpus


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    """Lowercase + collapse non-alphanumerics to underscore.

    Used to derive a human-readable token from a rule title for
    postulate-corpus search. Title `"PHI must not appear in unstructured
    logs"` -> `phi_must_not_appear_in_unstructured_logs`.
    """
    return _SLUG_RE.sub("_", text.lower()).strip("_")


def main() -> None:
    """Module entry point: ``python -m curator.rules_lint``.

    Lazy-imports state_machine so that the lint check can run before the
    full factory CLI parser loads. Uses FactoryDB._conn() (the project's
    canonical psycopg2-connection helper) — the same pattern Phase 5c's
    `curator queue-stuck` uses.
    """
    from config import DATABASE_URL
    from state_machine import FactoryDB

    db = FactoryDB(DATABASE_URL)
    with db._conn() as conn:
        sys.exit(run_lint(conn))


if __name__ == "__main__":
    main()
