"""brain_doctor cognify_liveness: every scheduled pass must have completed
within its cadence gap (no DB; a fake cursor answers the run-log queries)."""

from datetime import datetime, timedelta, timezone

import brain_doctor as bd


class _Cur:
    """Answers max(started_at) per pass from ``ages_h`` (None = never ran)
    and the 24 h error count."""

    def __init__(self, ages_h, errors=0):
        self.now = datetime.now(timezone.utc)
        self.ages_h = ages_h
        self.errors = errors
        self._row = None

    def execute(self, sql, params=None):
        if "count(*)" in sql:
            self._row = (self.errors,)
        else:
            age = self.ages_h.get(params[0])
            self._row = (None if age is None else self.now - timedelta(hours=age),)

    def fetchone(self):
        return self._row


def _fresh():
    return {name: 0.5 for name, _ in bd.CADENCE_MAX_GAP_H}


def test_all_passes_fresh_pass():
    assert bd.check_cognify_liveness(_Cur(_fresh()))[0] == "PASS"


def test_entities_is_expected_every_two_hours():
    gaps = dict(bd.CADENCE_MAX_GAP_H)
    assert gaps["entities"] == 6
    ok = _fresh() | {"entities": 5}
    assert bd.check_cognify_liveness(_Cur(ok))[0] == "PASS"
    late = _fresh() | {"entities": 7}
    status, detail = bd.check_cognify_liveness(_Cur(late))
    assert status == "FAIL" and "entities last completed" in detail
    never = _fresh() | {"entities": None}
    assert "entities last completed None" in bd.check_cognify_liveness(_Cur(never))[1]


def test_sweep_is_expected_nightly():
    gaps = dict(bd.CADENCE_MAX_GAP_H)
    assert gaps["sweep"] == 26
    # A nightly job is up to 24 h old between runs: still fine.
    assert bd.check_cognify_liveness(_Cur(_fresh() | {"sweep": 24.5}))[0] == "PASS"
    status, detail = bd.check_cognify_liveness(_Cur(_fresh() | {"sweep": 27}))
    assert status == "FAIL" and "sweep last completed" in detail
    assert "entities" not in detail
