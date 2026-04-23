"""Tests for dashboard widget pure helpers."""
from datetime import datetime, timedelta, timezone

import pytest

from dashboard.widgets.jobs_panel import _format_age, _humanize_age


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (1, "1s"),
        (30, "30s"),
        (59, "59s"),
        # Minute band
        (60, "1m 0s"),
        (61, "1m 1s"),
        (332, "5m 32s"),
        (3599, "59m 59s"),
        # Hour band
        (3600, "1h 0m"),
        (3660, "1h 1m"),
        (8100, "2h 15m"),
        (86399, "23h 59m"),
        # Day band
        (86400, "1d 0h"),
        (97200, "1d 3h"),
        (172800, "2d 0h"),
    ],
)
def test_humanize_age_bands(seconds, expected):
    assert _humanize_age(seconds) == expected


def test_humanize_age_clock_skew_clamps_to_zero():
    assert _humanize_age(-5) == "0s"


def test_format_age_none_returns_question_mark():
    assert _format_age(None) == "?"


def test_format_age_naive_datetime_treated_as_utc():
    naive = datetime.utcnow() - timedelta(seconds=42)
    result = _format_age(naive)
    # Allow a small drift window from test execution time.
    assert result.endswith("s")
    secs = int(result.removesuffix("s"))
    assert 40 <= secs <= 60


def test_format_age_aware_datetime_in_minute_band():
    aware = datetime.now(timezone.utc) - timedelta(seconds=332)
    result = _format_age(aware)
    # Should be "5m Xs" with X close to 32 (allow drift).
    assert result.startswith("5m ")
    assert result.endswith("s")


def test_format_age_future_timestamp_clamps_to_zero():
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    assert _format_age(future) == "0s"


def test_format_age_invalid_input_returns_question_mark():
    assert _format_age("not a datetime") == "?"
