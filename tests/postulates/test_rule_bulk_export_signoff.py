"""Postulate for compliance rule: bulk_exports_require_explicit_dpo_sign_off_claim_in_the_request

POSTULATE
---------
An endpoint with bulk_export semantics that doesn't validate request.dpo_signoff
is detected as violating. An endpoint with the check is not detected.
Single-record reads are not detected (no bulk semantics).

The rule (SOC2 CC6.7) requires endpoints returning >100 rows of regulated
data to validate a DPO sign-off claim. The matcher uses two signals:

  1. Bulk-export semantics (decorator/route hint matches a known marker)
  2. Absence of a DPO sign-off check in the handler body

If both are present, flag a violation. Phase 3.x will graduate this to
project-graph route analysis with payload-size estimation.
"""
from __future__ import annotations


def _is_bulk_export(handler_source: str) -> bool:
    """Detect bulk-export semantics via decorator/route hints."""
    bulk_markers = ("bulk_export", "@route('/export'", "@app.get('/export'")
    return any(m in handler_source for m in bulk_markers)


def _has_signoff_check(handler_source: str) -> bool:
    return "request.dpo_signoff" in handler_source or "validate_dpo_signoff" in handler_source


def _violates(handler_source: str) -> bool:
    if not _is_bulk_export(handler_source):
        return False
    return not _has_signoff_check(handler_source)


def test_bulk_export_without_dpo_signoff_violates():
    bad = '''
@bulk_export
def export_all_appointments(request):
    return appointments_repo.list_all()
'''
    assert _violates(bad)


def test_bulk_export_with_dpo_signoff_does_not_violate():
    good = '''
@bulk_export
def export_all_appointments(request):
    validate_dpo_signoff(request.dpo_signoff)
    return appointments_repo.list_all()
'''
    assert not _violates(good)


def test_single_record_read_does_not_violate():
    neutral = '''
def get_appointment(id):
    return appointments_repo.read(id)
'''
    assert not _violates(neutral)
