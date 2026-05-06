"""Tests for curator.rules_lint — postulate-coverage enforcement.

Strategy
--------
The lint reads tests/postulates/*.py for its corpus. Tests use
`monkeypatch` to redirect _POSTULATES_DIR to a tmp dir so the test
harness fully controls the corpus. This avoids false positives from
the real postulate suite and false negatives from rules whose titles
happen to collide with real postulate words.

DB-touching tests are gated on @pytest.mark.db; the slugify helper
test runs without a DB.
"""
from __future__ import annotations

import uuid

import pytest

from curator import rules_lint


# ---------------------------------------------------------------------------
# Pure helper tests (no DB).
# ---------------------------------------------------------------------------

def test_slugify():
    assert rules_lint._slugify("PHI must not appear in unstructured logs") == (
        "phi_must_not_appear_in_unstructured_logs"
    )
    assert rules_lint._slugify("HIPAA: log scrubbing") == "hipaa_log_scrubbing"
    assert rules_lint._slugify("  leading & trailing  ") == "leading_trailing"
    assert rules_lint._slugify("") == ""
    assert rules_lint._slugify("   ") == ""
    # Unicode characters that aren't ASCII alphanumerics get collapsed too.
    assert rules_lint._slugify("rule—em-dash") == "rule_em_dash"


# ---------------------------------------------------------------------------
# DB tests — exercise the SELECT + corpus-matching logic end-to-end.
# ---------------------------------------------------------------------------

def _seed_postulate(tmp_dir, name: str, body: str) -> None:
    """Write a fake postulate file under tmp_dir."""
    p = tmp_dir / f"test_{name}.py"
    p.write_text(body)


def _redirect_postulates_dir(monkeypatch, tmp_path):
    """Point the lint at a tmp postulates dir for hermetic testing."""
    monkeypatch.setattr(rules_lint, "_POSTULATES_DIR", tmp_path)


@pytest.mark.db
def test_no_rules_returns_empty(conn, project_factory, monkeypatch, tmp_path):
    """Empty test project (no profile-tagged rules) -> empty list.

    Scope to a freshly-created project so seeded rules from migration 023
    in the canonical 'devbrain' project don't pollute the assertion.
    """
    project = project_factory("rl_empty")
    _redirect_postulates_dir(monkeypatch, tmp_path)
    assert rules_lint.find_unverified_rules(conn, project_id=project["id"]) == []


@pytest.mark.db
def test_rule_with_uuid_in_postulate_passes(
    conn, project_factory, memory_factory, monkeypatch, tmp_path
):
    """A postulate that names the rule's UUID counts as verified."""
    project = project_factory("rl_uuid")
    rule = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="HIPAA log scrubbing rule",
        compliance_profiles=["hipaa"],
    )
    _seed_postulate(
        tmp_path,
        "rl_uuid_match",
        f"# this postulate verifies rule {rule['id']}\n",
    )
    _redirect_postulates_dir(monkeypatch, tmp_path)

    assert rules_lint.find_unverified_rules(conn, project_id=project["id"]) == []


@pytest.mark.db
def test_rule_with_title_slug_in_postulate_passes(
    conn, project_factory, memory_factory, monkeypatch, tmp_path
):
    """A postulate referencing the slugified title counts as verified."""
    project = project_factory("rl_slug")
    title = "PHI must not appear in unstructured logs"
    memory_factory(
        project["id"], kind="decision", tier="rule",
        title=title, content="rule body",
        compliance_profiles=["hipaa"],
    )
    _seed_postulate(
        tmp_path,
        "rl_slug_match",
        "def test_phi_must_not_appear_in_unstructured_logs():\n    pass\n",
    )
    _redirect_postulates_dir(monkeypatch, tmp_path)

    assert rules_lint.find_unverified_rules(conn, project_id=project["id"]) == []


@pytest.mark.db
def test_rule_without_postulate_fails(
    conn, project_factory, memory_factory, monkeypatch, tmp_path
):
    """A profile-tagged rule with no matching postulate is flagged."""
    project = project_factory("rl_miss")
    # Use a deliberately-unique title so even an empty-corpus accident
    # won't produce a false negative via substring overlap.
    unique = uuid.uuid4().hex
    rule = memory_factory(
        project["id"], kind="decision", tier="rule",
        title=f"unverified_rule_{unique}",
        content="missing postulate",
        compliance_profiles=["hipaa"],
    )
    _seed_postulate(tmp_path, "unrelated", "# nothing relevant here\n")
    _redirect_postulates_dir(monkeypatch, tmp_path)

    unverified = rules_lint.find_unverified_rules(conn, project_id=project["id"])
    flagged_ids = {r.id for r in unverified}
    assert rule["id"] in flagged_ids


@pytest.mark.db
def test_empty_profiles_rule_skipped(
    conn, project_factory, memory_factory, monkeypatch, tmp_path
):
    """Rules with empty/NULL compliance_profiles are not subject to lint
    (they're invisible by P6 anyway — no enforcement needed)."""
    project = project_factory("rl_emptyprof")
    # No compliance_profiles kwarg -> column stays NULL.
    memory_factory(
        project["id"], kind="decision", tier="rule",
        content="rule with no profiles",
    )
    _redirect_postulates_dir(monkeypatch, tmp_path)
    # No matching postulate exists, but lint should not flag.
    assert rules_lint.find_unverified_rules(conn, project_id=project["id"]) == []


@pytest.mark.db
def test_archived_rule_skipped(
    conn, project_factory, memory_factory, monkeypatch, tmp_path
):
    """Archived rules don't surface in any brief, so no postulate needed."""
    project = project_factory("rl_archived")
    rule = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="archived hipaa rule",
        compliance_profiles=["hipaa"],
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW() WHERE id = %s",
            (rule["id"],),
        )
    conn.commit()
    _redirect_postulates_dir(monkeypatch, tmp_path)

    assert rules_lint.find_unverified_rules(conn, project_id=project["id"]) == []


@pytest.mark.db
def test_run_lint_exit_code_clean(
    conn, project_factory, monkeypatch, tmp_path, capsys
):
    """Empty test project + matching postulates dir → exit 0.

    Uses a fresh project to scope away from canonical 'devbrain' seeded
    rules (which live in production-shape state and aren't subject to
    test invariants)."""
    project = project_factory("rl_clean")
    _redirect_postulates_dir(monkeypatch, tmp_path)
    rc = rules_lint.run_lint(conn, project_id=project["id"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out


@pytest.mark.db
def test_run_lint_exit_code_violations(
    conn, project_factory, memory_factory, monkeypatch, tmp_path, capsys
):
    project = project_factory("rl_violation")
    unique = uuid.uuid4().hex
    rule = memory_factory(
        project["id"], kind="decision", tier="rule",
        title=f"violating_rule_{unique}",
        content="needs postulate",
        compliance_profiles=["hipaa"],
    )
    _redirect_postulates_dir(monkeypatch, tmp_path)
    rc = rules_lint.run_lint(conn, project_id=project["id"])
    assert rc == 1
    captured = capsys.readouterr()
    assert str(rule["id"]) in captured.err
    assert "hipaa" in captured.err


@pytest.mark.db
def test_unverified_rule_has_full_payload(
    conn, project_factory, memory_factory, monkeypatch, tmp_path
):
    """The UnverifiedRule dataclass carries id, title, profiles."""
    project = project_factory("rl_payload")
    unique = uuid.uuid4().hex
    title = f"payload_rule_{unique}"
    rule = memory_factory(
        project["id"], kind="decision", tier="rule",
        title=title, content="x",
        compliance_profiles=["hipaa", "soc2"],
    )
    _redirect_postulates_dir(monkeypatch, tmp_path)
    unverified = rules_lint.find_unverified_rules(conn, project_id=project["id"])
    target = next((r for r in unverified if r.id == rule["id"]), None)
    assert target is not None
    assert target.title == title
    assert set(target.compliance_profiles) == {"hipaa", "soc2"}


def test_read_corpus_missing_dir(monkeypatch, tmp_path):
    """If _POSTULATES_DIR doesn't exist, _read_postulates_corpus returns ''."""
    monkeypatch.setattr(rules_lint, "_POSTULATES_DIR", tmp_path / "nope")
    assert rules_lint._read_postulates_corpus() == ""


def test_read_corpus_unreadable_file_skipped(monkeypatch, tmp_path):
    """If a candidate file errors on read_text, the lint silently skips it
    rather than aborting — keeps a single bad file from masking real
    violations elsewhere."""
    good = tmp_path / "test_good.py"
    good.write_text("good content")
    bad = tmp_path / "test_bad.py"
    bad.write_text("bad content")

    real_read_text = type(bad).read_text

    def stub_read_text(self, *args, **kwargs):
        if self.name == "test_bad.py":
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(bad), "read_text", stub_read_text)
    monkeypatch.setattr(rules_lint, "_POSTULATES_DIR", tmp_path)

    corpus = rules_lint._read_postulates_corpus()
    assert "good content" in corpus
    assert "bad content" not in corpus


def test_has_matching_postulate_empty_title():
    """_has_matching_postulate returns False for an empty title with no
    UUID hit (slug becomes '', which would otherwise match every corpus)."""
    rid = uuid.uuid4()
    assert rules_lint._has_matching_postulate(rid, "", "anything") is False
    assert rules_lint._has_matching_postulate(rid, "   ", "anything") is False
