"""brain_doctor cognify_output: silent failures inside cognify_run_log
metadata (no DB; metadata shapes copied from the Studio run log of
2026-10-28)."""

from datetime import datetime, timedelta, timezone

import brain_doctor as bd

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)


def run(pass_name, md, *, hours_ago=1.0, dur_s=1.0, rows=0, project=None, done=True):
    started = NOW - timedelta(hours=hours_ago)
    completed = started + timedelta(seconds=dur_s) if done else None
    return (pass_name, project, started, completed, rows, md)


def ev(rows, **kw):
    kw.setdefault("slow_s", 1800)
    return bd.evaluate_cognify_runs(rows, now=NOW, **kw)


# Real shapes (Studio, 2026-10-28).
RESUMMARIZE_NO_AUTH = {"pass": "resummarize", "failure_counts": {"no_auth": 859},
                       "sessions_failed": 859, "sessions_discovered": 859}
FANOUT_NO_AUTH = {"pass": "fanout", "failure_counts": {"no_auth": 1156},
                  "sessions_failed": 1156, "sessions_skipped": 0,
                  "sessions_processed": 0, "sessions_discovered": 1156}
EDGES_OLD = {"pass": "edges", "cites_edges_created": 0, "contradicts_edges_created": 0}
DECAY = {"pass": "decay"}
EXTRACT_IDLE = {"pass": "extract", "since": "2026-10-28T03:19:52+00:00", "backlog": [],
                "failure_counts": {}, "lessons_created": 0, "sessions_processed": 0}
EXTRACT_EMPTY = {"pass": "extract", "backlog": [{"id": "a", "tries": 0}] * 90,
                 "failure_counts": {}, "lessons_created": 0, "sessions_processed": 20}


def test_the_studio_on_2026_10_28_fails():
    rows = [run("resummarize", RESUMMARIZE_NO_AUTH, hours_ago=h) for h in (1, 2, 3)]
    rows += [run("fanout", FANOUT_NO_AUTH, hours_ago=h) for h in (1.5, 3.5)]
    rows += [run("edges", EDGES_OLD, dur_s=74, project="37ebd8d4"), run("decay", DECAY)]
    rows += [run("extract", EXTRACT_IDLE, project="37ebd8d4", hours_ago=h) for h in (1, 2)]
    status, detail = ev(rows)
    assert status == "FAIL"
    assert "resummarize: no_auth=2577 in 3 run(s)" in detail
    assert "fanout: no_auth=2312 in 2 run(s)" in detail
    assert "resummarize: 0 processed in 3 runs with 859 waiting" in detail
    assert "edges" not in detail and "extract" not in detail


def test_healthy_runs_pass():
    rows = [
        run("resummarize", {"sessions_discovered": 859, "sessions_processed": 20,
                            "failure_counts": {"api": 1}, "backlog_remaining": 839},
            rows=20, hours_ago=h) for h in (1, 2)
    ]
    rows += [run("extract", EXTRACT_IDLE, hours_ago=h) for h in (1, 2)]
    rows += [run("edges", {"pairs_judged": 15, "seeds_pending": 40,
                           "failure_counts": {}}, rows=2)]
    assert ev(rows)[0] == "PASS"


def test_run_level_blocked_fails_even_without_unit_failures():
    md = {"sessions_discovered": 859, "sessions_processed": 0, "failure_counts": {},
          "blocked": "no_auth", "allowed_backends": ["anthropic", "codex"]}
    status, detail = ev([run("resummarize", md)])
    assert status == "FAIL" and "resummarize: no_auth in 1 run(s)" in detail


def test_one_empty_run_is_not_a_stall_but_repeats_are():
    md = {"pairs_judged": 0, "seeds_pending": 12, "failure_counts": {}}
    assert ev([run("edges", md)])[0] == "PASS"
    status, detail = ev([run("edges", md, hours_ago=h) for h in (1, 7, 13)])
    assert status == "FAIL" and "edges: 0 processed in 3 runs with 12 waiting" in detail


def test_edges_seeds_without_candidates_count_as_progress():
    md = {"pairs_judged": 0, "seeds_done": 40, "seeds_pending": 160, "failure_counts": {}}
    assert ev([run("edges", md, hours_ago=h) for h in (1, 7)])[0] == "PASS"


def test_extract_refusal_after_237_fails():
    """#237: a missing credential is a `refused` unit and the pass stops,
    carrying the backlog (shape from test_pass_stops_at_first_refusal)."""
    md = {"pass": "extract", "failure_counts": {"refused": 1}, "stopped": "refused",
          "backlog": [{"id": s, "tries": 0} for s in "abc"], "zero_yield": {"refused": 1},
          "fallback": {}, "items": {}, "lessons_created": 0, "sessions_processed": 1}
    status, detail = ev([run("extract", md, project="722c9514-1aa8")])
    assert status == "FAIL" and "extract: refused=1 in 1 run(s)" in detail
    # Without a per-unit count, `stopped` alone still fails.
    md2 = dict(md, failure_counts={})
    assert "extract: refused in 1 run(s)" in ev([run("extract", md2)])[1]
    # On a deployment that allows no hosted backend it is by design.
    assert ev([run("extract", md, hours_ago=h) for h in (1, 2)],
              refusal_expected=True)[0] == "PASS"


def test_fallback_refusal_alone_is_not_an_auth_failure():
    """codex hit an API error and the Anthropic fallback had no key (the
    Studio's normal setup): that unit failed `api`, not auth. Repeated
    zero output is still caught by the stall rule."""
    md = {"failure_counts": {"api": 1}, "fallback": {"failed:refused": 1},
          "backlog": [{"id": "a", "tries": 1}], "stopped": None}
    assert ev([run("extract", md, rows=4)])[0] == "PASS"
    status, detail = ev([run("extract", md, hours_ago=h) for h in (1, 2)])
    assert status == "FAIL" and "extract: 0 processed in 2 runs" in detail


def test_extract_working_through_backlog_with_no_output_fails():
    """20 sessions a run, 90 waiting, nothing extracted: a silent stall."""
    status, detail = ev([run("extract", EXTRACT_EMPTY, project="722c9514-1aa8", hours_ago=h)
                         for h in (1, 4)])
    assert status == "FAIL" and "extract [722c9514]: 0 processed in 2 runs" in detail


def test_stall_needs_every_run_stuck():
    stuck = {"sessions_discovered": 5, "sessions_processed": 0, "failure_counts": {}}
    ok = {"sessions_discovered": 5, "sessions_processed": 5, "failure_counts": {}}
    rows = [run("fanout", stuck, hours_ago=3), run("fanout", ok, hours_ago=2),
            run("fanout", stuck, hours_ago=1)]
    assert ev(rows)[0] == "PASS"


def test_policy_refusal_on_a_phi_deployment_is_not_a_failure():
    md = {"sessions_discovered": 40, "sessions_processed": 0,
          "failure_counts": {"refused": 40}}
    rows = [run("fanout", md, hours_ago=h) for h in (1, 2)]
    assert ev(rows, refusal_expected=True)[0] == "PASS"
    status, detail = ev(rows)  # refusals where backends are allowed: misconfig
    assert status == "FAIL" and "fanout: refused=80" in detail
    blocked = {"blocked": "refused", "allowed_backends": [], "sessions_discovered": 3,
               "sessions_processed": 0, "failure_counts": {}}
    assert ev([run("resummarize", blocked, hours_ago=h) for h in (1, 2)])[0] == "PASS"


def test_no_auth_is_a_failure_even_where_refusals_are_expected():
    md = {"failure_counts": {"no_auth": 3}, "sessions_discovered": 3, "sessions_processed": 0}
    assert ev([run("fanout", md)], refusal_expected=True)[0] == "FAIL"


def test_prefixed_failure_labels_count():
    md = {"failure_counts": {"refused:openai": 2, "api:openai": 1},
          "sessions_discovered": 3, "sessions_processed": 1}
    status, detail = ev([run("fanout", md)])
    assert status == "FAIL" and "refused=2" in detail


def test_slow_and_abandoned_runs_warn():
    rows = [
        run("edges", {"pairs_judged": 3, "seeds_pending": 0}, dur_s=2400),
        run("resummarize", {}, hours_ago=2, done=False),
        run("decay", DECAY, hours_ago=0.1, done=False),  # still running, recent
    ]
    status, detail = ev(rows)
    assert status == "WARN"
    assert "edges took 2400s" in detail and "resummarize started" in detail
    assert "decay" not in detail


def test_dry_runs_are_ignored():
    md = dict(RESUMMARIZE_NO_AUTH, dry_run=True)
    assert ev([run("resummarize", md, hours_ago=h) for h in (1, 2)])[0] == "PASS"


def test_hosted_llm_disabled_reads_env(monkeypatch):
    monkeypatch.setenv("DEVBRAIN_COGNIFY_ALLOWED_BACKENDS", "")
    assert bd._hosted_llm_disabled() is True
    monkeypatch.setenv("DEVBRAIN_COGNIFY_ALLOWED_BACKENDS", "codex,anthropic")
    assert bd._hosted_llm_disabled() is False


def test_check_is_registered_after_liveness():
    names = [n for n, _ in bd.CHECKS]
    assert names.index("cognify_output") == names.index("cognify_liveness") + 1
