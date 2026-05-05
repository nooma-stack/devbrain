"""Test migration 023 seeds the 5 compliance rules with the expected profile tags.

Migration 023 inserts five tier='rule' rows into devbrain.memory, each with
exactly one compliance profile:

  - 2x hipaa: PHI logs, audit log writes
  - 1x ferpa: student identifier redaction
  - 2x soc2:  secrets in errors, bulk export sign-off

These tests assert each row exists with the expected compliance_profiles
array. Phase 7c per-rule postulates (tests/postulates/test_rule_*.py) cover
the enforcement contract; this file only proves the seed migration ran and
preserved the profile tags.

Tests are gated on @pytest.mark.db (skip without DEVBRAIN_DB_PASSWORD).
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_023_seeds_phi_no_unstructured_logs_rule(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles FROM devbrain.memory "
            "WHERE tier = 'rule' AND title = 'PHI columns must not appear in unstructured logs'"
        )
        row = cur.fetchone()
    assert row is not None, "PHI-no-unstructured-logs rule missing"
    assert row[0] == ['hipaa']


@pytest.mark.db
def test_023_seeds_audit_log_required_rule(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles FROM devbrain.memory "
            "WHERE tier = 'rule' AND title = "
            "'Audit log writes required for every PHI read/write at service-method boundary'"
        )
        row = cur.fetchone()
    assert row is not None, "audit-log-required rule missing"
    assert row[0] == ['hipaa']


@pytest.mark.db
def test_023_seeds_secrets_in_errors_rule(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles FROM devbrain.memory "
            "WHERE tier = 'rule' AND title = 'Secrets must not appear in error messages or stack traces'"
        )
        row = cur.fetchone()
    assert row is not None, "secrets-in-errors rule missing"
    assert row[0] == ['soc2']


@pytest.mark.db
def test_023_seeds_ferpa_student_id_rule(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles FROM devbrain.memory "
            "WHERE tier = 'rule' AND title = "
            "'Student identifiers (SSID, full name) must not leave the EMR boundary in unredacted form'"
        )
        row = cur.fetchone()
    assert row is not None, "ferpa-student-id rule missing"
    assert row[0] == ['ferpa']


@pytest.mark.db
def test_023_seeds_bulk_export_signoff_rule(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles FROM devbrain.memory "
            "WHERE tier = 'rule' AND title = 'Bulk exports require explicit DPO sign-off claim in the request'"
        )
        row = cur.fetchone()
    assert row is not None, "bulk-export-signoff rule missing"
    assert row[0] == ['soc2']
