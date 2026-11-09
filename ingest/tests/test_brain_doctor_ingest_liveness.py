"""brain_doctor ingest_liveness: FAIL only when a transcript is waiting.

Regression (2026-11-07..09): the check failed whenever ingest.log had not
been written for 3h, so every quiet night or weekend sent a FAIL email
while the watcher was healthy.
"""

import brain_doctor as bd

NOW = 1_800_000_000.0
H = 3600


def ev(log_age_h, files):
    log_mtime = None if log_age_h is None else NOW - log_age_h * H
    return bd.evaluate_ingest_liveness(log_mtime, files, now=NOW)


def f(age_h, size=50_000, name="s.jsonl"):
    return (name, NOW - age_h * H, size)


def test_quiet_overnight_is_pass():
    # Last transcript landed 15h ago, ingest handled it right after.
    status, msg = ev(14.9, [f(15), f(40)])
    assert status == "PASS"
    assert "nothing waiting" in msg


def test_transcript_changed_after_ingest_is_fail():
    status, msg = ev(6, [f(2), f(5), f(30)])
    assert status == "FAIL"
    assert "2 transcript(s)" in msg
    assert "oldest waiting 5.0h" in msg


def test_within_grace_is_pass():
    # Large session still re-embedding: file 20 min newer than the log.
    status, _ = ev(1.0, [f(1.0 - 20 / 60)])
    assert status == "PASS"


def test_tiny_files_ignored_like_the_watcher():
    status, _ = ev(6, [f(1, size=500)])
    assert status == "PASS"


def test_missing_log_warns():
    assert ev(None, [f(1)])[0] == "WARN"
