"""Postulate for compliance rule: secrets_must_not_appear_in_error_messages_or_stack_traces

POSTULATE
---------
An exception body containing literal substrings like "password=", "token=",
"secret=", "api_key=", or "bearer" with values is detected as violating.
A version using "<redacted>" placeholders is not detected.

The rule (SOC2 CC6.1) forbids exception paths from emitting raw secret
values. The matcher pattern looks for `keyword=<value>` where keyword is
one of password/token/secret/api_key/bearer and value is a quoted literal
or interpolation. A literal `<redacted>` is anchor-matched as the safe
form. Phase 3.x will graduate this to AST + traceback frame analysis.
"""
from __future__ import annotations

import re

_SECRET_PATTERNS = re.compile(
    r'(password|token|secret|api_key|bearer)\s*=\s*([\'"\{][^\'"\<]+[\'"\}])',
    re.IGNORECASE,
)


def _violates(source: str) -> bool:
    return bool(_SECRET_PATTERNS.search(source))


def test_password_literal_in_error_violates():
    bad = 'raise ValueError(f"Auth failed: password={p}")'
    assert _violates(bad)


def test_redacted_password_does_not_violate():
    good = 'raise ValueError("Auth failed: password=<redacted>")'
    assert not _violates(good)


def test_token_literal_in_error_violates():
    bad = 'logger.error(f"Bearer token={t} expired")'
    assert _violates(bad)
