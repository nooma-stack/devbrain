"""Postulate for compliance rule: student_identifiers_ssid_full_name_must_not_leave_the_emr_boundary_in_unredacted_form

POSTULATE
---------
A function returning student.ssid or student.full_name to a non-EMR caller
(detected by file path being outside emr/ boundary) without redaction is
detected as violating. A version using hash_ssid() or redact_name() is not
detected.

The rule (FERPA 99.31) requires student SSID/full_name to be hashed or
redacted when crossing the EMR boundary. The matcher uses three signals:

  1. file_path outside the emr/ subtree
  2. raw `student.ssid` or `student.full_name` reference present
  3. no `hash_ssid()` / `redact_name()` / `hash_student_id()` redactor

All three must be true to flag a violation. Phase 3.x will graduate this
to a project-graph boundary analysis.
"""
from __future__ import annotations

import re

_RAW_STUDENT_ID = re.compile(
    r'\bstudent\.(ssid|full_name)\b',
)
_REDACTOR_PRESENT = re.compile(
    r'(hash_ssid|redact_name|hash_student_id)\s*\(',
)


def _violates(source: str, file_path: str) -> bool:
    """v3.0 matcher: file outside emr/ AND raw student id reference AND no redactor."""
    if "emr/" in file_path:
        return False
    if not _RAW_STUDENT_ID.search(source):
        return False
    if _REDACTOR_PRESENT.search(source):
        return False
    return True


def test_google_workspace_returning_raw_ssid_violates():
    bad = '''
def push_to_calendar(student):
    event = {"description": f"Student: {student.ssid}"}
    return event
'''
    assert _violates(bad, file_path="integrations/google_workspace.py")


def test_google_workspace_with_hashed_ssid_does_not_violate():
    good = '''
def push_to_calendar(student):
    event = {"description": f"Student: {hash_ssid(student.ssid)}"}
    return event
'''
    assert not _violates(good, file_path="integrations/google_workspace.py")


def test_emr_internal_can_use_raw_ssid():
    inside_emr = '''
def render_iep(student):
    return f"SSID: {student.ssid}"
'''
    assert not _violates(inside_emr, file_path="emr/iep_renderer.py")
