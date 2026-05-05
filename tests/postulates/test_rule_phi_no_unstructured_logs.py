"""Postulate for compliance rule: phi_columns_must_not_appear_in_unstructured_logs

POSTULATE
---------
A logger call that emits raw phi_* table column values is detected as
violating the rule. A redacted version (column names without values, or
hashed identifiers) is not detected.

The rule (HIPAA 164.312(a)(2)(iv)) forbids reads/writes against phi_*
tables from emitting raw column values to a logger. The matcher here is
a regex heuristic — Phase 3.x will graduate it to AST analysis in
eval_security. Until then, the heuristic must:

  - Detect the violation pattern (logger.info(f"... phi_x ... {var}"))
  - Not flag a redacted-id-only line (column name + hashed id, no values)
  - Not flag a non-PHI logger line (factory_jobs etc.)
"""
from __future__ import annotations

import re

# Inline matcher — Phase 3.x will graduate this to AST analysis in eval_security.
_LOGGER_PHI_RE = re.compile(
    r'(logger|log)\.(info|debug|warn|warning|error|critical)'
    r'.*phi_\w+.*\{[a-zA-Z_.]+\}',
    re.IGNORECASE,
)


def _violates(source: str) -> bool:
    return bool(_LOGGER_PHI_RE.search(source))


def test_logger_emitting_phi_table_row_violates():
    bad = 'logger.info(f"phi_audit_log row: {row}")'
    assert _violates(bad)


def test_logger_with_redacted_phi_does_not_violate():
    good = 'logger.info(f"phi_audit_log accessed (id=<redacted>)")'
    assert not _violates(good)


def test_non_phi_logger_does_not_violate():
    neutral = 'logger.info(f"factory_jobs status: {row}")'
    assert not _violates(neutral)
