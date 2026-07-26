"""
Unit tests for training_load._recompute() behaviour.

Run with: python -m pytest tests/test_training_load.py -v
No GCS credentials required — tests use pure in-memory state.
"""

import math
import sys
import os

# Ensure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from training_load import _recompute, _K_CTL, _K_ATL, _DECAY_CTL, _DECAY_ATL


def _make_state(seed_date, seed_ctl, seed_atl, activities):
    return {
        "seed_date": seed_date,
        "seed_ctl": float(seed_ctl),
        "seed_atl": float(seed_atl),
        "activities": activities,
    }


def test_single_activity_then_rest():
    """Day 1: one activity load=87; Day 2: rest. TSB for Day 2 is lag-1 (before decay)."""
    state = _make_state(
        "2026-01-01", 40.0, 40.0,
        [{"date": "2026-01-01", "load": 87.0}],
    )

    # Day 1 end-of-day
    ctl1, atl1, tsb1 = _recompute(state, "2026-01-01")

    # TSB day 1 is the morning value (before load + decay), i.e. seed_ctl - seed_atl
    assert tsb1 == round(40.0 - 40.0), f"Expected TSB=0 for day 1, got {tsb1}"

    # Manually verify day 1 EoD
    load = 87.0
    expected_ctl1 = (40.0 + (load - 40.0) * _K_CTL) * _DECAY_CTL
    expected_atl1 = (40.0 + (load - 40.0) * _K_ATL) * _DECAY_ATL
    assert abs(ctl1 - expected_ctl1) < 1e-9
    assert abs(atl1 - expected_atl1) < 1e-9

    # Day 2: rest day
    ctl2, atl2, tsb2 = _recompute(state, "2026-01-02")

    # TSB day 2 = morning of day 2 = EoD of day 1 (ctl1 - atl1)
    assert tsb2 == round(ctl1 - atl1), f"TSB lag-1 failed: expected {round(ctl1 - atl1)}, got {tsb2}"

    # Rest day: just decay
    expected_ctl2 = ctl1 * _DECAY_CTL
    expected_atl2 = atl1 * _DECAY_ATL
    assert abs(ctl2 - expected_ctl2) < 1e-9
    assert abs(atl2 - expected_atl2) < 1e-9


def test_duplicate_activity_dropped():
    """Two activity records with the same date and load: should sum (not double-count)."""
    # _recompute sums loads for same date, so two entries with load=50 = total load 100
    state_single = _make_state(
        "2026-01-01", 30.0, 30.0,
        [{"date": "2026-01-01", "load": 100.0}],
    )
    state_double = _make_state(
        "2026-01-01", 30.0, 30.0,
        [{"date": "2026-01-01", "load": 50.0},
         {"date": "2026-01-01", "load": 50.0}],
    )

    ctl_s, atl_s, _ = _recompute(state_single, "2026-01-01")
    ctl_d, atl_d, _ = _recompute(state_double, "2026-01-01")

    assert abs(ctl_s - ctl_d) < 1e-9, "Duplicate loads not summed correctly"
    assert abs(atl_s - atl_d) < 1e-9, "Duplicate loads not summed correctly"


def test_add_activity_idempotent():
    """add_activity() must reject a duplicate (same date + load within 0.01)."""
    import unittest.mock as mock

    with mock.patch("training_load._load_state") as mock_load, \
         mock.patch("training_load._save_state") as mock_save:

        mock_load.return_value = {
            "seed_date": "2026-01-01",
            "seed_ctl": 30.0,
            "seed_atl": 30.0,
            "activities": [{"date": "2026-01-02", "load": 50.0}],
        }

        from training_load import add_activity

        # Same date + same load → must return False (skipped)
        result = add_activity("2026-01-02", 50.0)
        assert result is False, "Duplicate add_activity should return False"
        mock_save.assert_not_called()

        # Same date + different load → must be added
        mock_save.reset_mock()
        result = add_activity("2026-01-02", 99.0)
        assert result is True, "Different load on same date should be added"
        mock_save.assert_called_once()


def test_rest_day_zero_filled():
    """Days between seed and an activity with no load entry are treated as rest (zero load)."""
    state = _make_state(
        "2026-01-01", 40.0, 40.0,
        [{"date": "2026-01-03", "load": 60.0}],  # day 3 only; days 1-2 are rest
    )

    # Day 2 should be a pure decay day (no load)
    ctl2, atl2, tsb2 = _recompute(state, "2026-01-02")

    # Day 1: rest, day 2: rest
    expected_ctl2 = 40.0 * _DECAY_CTL * _DECAY_CTL
    expected_atl2 = 40.0 * _DECAY_ATL * _DECAY_ATL
    assert abs(ctl2 - expected_ctl2) < 1e-9, f"Rest-day zero-fill wrong: ctl={ctl2}, expected={expected_ctl2}"
    assert abs(atl2 - expected_atl2) < 1e-9


def test_tsb_is_morning_lag1():
    """
    TSB returned by _recompute(target) is the morning value of target_date
    (i.e. EoD of the previous day), NOT EoD of target. Verify the lag-1 invariant:
      tsb(target) == round(ctl(target-1) - atl(target-1))
    """
    state = _make_state(
        "2026-04-12", 40.0, 40.0,
        [
            {"date": "2026-04-13", "load": 87.0},
            {"date": "2026-04-15", "load": 55.0},
            {"date": "2026-04-17", "load": 120.0},
        ],
    )

    ctl17, atl17, tsb17 = _recompute(state, "2026-04-17")
    ctl16, atl16, _    = _recompute(state, "2026-04-16")

    # TSB for April 17 must equal morning of April 17 = EoD of April 16
    assert tsb17 == round(ctl16 - atl16), (
        f"TSB lag-1 failed: tsb17={tsb17}, round(ctl16-atl16)={round(ctl16 - atl16)}"
    )
    assert ctl17 > 0 and atl17 > 0
