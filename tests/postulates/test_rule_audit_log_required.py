"""Postulate for compliance rule: audit_log_writes_required_for_every_phi_read_write_at_service_method_boundary

POSTULATE
---------
A service method that calls phi_*_repo.read(id) without a corresponding
audit_log_writer.record(...) call is detected as violating the rule.
A method that does both is not detected.

The rule (HIPAA 164.312(b)) requires every service method that reads or
writes phi_* tables to emit an audit_log row. The matcher checks for the
presence of a phi-repo call AND the absence of an audit_log_writer call
in the same method body. Phase 3.x will graduate this to AST function-
scope analysis.
"""
from __future__ import annotations


def _violates(method_source: str) -> bool:
    """v3.0 matcher: presence of phi repo call AND absence of audit_log call."""
    has_phi_call = "phi_" in method_source and "_repo.read" in method_source
    has_audit_log = "audit_log_writer.record" in method_source
    return has_phi_call and not has_audit_log


def test_phi_read_without_audit_log_violates():
    bad = '''
def read_phi_audit_entry(id):
    return phi_audit_log_repo.read(id)
'''
    assert _violates(bad)


def test_phi_read_with_audit_log_does_not_violate():
    good = '''
def read_phi_audit_entry(id):
    audit_log_writer.record(actor='system', action='read', resource='phi_audit_log')
    return phi_audit_log_repo.read(id)
'''
    assert not _violates(good)


def test_non_phi_method_does_not_violate():
    neutral = '''
def read_factory_job(id):
    return factory_jobs_repo.read(id)
'''
    assert not _violates(neutral)
