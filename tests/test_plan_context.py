"""
Tests for plan resolution and the trek briefing context.

Covers plan.json as data (every planned date resolves), the resolvers in
context_builder, and the trek behaviours: load framing, evening briefings,
and graceful handling of late/batched stage uploads.

No GCS credentials required — the GCS loaders are stubbed.

Run with: python -m pytest tests/test_plan_context.py -v
      or: python tests/test_plan_context.py     (no pytest needed)
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import context_builder as cb
from context_builder import (
    _active_phase,
    build_context,
    in_trek_window,
    plan_entry,
    recorded_stage_load,
)
from training_load import _recompute

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
PLAN = json.load(open(os.path.join(REPO_ROOT, "plan.json"), encoding="utf-8"))

TAPER_DAYS = [f"2026-08-{d:02d}" for d in range(16, 27)]
TRAVEL_DAY = "2026-08-27"
TREK_DAYS = ["2026-08-28", "2026-08-29", "2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stage_activity(tss, minutes=270):
    return [{"sport": "hiking", "duration_min": minutes, "training_stress_score": tss}]


def _context(day, activities=None, metrics=None):
    """Build the real context string with the GCS loaders stubbed out."""
    cb.load_plan = lambda: PLAN
    cb.load_athlete_profile = lambda: cb.DEFAULT_PROFILE.copy()
    cb.load_briefing = lambda _d: None
    db = {
        "meta": {},
        "metrics": metrics if metrics is not None else {day: {"ctl": 45.0, "atl": 78.0, "tsb": -33.0}},
        "activities": activities or {},
    }
    return build_context(db, day)


# ---------------------------------------------------------------------------
# plan.json as data
# ---------------------------------------------------------------------------

def test_plan_is_versioned():
    # The deploy-time sync only supersedes the stored plan on a higher version.
    assert isinstance(PLAN.get("version"), int)
    assert PLAN["version"] >= 2


def test_every_planned_date_resolves():
    for day in TAPER_DAYS + [TRAVEL_DAY] + TREK_DAYS:
        assert plan_entry(PLAN, day) is not None, f"{day} has no plan entry"


def test_travel_day_is_flagged_as_travel_not_a_session():
    entry = plan_entry(PLAN, TRAVEL_DAY)
    assert entry["kind"] == "travel"
    assert entry["session"]["intensity"] == "none"


def test_taper_days_are_rest_or_low_intensity_only():
    for day in TAPER_DAYS:
        entry = plan_entry(PLAN, day)
        assert entry["kind"] == "session"
        assert entry["session"]["intensity"] in ("none", "low"), f"{day} is not a taper-grade session"


def test_all_six_stages_present_with_required_fields():
    stages = PLAN["trek"]["stages"]
    assert sorted(stages) == TREK_DAYS
    for day, stage in stages.items():
        for field in ("day", "from", "to", "distance_km", "duration", "hazard", "estimated_tss"):
            assert stage.get(field) is not None, f"{day} missing {field}"


def test_dawn_starts_flagged_on_the_two_high_pass_days():
    stages = PLAN["trek"]["stages"]
    dawn = {d for d, s in stages.items() if s.get("dawn_start")}
    assert dawn == {"2026-08-30", "2026-08-31"}


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def test_plan_entry_kinds():
    assert plan_entry(PLAN, "2026-08-16")["kind"] == "session"
    assert plan_entry(PLAN, TRAVEL_DAY)["kind"] == "travel"
    assert plan_entry(PLAN, "2026-08-30")["kind"] == "trek_stage"
    assert plan_entry(PLAN, "2026-09-20") is None


def test_in_trek_window_boundaries():
    assert not in_trek_window(PLAN, "2026-08-27")
    assert in_trek_window(PLAN, "2026-08-28")
    assert in_trek_window(PLAN, "2026-09-02")
    assert not in_trek_window(PLAN, "2026-09-03")


def test_in_trek_window_is_safe_on_empty_or_malformed_plans():
    # Must not crash, and must not suppress warnings when the plan is unusable.
    assert not in_trek_window({}, "2026-08-30")
    assert not in_trek_window({"trek_mode": {"start_date": "x", "end_date": "y"}}, "2026-08-30")
    assert not in_trek_window(PLAN, "not-a-date")


def test_active_phase_tracks_the_date():
    def phase_name(day):
        return _active_phase(PLAN, date.fromisoformat(day)).get("name")

    assert phase_name("2026-08-10") == "Trek-prep block"
    assert phase_name("2026-08-16") == "Taper into Carros de Foc"
    assert phase_name(TRAVEL_DAY) == "Taper into Carros de Foc"
    assert phase_name("2026-08-28") == "Carros de Foc trek"


def test_active_phase_is_empty_once_every_phase_has_expired():
    # Regression: falling back to current_phase unconditionally claimed the taper
    # was still running after the trek had finished.
    assert _active_phase(PLAN, date.fromisoformat("2026-09-03")) == {}


def test_active_phase_keeps_legacy_undated_current_phase():
    legacy = {"current_phase": {"name": "Legacy block", "focus": "whatever"}}
    assert _active_phase(legacy, date.fromisoformat("2026-08-30"))["name"] == "Legacy block"


def test_recorded_stage_load():
    assert recorded_stage_load({"activities": {}}, "2026-08-30") == (False, None)

    db = {"activities": {"2026-08-30": _stage_activity(294)}}
    assert recorded_stage_load(db, "2026-08-30") == (True, 294.0)

    # Two activities on one day sum.
    db = {"activities": {"2026-08-30": _stage_activity(200) + _stage_activity(94)}}
    assert recorded_stage_load(db, "2026-08-30") == (True, 294.0)

    # Logged but no TSS attached yet.
    db = {"activities": {"2026-08-30": [{"duration_min": 400}]}}
    assert recorded_stage_load(db, "2026-08-30") == (True, None)


# ---------------------------------------------------------------------------
# Briefing context — taper and travel
# ---------------------------------------------------------------------------

def test_taper_day_states_the_planned_session_and_the_taper_policy():
    ctx = _context("2026-08-19")
    assert "TODAY (2026-08-19): [Easy pack walk]" in ctx
    assert "OPTIONAL — skipping is a valid choice" in ctx
    assert "TAPER POLICY:" in ctx
    assert "=== TODAY'S TREK STAGE ===" not in ctx


def test_taper_day_carries_no_trek_load_framing():
    # Rising-load framing belongs to the trek, not to the taper.
    assert "TREK LOAD FRAMING" not in _context("2026-08-20")


def test_taper_nutrition_is_maintenance_not_a_deficit():
    ctx = _context("2026-08-19")
    assert "Maintenance calories" in ctx
    assert "Fat-loss calorie deficit" not in ctx


def test_travel_day_forbids_training_and_previews_stage_one():
    ctx = _context(TRAVEL_DAY)
    assert "=== TODAY IS A TRAVEL DAY — NO TRAINING ===" in ctx
    assert "Do NOT prescribe a training session today" in ctx
    assert "TOMORROW'S TREK STAGE" in ctx
    assert "Day 1: Espot" in ctx


# ---------------------------------------------------------------------------
# Briefing context — trek
# ---------------------------------------------------------------------------

def test_trek_morning_prescribes_todays_stage():
    ctx = _context("2026-08-30", activities={"2026-08-29": _stage_activity(191)})
    assert "=== TODAY'S TREK STAGE ===" in ctx
    assert "ALREADY COMPLETED AND UPLOADED" not in ctx
    assert "Today's session IS this stage" in ctx


def test_trek_evening_debriefs_instead_of_prescribing():
    ctx = _context("2026-08-30", activities={"2026-08-30": _stage_activity(294)})
    assert "ALREADY COMPLETED AND UPLOADED" in ctx
    assert "already in the log at 294 TSS" in ctx
    assert "plan expected ~280 TSS" in ctx
    assert "he has walked it" in ctx
    assert "Today's session IS this stage" not in ctx


def test_tomorrows_stage_is_briefed_with_hazard_and_dawn_start():
    ctx = _context("2026-08-30", activities={"2026-08-30": _stage_activity(294)})
    assert "TOMORROW'S TREK STAGE" in ctx
    assert "Coll de Contraix" in ctx
    assert "DAWN START REQUIRED" in ctx


def test_trek_load_framing_blocks_fatigue_alarms():
    ctx = _context("2026-08-31")
    assert "TREK LOAD FRAMING" in ctx
    assert "ATL will RISE steeply and TSB will FALL well negative" in ctx
    assert "Do NOT issue fatigue, overreaching" in ctx
    assert "Planned cumulative load" in ctx


def test_trek_mode_suppresses_missing_data_complaints():
    ctx = _context("2026-08-30")
    assert "TREK MODE ACTIVE" in ctx
    assert "Do not warn about missing/unprocessed files" in ctx


# ---------------------------------------------------------------------------
# Briefing context — graceful gaps
# ---------------------------------------------------------------------------

def test_missing_prior_stage_is_a_placeholder_not_a_warning():
    # Day 3's FIT never arrived; briefing runs on Day 4.
    ctx = _context("2026-08-31", activities={
        "2026-08-28": _stage_activity(118),
        "2026-08-29": _stage_activity(191),
    })
    assert "2026-08-30 Day 3: AWAITING UPLOAD — expected load ~280 TSS" in ctx
    assert "NOT a recorded value" in ctx
    assert "UNDERSTATE his true fatigue" in ctx
    assert "Never describe an un-uploaded stage as a rest day or as zero load." in ctx


def test_todays_unwalked_stage_is_not_reported_as_a_missing_upload():
    ctx = _context("2026-08-31", activities={
        "2026-08-28": _stage_activity(118),
        "2026-08-29": _stage_activity(191),
        "2026-08-30": _stage_activity(294),
    })
    assert "2026-08-31 Day 4: TODAY — not yet walked/uploaded" in ctx
    assert "AWAITING UPLOAD" not in ctx


def test_ledger_reports_received_stages_against_plan():
    ctx = _context("2026-08-29", activities={"2026-08-28": _stage_activity(118)})
    assert "2026-08-28 Day 1: FIT RECEIVED — recorded 118 TSS (plan expected ~110 TSS)" in ctx


def test_absent_metrics_are_stated_plainly_not_invented():
    ctx = _context("2026-08-30", metrics={"2026-08-28": {"ctl": 42.8, "atl": 38.2, "tsb": 15}})
    assert "NO TRAINING-LOAD FIGURES FOR TODAY" in ctx
    assert "do not invent current numbers" in ctx


# ---------------------------------------------------------------------------
# Guard suppression — mirrors the filter applied in app.run_daily_pipeline
# ---------------------------------------------------------------------------

def test_gap_warnings_skip_trek_dates_only():
    unregistered = {"2026-08-19", "2026-08-29", "2026-08-31", "2026-09-05"}
    warned = sorted(d for d in unregistered if not in_trek_window(PLAN, d))
    assert warned == ["2026-08-19", "2026-09-05"]


# ---------------------------------------------------------------------------
# Load-pipeline invariants the trek behaviour relies on (computation untouched)
# ---------------------------------------------------------------------------

def _state(loads):
    return {
        "seed_date": "2026-08-27", "seed_ctl": 42.0, "seed_atl": 30.0,
        "activities": [{"date": d, "load": l} for d, l in loads],
    }


def test_batched_out_of_order_uploads_match_in_order_arrival():
    # Two stages uploaded together after a refuge with no signal must land on the
    # same CTL/ATL/TSB as if each had arrived on its own evening.
    in_order = [("2026-08-28", 118), ("2026-08-29", 191), ("2026-08-30", 294), ("2026-08-31", 172)]
    batched = [("2026-08-28", 118), ("2026-08-29", 191), ("2026-08-31", 172), ("2026-08-30", 294)]
    assert _recompute(_state(batched), "2026-08-31") == _recompute(_state(in_order), "2026-08-31")


def test_a_missing_stage_understates_fatigue():
    # Justifies the context telling the briefing to say so out loud.
    full = _recompute(_state([("2026-08-28", 118), ("2026-08-29", 191), ("2026-08-30", 294)]), "2026-08-30")
    gapped = _recompute(_state([("2026-08-28", 118), ("2026-08-29", 191)]), "2026-08-30")
    assert gapped[1] < full[1]


def test_atl_rises_across_the_trek():
    # The framing block exists because this is the expected shape.
    loads = [("2026-08-28", 118), ("2026-08-29", 191), ("2026-08-30", 294),
             ("2026-08-31", 172), ("2026-09-01", 140), ("2026-09-02", 265)]
    atls = [_recompute(_state(loads), d)[1] for d in TREK_DAYS]
    assert atls == sorted(atls)
    assert atls[-1] > atls[0] * 2


# ---------------------------------------------------------------------------
# Standalone runner — the project venv lives on iCloud and pip installs stall
# there, so these must also run without pytest.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e.__class__.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
