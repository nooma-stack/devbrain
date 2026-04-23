"""Tests for the WARNING oscillation guardrail.

Covers the 2026-04-23 escalation that fires when the same WARNING
findings persist across consecutive review rounds. The fix loop
otherwise spends an implementer pass per round on findings the
implementer apparently can't or won't fix — escalating instead to
a human is the bounded-cost option.

The guardrail itself lives at the end of `_run_review` in
orchestrator.py, fired after the should_fix check and before the
normal FIX_LOOP transition. We exercise it end-to-end by stubbing
`run_cli` (no actual claude call) and `subprocess.run` (no
`git diff main...HEAD`) and reading back the post-review job
status + metadata.
"""
import pytest

import orchestrator as orch_mod
from orchestrator import (
    FactoryOrchestrator,
    _findings_overlap,
)
from state_machine import FactoryDB, JobStatus
from config import DATABASE_URL

TEST_TITLE_PREFIX = "oscillation_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.factory_jobs WHERE title LIKE %s",
            (f"{TEST_TITLE_PREFIX}%",),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "DELETE FROM devbrain.factory_cleanup_reports "
                "WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts "
                "WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL "
                "WHERE blocked_by_job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()


@pytest.fixture
def orch():
    return FactoryOrchestrator(DATABASE_URL)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeCLIResult:
    """Shape-compatible with cli_executor.CLIResult for what _run_review reads."""

    def __init__(self, stdout: str):
        self.cli = "claude"
        self.exit_code = 0
        self.stdout = stdout
        self.stderr = ""
        self.success = True


def _stub_review_env(monkeypatch, *, arch_stdout: str, sec_stdout: str):
    """Stub run_cli (both arch + security reviews) and subprocess.run
    (git diff) so _run_review exercises the gate without touching CLIs
    or git. run_cli returns arch_stdout then sec_stdout in order."""
    responses = iter([_FakeCLIResult(arch_stdout), _FakeCLIResult(sec_stdout)])

    def fake_run_cli(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(orch_mod, "run_cli", fake_run_cli)
    monkeypatch.setattr(
        orch_mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(stdout="diff --git a/x b/x\n"),
    )


def _make_job_in_fix_cycle(
    db: FactoryDB,
    title: str,
    *,
    prior_arch_text: str = "1. WARNING: same warning at file.py:10",
    prior_sec_text: str = "(no findings)",
):
    """Create a job, seed it with one prior round of WARNING-only review
    artifacts, and walk it QUEUED → PLANNING → IMPLEMENTING → REVIEWING
    → FIX_LOOP → IMPLEMENTING. Returns the job in IMPLEMENTING state
    with error_count=1 — i.e., ready for _run_review to fire round 2's
    gate against the seeded prior round.

    The two seeded review artifacts represent round 1's arch + security
    review. _run_review will add round 2's pair when called, giving
    `_get_last_round_warnings` exactly the [-4:-2] slice it expects.
    """
    job_id = db.create_job(project_slug="devbrain", title=title, spec="test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(
        job_id=job_id, phase="review", artifact_type="arch_review",
        content=prior_arch_text,
        warning_count=prior_arch_text.upper().count("WARNING"),
    )
    db.store_artifact(
        job_id=job_id, phase="review", artifact_type="security_review",
        content=prior_sec_text,
        warning_count=prior_sec_text.upper().count("WARNING"),
    )
    db.transition(job_id, JobStatus.FIX_LOOP, metadata={"warning_findings": 1})
    db.transition(job_id, JobStatus.IMPLEMENTING)
    return db.get_job(job_id)


# ─── Unit test ────────────────────────────────────────────────────────────

def test_findings_overlap_matches_on_signature_returns_original():
    """`_findings_overlap` matches on normalized signatures (case-
    insensitive, whitespace-collapsed, prefix-truncated) but returns
    the original current-round text — never the signature — so that
    humans read the reviewer's own words in the notification and
    job metadata, not a lowercased stub."""
    # Same finding, different case + whitespace → returns current original
    current = ["WARNING: missing null check at x.py:42"]
    prior = ["warning:   missing  null check  at x.py:42"]
    assert _findings_overlap(current, prior) == [
        "WARNING: missing null check at x.py:42"
    ]

    # Different findings → empty intersection
    assert _findings_overlap(["foo"], ["bar"]) == []

    # Truncation at 80 chars — same prefix matches even when suffixes
    # diverge; the returned text is the full original current item.
    long_prefix = "warning: identical first eighty chars of finding text padding xxxxxxxxxxxxxxxxxxxxxx"
    assert len(long_prefix) >= 80
    current_item = long_prefix + " distinct suffix one"
    assert _findings_overlap(
        [current_item],
        [long_prefix + " entirely different suffix two"],
    ) == [current_item]

    # Duplicate current-round findings with the same signature fold to
    # the first occurrence — stable, reviewer-ordered output.
    dup = "WARNING: same thing at z.py:1"
    assert _findings_overlap([dup, dup], [dup]) == [dup]


# ─── Gate behavior ────────────────────────────────────────────────────────

def test_repeating_warnings_escalate_to_failed(orch, db, monkeypatch):
    """When the same WARNING appears in two consecutive rounds with no
    blockers, the gate transitions REVIEWING → FAILED with structured
    metadata instead of bouncing through FIX_LOOP again."""
    monkeypatch.setattr(orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True)
    job = _make_job_in_fix_cycle(
        db,
        f"{TEST_TITLE_PREFIX}escalate",
        prior_arch_text="1. WARNING: missing null check at x.py:42",
    )

    _stub_review_env(
        monkeypatch,
        arch_stdout="1. WARNING: missing null check at x.py:42",
        sec_stdout="(no findings)",
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FAILED
    assert result.metadata.get("failure") == "warning_oscillation"
    assert result.metadata.get("error_count_at_escalation") == 1
    repeating = result.metadata.get("repeating_warnings", [])
    assert repeating, "expected at least one repeating warning signature"
    assert any("missing null check" in r for r in repeating)


def test_blocking_findings_bypass_oscillation_guardrail(orch, db, monkeypatch):
    """BLOCKING always wins: even if WARNINGs are repeating round to
    round, the presence of a real BLOCKING finding routes the job
    back to FIX_LOOP. We don't want to give up on a real bug."""
    monkeypatch.setattr(orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True)
    job = _make_job_in_fix_cycle(
        db,
        f"{TEST_TITLE_PREFIX}blocking_wins",
        prior_arch_text="1. WARNING: same warning persisting at y.py:1",
    )

    _stub_review_env(
        monkeypatch,
        arch_stdout=(
            "1. WARNING: same warning persisting at y.py:1\n"
            "2. BLOCKING: actual exploit at y.py:5\n"
        ),
        sec_stdout="(no findings)",
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FIX_LOOP
    assert result.metadata.get("trigger_reason") == "blocking"
    assert result.metadata.get("blocking_findings") == 1


def test_different_warnings_do_not_escalate(orch, db, monkeypatch):
    """If round 2's WARNINGs do not overlap round 1's, the loop is
    making progress on something — guardrail does not fire and the
    job goes back through FIX_LOOP normally."""
    monkeypatch.setattr(orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True)
    job = _make_job_in_fix_cycle(
        db,
        f"{TEST_TITLE_PREFIX}different",
        prior_arch_text="1. WARNING: old warning at a.py:1",
    )

    _stub_review_env(
        monkeypatch,
        arch_stdout="1. WARNING: brand new completely different warning at b.py:2",
        sec_stdout="(no findings)",
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FIX_LOOP
    assert result.metadata.get("trigger_reason") == "warning"


def test_first_round_no_prior_state_does_not_escalate(orch, db, monkeypatch):
    """Round 1 (error_count=0) cannot trigger the guardrail — there is
    no prior round to compare against. The job follows the normal
    WARNING → FIX_LOOP path even when the same string would have
    matched, because the guard only inspects history that exists."""
    monkeypatch.setattr(orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True)

    # Bare job — no prior review artifacts seeded, error_count=0.
    job_id = db.create_job(
        project_slug="devbrain",
        title=f"{TEST_TITLE_PREFIX}first_round",
        spec="test",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    job = db.get_job(job_id)
    assert job.error_count == 0

    _stub_review_env(
        monkeypatch,
        arch_stdout="1. WARNING: any warning at a.py:1",
        sec_stdout="(no findings)",
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FIX_LOOP
    assert result.metadata.get("trigger_reason") == "warning"
    assert "failure" not in result.metadata


# ─── Notification side-effect ────────────────────────────────────────────
# `_notify_warning_oscillation` is wrapped in a silently-swallowing
# try/except so a body-format bug (wrong field name, KeyError) would
# never surface in prod. These two tests are the only thing that would
# catch that rot — they stub NotificationRouter at its import site and
# assert on the captured event.

class _FakeRouter:
    """Captures NotificationEvent instances sent via .send(). One
    instance per test — reset via the `sent_events` class attribute
    inside each test."""
    sent_events: list = []

    def __init__(self, db, *args, **kwargs):
        pass

    def send(self, event):
        type(self).sent_events.append(event)


def _stub_notification_router(monkeypatch):
    """Patch NotificationRouter at its source module. The orchestrator
    imports it lazily inside `_notify_warning_oscillation`, so we patch
    where it is defined — `factory.notifications.router` — not where
    it is referenced."""
    from notifications import router as router_module

    _FakeRouter.sent_events = []
    monkeypatch.setattr(router_module, "NotificationRouter", _FakeRouter)


def test_oscillation_notification_fires_with_correct_payload(orch, db, monkeypatch):
    """The FAILED transition's side-effect notification fires with
    event_type=needs_human, carries the job id and repeating findings
    in metadata, and renders the original reviewer text (not a
    lowercased signature) into the body."""
    monkeypatch.setattr(orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True)
    _stub_notification_router(monkeypatch)

    job = _make_job_in_fix_cycle(
        db,
        f"{TEST_TITLE_PREFIX}notify_fires",
        prior_arch_text="1. WARNING: Missing Null Check at X.py:42",
    )
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            ("test-dev", job.id),
        )
        conn.commit()
    job = db.get_job(job.id)

    _stub_review_env(
        monkeypatch,
        arch_stdout="1. WARNING: Missing Null Check at X.py:42",
        sec_stdout="(no findings)",
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FAILED
    assert len(_FakeRouter.sent_events) == 1
    event = _FakeRouter.sent_events[0]
    assert event.event_type == "needs_human"
    assert event.recipient_dev_id == "test-dev"
    assert event.job_id == job.id
    assert event.metadata.get("repeating_warnings")
    # Original reviewer text — preserves case — reaches metadata + body.
    assert any(
        "Missing Null Check" in r for r in event.metadata["repeating_warnings"]
    )
    assert "Missing Null Check" in event.body


def test_oscillation_notification_skips_when_no_submitted_by(orch, db, monkeypatch):
    """When job.submitted_by is None there is nobody to notify — the
    early-return branch fires before any NotificationEvent is built.
    The FAILED transition itself still commits."""
    monkeypatch.setattr(orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True)
    _stub_notification_router(monkeypatch)

    # _make_job_in_fix_cycle leaves submitted_by=NULL by default.
    job = _make_job_in_fix_cycle(
        db,
        f"{TEST_TITLE_PREFIX}notify_no_submitter",
        prior_arch_text="1. WARNING: same issue at w.py:1",
    )
    assert job.submitted_by is None

    _stub_review_env(
        monkeypatch,
        arch_stdout="1. WARNING: same issue at w.py:1",
        sec_stdout="(no findings)",
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FAILED
    assert _FakeRouter.sent_events == []
