"""
Assembles the full training context string prepended to every morning briefing prompt.

GCS objects used:
  athlete_profile.json       — editable athlete config
  briefings/YYYY-MM-DD.txt   — one plain-text file per day's briefing output
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

GCS_BUCKET = os.environ.get("GCS_BUCKET", "running-coach-data-uplifted")
_PROFILE_OBJECT = "athlete_profile.json"

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "Raviv",
    "age": 60,
    "sex": "male",
    "sport": "treadmill running",
    "threshold_pace_kmh": 8.5,
    "goal": "run 10km in 60 minutes",
    "goal_pace_kmh": 10.0,
    "training_frequency_min_per_week": 3,
    "max_hr_bpm": 165,
    "hr_zones": {
        "z1_max": 128,
        "z2_min": 128, "z2_max": 140,
        "z3_min": 140, "z3_max": 150,
        "z4_min": 150, "z4_max": 158,
        "z5_min": 158,
    },
    "notes": "Treadmill runner. Cardiac drift 6-10 bpm over 40 min. No injury history.",
}


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def load_athlete_profile() -> dict[str, Any]:
    try:
        blob = _gcs_client().bucket(GCS_BUCKET).blob(_PROFILE_OBJECT)
        if not blob.exists():
            return DEFAULT_PROFILE.copy()
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("context_builder: could not load athlete_profile.json: %s", e)
        return DEFAULT_PROFILE.copy()


def save_athlete_profile(profile: dict[str, Any]) -> None:
    _gcs_client().bucket(GCS_BUCKET).blob(_PROFILE_OBJECT).upload_from_string(
        json.dumps(profile, indent=2, ensure_ascii=False),
        content_type="application/json",
    )
    logger.info("context_builder: saved athlete_profile.json")


def load_briefing(date_str: str) -> str | None:
    try:
        blob = _gcs_client().bucket(GCS_BUCKET).blob(f"briefings/{date_str}.txt")
        if not blob.exists():
            return None
        return blob.download_as_text(encoding="utf-8")
    except Exception as e:
        logger.warning("context_builder: could not load briefing %s: %s", date_str, e)
        return None


def load_plan() -> dict:
    try:
        blob = _gcs_client().bucket(GCS_BUCKET).blob("plan.json")
        if not blob.exists():
            return {}
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("context_builder: could not load plan.json: %s", e)
        return {}


def save_plan(plan: dict) -> None:
    _gcs_client().bucket(GCS_BUCKET).blob("plan.json").upload_from_string(
        json.dumps(plan, indent=2, ensure_ascii=False),
        content_type="application/json",
    )
    logger.info("context_builder: saved plan.json")


def save_briefing(date_str: str, text: str) -> None:
    _gcs_client().bucket(GCS_BUCKET).blob(f"briefings/{date_str}.txt").upload_from_string(
        text, content_type="text/plain; charset=utf-8",
    )
    logger.info("context_builder: saved briefings/%s.txt", date_str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tsb_label(tsb: float | int | None) -> str:
    if tsb is None:
        return "unknown"
    if tsb > 5:
        return "fresh / supercompensation window"
    if tsb >= 0:
        return "neutral"
    if tsb >= -5:
        return "productive training zone"
    return "fatigued — recovery needed"


def _hrv_trend(metrics: dict, today: str, days: int = 7) -> str:
    today_d = date.fromisoformat(today)
    vals = []
    for i in range(days):
        v = metrics.get((today_d - timedelta(days=i)).isoformat(), {}).get("hrv_last")
        if v is not None:
            vals.append(float(v))
    if len(vals) < 4:
        return "insufficient data"
    half = len(vals) // 2
    recent_avg = sum(vals[:half]) / half
    older_avg = sum(vals[half:]) / (len(vals) - half)
    diff = recent_avg - older_avg
    if diff > 2:
        return "improving"
    if diff < -2:
        return "declining"
    return "stable"


def _weeks_to_goal(metrics: dict, today: str, profile: dict) -> str:
    """Rough estimate based on CTL progression over the last 4 weeks."""
    today_d = date.fromisoformat(today)
    ctl_now = metrics.get(today, {}).get("ctl")
    ctl_4w = metrics.get((today_d - timedelta(weeks=4)).isoformat(), {}).get("ctl")
    current = float(profile.get("threshold_pace_kmh", 8.5))
    goal = float(profile.get("goal_pace_kmh", 10.0))
    gap = goal - current
    if gap <= 0:
        return "goal already achieved"
    if ctl_now is None or ctl_4w is None or float(ctl_now) <= float(ctl_4w):
        return "unable to estimate (no recent CTL progression)"
    ctl_gain_per_week = (float(ctl_now) - float(ctl_4w)) / 4
    # heuristic: 0.1 km/h pace gain per 5 CTL points
    pace_per_week = ctl_gain_per_week * (0.1 / 5)
    if pace_per_week <= 0:
        return "unable to estimate"
    weeks = round(gap / pace_per_week)
    return f"~{weeks} weeks at current progression rate"


# ---------------------------------------------------------------------------
# Plan resolution — plan.json is the source of truth for "what is planned on X"
# ---------------------------------------------------------------------------

def _trek_window(plan: dict) -> tuple[date, date] | None:
    """(start, end) of the trek, from trek_mode if present else the trek block."""
    trek = plan.get("trek") or {}
    mode = plan.get("trek_mode") or {}
    start = mode.get("start_date") or trek.get("start_date")
    end = mode.get("end_date") or trek.get("end_date")
    if not start or not end:
        return None
    try:
        return date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return None


def in_trek_window(plan: dict, day: str | date) -> bool:
    """True if the given date falls inside the trek, where degraded data is expected."""
    window = _trek_window(plan)
    if not window:
        return False
    try:
        d = date.fromisoformat(day) if isinstance(day, str) else day
    except ValueError:
        return False
    return window[0] <= d <= window[1]


def plan_entry(plan: dict, day: str) -> dict | None:
    """What is planned on an ISO date: a trek stage, a travel day, or a session."""
    stage = ((plan.get("trek") or {}).get("stages") or {}).get(day)
    if stage:
        return {"kind": "trek_stage", "stage": stage}
    session = (plan.get("sessions") or {}).get(day)
    if session:
        return {"kind": "travel" if session.get("travel") else "session", "session": session}
    return None


def _phase_covers(phase: dict, day: date) -> bool:
    try:
        return date.fromisoformat(phase["start_date"]) <= day <= date.fromisoformat(phase["end_date"])
    except (KeyError, TypeError, ValueError):
        return False


def _active_phase(plan: dict, day: date) -> dict:
    """The phase whose window contains `day`.

    Falls back to current_phase only when it actually covers the day, or when it
    carries no dates at all (legacy plans). Returns {} once every phase has expired —
    claiming an expired phase is still active is worse than admitting there is none.
    """
    for phase in plan.get("phases") or []:
        if _phase_covers(phase, day):
            return phase

    current = plan.get("current_phase") or {}
    if not current:
        return {}
    if not current.get("start_date") and not current.get("end_date"):
        return current
    return current if _phase_covers(current, day) else {}


def _stage_headline(stage: dict) -> str:
    bits = [f"Day {stage.get('day', '?')}", f"{stage.get('from', '?')} → {stage.get('to', '?')}"]
    if stage.get("distance_km") is not None:
        bits.append(f"{stage['distance_km']} km")
    if stage.get("ascent_m") is not None:
        bits.append(f"+{stage['ascent_m']} m")
    if stage.get("descent_m") is not None:
        bits.append(f"-{stage['descent_m']} m")
    if stage.get("duration"):
        bits.append(str(stage["duration"]))
    return " | ".join(bits)


def _stage_detail_lines(stage: dict) -> list[str]:
    profile = f"Distance: {stage.get('distance_km', '?')} km"
    profile += (
        f" | Ascent: +{stage['ascent_m']} m" if stage.get("ascent_m") is not None
        else " | Ascent: not specified in the itinerary"
    )
    if stage.get("descent_m") is not None:
        profile += f" | Descent: -{stage['descent_m']} m"
    profile += f" | Time on feet: {stage.get('duration', '?')}"

    start = f"Start: {stage.get('start', '?')}"
    if stage.get("dawn_start"):
        start += "  ** DAWN START REQUIRED — high pass; be over the top before afternoon storms build **"

    out = [
        f"Day {stage.get('day', '?')}: {stage.get('from', '?')} → {stage.get('to', '?')}",
        profile,
        start,
        f"Key hazard: {stage.get('hazard', '—')}",
    ]
    if stage.get("notes"):
        out.append(f"Notes: {stage['notes']}")
    if stage.get("estimated_tss") is not None:
        out.append(
            f"Planned load: ~{stage['estimated_tss']} TSS — PLANNING ESTIMATE from the itinerary, "
            "not a measured value. Use it as the stand-in for the day's load when no FIT file arrives, "
            "and never present it as recorded TSS."
        )
    return out


def recorded_stage_load(db: dict, day: str) -> tuple[bool, float | None]:
    """(activity_logged, total_TSS) for a date, from the stored activity log.

    Used to tell a morning briefing (stage still ahead) from an evening one
    (stage done and uploaded), and to spot stages whose FIT has not landed yet.
    """
    acts = (db.get("activities") or {}).get(day) or []
    if not acts:
        return False, None
    total = None
    for a in acts:
        v = a.get("training_stress_score")
        if v is None:
            v = a.get("tss")
        if v is None:
            v = a.get("suunto_tss")
        if v is not None:
            total = (total or 0.0) + float(v)
    return True, total


def _latest_value_date(metrics: dict, field: str, today: str) -> str | None:
    """Most recent date up to and including `today` that has a value for `field`."""
    for d in sorted((k for k in metrics if k <= today), reverse=True):
        if (metrics.get(d) or {}).get(field) is not None:
            return d
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_context(db: dict[str, Any], today: str) -> str:
    """Return a fully formatted context string to prepend to the Claude prompt."""
    metrics = db.get("metrics") or {}
    activities = db.get("activities") or {}
    profile = load_athlete_profile()
    today_d = date.fromisoformat(today)
    today_m = metrics.get(today) or {}

    lines: list[str] = []

    # ── Athlete profile ──────────────────────────────────────────────────────
    hz = profile.get("hr_zones", DEFAULT_PROFILE["hr_zones"])

    def _zone_str(key: str, lo: int, hi: int) -> str:
        """Format a zone from either array [lo,hi] or legacy scalar keys."""
        z = hz.get(key)
        if isinstance(z, (list, tuple)) and len(z) == 2:
            lo2, hi2 = int(z[0]), int(z[1])
            return f"<{hi2}" if lo2 == 0 else (f">{lo2}" if hi2 >= 900 else f"{lo2}–{hi2}")
        # legacy flat-key format
        return f"{lo}–{hi}"

    hz_text = (
        f"Z1 {_zone_str('Z1', 0, 128)}, "
        f"Z2 {_zone_str('Z2', 128, 140)}, "
        f"Z3 {_zone_str('Z3', 140, 150)}, "
        f"Z4 {_zone_str('Z4', 150, 158)}, "
        f"Z5 {_zone_str('Z5', 158, 999)} bpm"
    )

    freq = profile.get("training_days_per_week") or profile.get("training_frequency_min_per_week", 3)
    max_hr = profile.get("max_hr") or profile.get("max_hr_bpm", 163)
    lines += [
        "=== ATHLETE PROFILE ===",
        f"Name: {profile.get('name', 'Raviv')} | Age: {profile.get('age', 60)} | Gender: {profile.get('gender', profile.get('sex', 'male')).title()}",
        f"Sport: {profile.get('sport', 'treadmill running')}",
        f"Max HR: {max_hr} bpm",
        f"HR zones: {hz_text}",
        f"Training frequency: {freq}×/week",
    ]
    if profile.get("notes"):
        lines.append(f"Notes: {profile['notes']}")

    weekly = profile.get("weekly_structure", [])
    if weekly:
        lines.append("Weekly session structure:")
        for s in weekly:
            lines.append(f"  Session {s.get('session', '?')} — {s.get('type', '')}: {s.get('description', '')}")

    # ── Progress toward goal ─────────────────────────────────────────────────
    current_pace = float(profile.get("threshold_pace_kmh", 8.5))
    goal_pace = float(profile.get("goal_pace_kmh", 10.0))
    gap = round(goal_pace - current_pace, 2)
    lines += [
        "",
        "=== PROGRESS TOWARD GOAL ===",
        f"Goal: {profile.get('goal', 'run 10km in 60 minutes')}",
        f"Current threshold pace: {current_pace} km/h  |  Target: {goal_pace} km/h  |  Gap: {gap} km/h",
        f"Estimated time to goal: {_weeks_to_goal(metrics, today, profile)}",
    ]

    # ── Training load — read from metrics[today] (same source as dashboard tiles)
    ctl = today_m.get("ctl")
    atl = today_m.get("atl")
    tsb = today_m.get("tsb")
    ac_ratio = today_m.get("ac_ratio")
    lines += [
        "",
        "=== TRAINING LOAD ===",
        f"CTL (fitness):  {ctl if ctl is not None else '—'}",
        f"ATL (fatigue):  {atl if atl is not None else '—'}",
        f"TSB (form):     {tsb if tsb is not None else '—'}  →  {_tsb_label(tsb)}",
        f"  TSB guide: >5 = fresh/supercompensation, 0–5 = neutral, -5–0 = productive, <-5 = fatigued",
        f"A:C ratio:      {round(float(ac_ratio), 2) if ac_ratio is not None else '—'}",
    ]
    if ctl is None and atl is None and tsb is None:
        last_d = _latest_value_date(metrics, "ctl", today)
        if last_d:
            age = (today_d - date.fromisoformat(last_d)).days
            last_m = metrics.get(last_d) or {}
            lines.append(
                f"NO TRAINING-LOAD FIGURES FOR TODAY. Most recent known values are from {last_d} "
                f"({age} day(s) ago): CTL {last_m.get('ctl')}, ATL {last_m.get('atl')}, TSB {last_m.get('tsb')}. "
                "State this staleness plainly in one line and reason from the plan — do not invent current numbers."
            )
        else:
            lines.append(
                "NO TRAINING-LOAD FIGURES AVAILABLE AT ALL. Say so plainly and give plan-based guidance only."
            )

    # ── Last 4 weeks of activities ───────────────────────────────────────────
    cutoff = (today_d - timedelta(weeks=4)).isoformat()
    recent: list[tuple[str, dict]] = []
    for day, day_acts in activities.items():
        if day < cutoff:
            continue
        for a in day_acts:
            recent.append((day, a))
    recent.sort(key=lambda x: x[0], reverse=True)

    lines += ["", "=== ACTIVITIES — LAST 4 WEEKS (newest first) ==="]
    if not recent:
        lines.append("No activities recorded.")
    else:
        lines.append(f"{'Date':<12} {'Label':<14} {'Sport':<12} {'Dist':>7} {'Dur':>7} {'AvgHR':>6} {'MaxHR':>6} {'TSS':>6}")
        lines.append("─" * 72)
        for day, a in recent[:28]:
            label = (a.get("label") or "")[:13]
            sport = (a.get("sport") or "—")[:11]
            dist_km = a.get("distance_km")
            dist = f"{dist_km:.1f}km" if dist_km else "—"
            dur_min = a.get("duration_min")
            dur = f"{int(dur_min)}min" if dur_min else "—"
            avg_hr = f"{int(a['avg_hr'])}" if a.get("avg_hr") else "—"
            max_hr = f"{int(a['max_hr'])}" if a.get("max_hr") else "—"
            tss = a.get("training_stress_score") or a.get("tss")
            tss_s = f"{float(tss):.0f}" if tss is not None else "—"
            lines.append(
                f"{day:<12} {label:<14} {sport:<12} {dist:>7} {dur:>7} {avg_hr:>6} {max_hr:>6} {tss_s:>6}"
            )

    # ── Biometrics — last 7 days ─────────────────────────────────────────────
    hrv_trend = _hrv_trend(metrics, today)
    lines += ["", "=== BIOMETRICS — LAST 7 DAYS ==="]
    lines.append(f"{'Date':<12} {'HRV':>5} {'RestHR':>7} {'Sleep':>8} {'SleepQ':>7} {'Recovery':>9}")
    lines.append("─" * 55)
    for i in range(7):
        d = (today_d - timedelta(days=i)).isoformat()
        m = metrics.get(d) or {}
        hrv = m.get("hrv_last")
        rhr = m.get("resting_hr")
        sleep_min = m.get("sleep_duration_min")
        sleep_q = m.get("sleep_quality")
        rec = m.get("recovery_score")
        hrv_s = f"{float(hrv):.0f}" if hrv is not None else "—"
        rhr_s = f"{int(rhr)}" if rhr is not None else "—"
        if sleep_min is not None:
            h, mn = divmod(int(sleep_min), 60)
            sleep_s = f"{h}h{mn:02d}m"
        else:
            sleep_s = "—"
        sleepq_s = f"{float(sleep_q):.0f}%" if sleep_q is not None else "—"
        rec_s = f"{int(rec)}%" if rec is not None else "—"
        lines.append(f"{d:<12} {hrv_s:>5} {rhr_s:>7} {sleep_s:>8} {sleepq_s:>7} {rec_s:>9}")
    lines.append(f"HRV 7d trend: {hrv_trend}")
    if not any(today_m.get(f) is not None for f in ("hrv_last", "resting_hr", "sleep_duration_min")):
        lines.append(
            "NO BIOMETRICS RECORDED FOR TODAY — the watch has not synced. Acknowledge this plainly "
            "in one line; do not infer, estimate or invent values to fill the gap."
        )

    # ── Plan: trek, phase, and the dated itinerary ───────────────────────────
    # plan.json is the SOURCE OF TRUTH for what is scheduled on a given date.
    plan = load_plan()
    trek = plan.get("trek") or {}
    phase = _active_phase(plan, today_d)
    stages = trek.get("stages") or {}
    tomorrow = (today_d + timedelta(days=1)).isoformat()
    on_trek = in_trek_window(plan, today_d)

    if trek:
        trek_start = trek.get("start_date", "?")
        days_to_trek = (date.fromisoformat(trek_start) - today_d).days if trek_start != "?" else "?"
        lines += [
            "",
            "=== UPCOMING GOAL: TREK ===",
            f"Event: {trek.get('name', '?')} | Start: {trek_start} ({days_to_trek} days away) | End: {trek.get('end_date', '?')}",
            f"Distance: ~{trek.get('distance_km', '?')} km | Total ascent: ~{trek.get('ascent_m', '?')} m | Duration: {trek.get('duration_days', '?')} days",
        ]
        if trek.get("base"):
            lines.append(f"Base: {trek['base']}")
        if trek.get("nutrition"):
            lines.append(f"Trek nutrition: {trek['nutrition']}")
        if trek.get("notes"):
            lines.append(f"Notes: {trek['notes']}")

    if phase:
        lines += [
            "",
            "=== CURRENT TRAINING PHASE ===",
            f"Phase: {phase.get('name', '?')}",
            f"Period: {phase.get('start_date', '?')} → {phase.get('end_date', '?')}",
            f"Focus: {phase.get('focus', '?')}",
        ]
        if phase.get("nutrition"):
            lines.append(f"Nutrition: {phase['nutrition']}")

    # ── The dated itinerary — next 7 days ────────────────────────────────────
    lines += [
        "",
        "=== PLANNED — NEXT 7 DAYS ===",
        "This itinerary is the SOURCE OF TRUTH for what is scheduled on each date. "
        "Do not invent, substitute or upgrade a session that is not listed here.",
    ]
    for i in range(7):
        d = (today_d + timedelta(days=i)).isoformat()
        if i == 0:
            label = f"TODAY ({d})"
        elif i == 1:
            label = f"TOMORROW ({d})"
        else:
            label = d
        entry = plan_entry(plan, d)
        if not entry:
            lines.append(f"  {label}: (no planned session)")
        elif entry["kind"] == "trek_stage":
            lines.append(f"  {label}: [TREK STAGE] {_stage_headline(entry['stage'])}")
        elif entry["kind"] == "travel":
            s = entry["session"]
            lines.append(f"  {label}: [TRAVEL DAY — NO TRAINING] {s.get('detail', '')}")
        else:
            s = entry["session"]
            optional = " (OPTIONAL — skipping is a valid choice)" if s.get("optional") else ""
            lines.append(
                f"  {label}: [{s.get('type', '?')}] {s.get('detail', '')}{optional}"
                f"  [intensity: {s.get('intensity', '?')}]"
            )

    lines.append(
        "INTENSITY NOTE: Hill walks and pack walks are LOW intensity. "
        "Apply HRV/TSB recovery gates only to HIGH-intensity sessions (threshold runs, intervals). "
        "Do not block low-intensity sessions with the same thresholds as hard runs."
    )
    # Only surface the taper rules while the taper is actually the active phase.
    if plan.get("taper_policy") and "taper" in str(phase.get("name", "")).lower():
        lines.append(f"TAPER POLICY: {plan['taper_policy']}")

    # ── Today's own stage / travel day ───────────────────────────────────────
    today_entry = plan_entry(plan, today)

    if today_entry and today_entry["kind"] == "trek_stage":
        stage_today = today_entry["stage"]
        stage_done, stage_tss = recorded_stage_load(db, today)
        header = (
            "=== TODAY'S TREK STAGE — ALREADY COMPLETED AND UPLOADED ==="
            if stage_done else "=== TODAY'S TREK STAGE ==="
        )
        lines += ["", header] + _stage_detail_lines(stage_today)

        if stage_done:
            planned = stage_today.get("estimated_tss")
            if stage_tss is not None:
                cmp_note = f" (plan expected ~{planned} TSS)" if planned is not None else ""
                lines.append(
                    f"RECORDED: this stage is already in the log at {stage_tss:.0f} TSS{cmp_note}."
                )
            else:
                lines.append("RECORDED: this stage is already in the log (no TSS value attached yet).")
            lines.append(
                "This is an EVENING briefing, generated after Raviv uploaded the stage from the refuge. "
                "Do NOT prescribe today's stage as if it were still ahead of him — he has walked it. "
                "Replace Option A / Option B with: (a) a short debrief of the stage just completed, "
                "comparing recorded load against plan and saying what it means for tomorrow, and "
                "(b) tonight's actions at the refuge — refuel, rehydrate, salt, leg care, kit and water "
                "prep for the morning, and a target lights-out time that supports tomorrow's start."
            )
        else:
            lines.append(
                "Today's session IS this stage. Option A and Option B must describe how to execute it — "
                "pacing, fuelling, hydration and hazard management on the mountain — not a treadmill or gym session."
            )

    if today_entry and today_entry["kind"] == "travel":
        s = today_entry["session"]
        lines += ["", "=== TODAY IS A TRAVEL DAY — NO TRAINING ===", s.get("detail", "")]
        if s.get("guidance"):
            lines.append(s["guidance"])
        lines.append(
            "Do NOT prescribe a training session today, not even a shakeout. Replace Option A / Option B "
            "with travel and recovery guidance: hydration through the flight and drive, carb-loading and "
            "meal timing, sleep protection, and light movement/stretching at the stops."
        )

    # ── Tomorrow's stage — this is what the outlook section must brief ───────
    tomorrow_stage = stages.get(tomorrow)
    if tomorrow_stage:
        lines += [
            "",
            "=== TOMORROW'S TREK STAGE — BRIEF THIS IN 'Next session outlook' ===",
        ] + _stage_detail_lines(tomorrow_stage)
        lines.append(
            "The 'Next session outlook' section MUST brief this stage: distance, ascent, expected time on "
            "feet and the key hazard, plus concrete fuel, pacing, hydration and start-time guidance for it. "
            "Fuel = EAT at maintenance-plus; never recommend a calorie deficit during the trek. "
            "If the stage is flagged DAWN START, say so explicitly and give a wake/depart time."
        )

    # ── Rising ATL / falling TSB is the planned shape of the event ──────────
    if stages and (on_trek or tomorrow in stages):
        window = f"{trek.get('start_date', '?')} → {trek.get('end_date', '?')}"
        lines += [
            "",
            "=== TREK LOAD FRAMING — RISING ATL / FALLING TSB IS EXPECTED ===",
            f"This is a KNOWN, PLANNED {trek.get('duration_days', 6)}-day event ({window}). Consecutive "
            "loaded days mean ATL will RISE steeply and TSB will FALL well negative across the trek. "
            "That is the intended shape of the event, not a red flag.",
            "Do NOT issue fatigue, overreaching or 'A:C ratio spike' warnings, and do not turn a traffic "
            "light Amber or Red, merely because ATL climbs or TSB goes deeply negative on these dates. "
            "Do not advise cutting the trek short, skipping a stage, or 'backing off the load' on the "
            "strength of the numbers alone — the itinerary is fixed and these values are the plan working.",
            "Judge the traffic lights against the STAGE AHEAD — can he safely start and complete tomorrow's "
            "stage? — not against a training-block baseline. Reserve Amber/Red for genuine warning signs: "
            "injury or joint pain, illness or GI trouble, a sharp HRV collapse WITH symptoms, badly broken "
            "sleep, dehydration, or a stage he realistically cannot complete safely.",
        ]
        running = 0.0
        progression: list[str] = []
        for d in sorted(stages):
            est = stages[d].get("estimated_tss")
            if est is None:
                continue
            running += float(est)
            progression.append(f"{d} D{stages[d].get('day', '?')} +{est:.0f} → {running:.0f} cum")
        if progression:
            lines.append(
                "Planned cumulative load (PLANNING ESTIMATES from the itinerary, not measured TSS): "
                + " | ".join(progression)
            )

    # ── Which stages have actually reached the load model ───────────────────
    stages_to_date = sorted(d for d in stages if d <= today)
    if stages_to_date and (on_trek or tomorrow in stages):
        lines += ["", "=== TREK STAGE UPLOAD LEDGER ==="]
        awaiting: list[str] = []
        for d in stages_to_date:
            st = stages[d]
            logged, tss = recorded_stage_load(db, d)
            planned = st.get("estimated_tss")
            plan_txt = f"~{planned} TSS" if planned is not None else "not estimated"
            if logged and tss is not None:
                lines.append(
                    f"  {d} Day {st.get('day', '?')}: FIT RECEIVED — recorded {tss:.0f} TSS (plan expected {plan_txt})"
                )
            elif logged:
                lines.append(
                    f"  {d} Day {st.get('day', '?')}: activity logged, no TSS attached yet (plan expects {plan_txt})"
                )
            elif d == today:
                # Today's stage simply has not been walked yet — not a missing upload.
                lines.append(
                    f"  {d} Day {st.get('day', '?')}: TODAY — not yet walked/uploaded, expected load {plan_txt}"
                )
            else:
                awaiting.append(d)
                lines.append(
                    f"  {d} Day {st.get('day', '?')}: AWAITING UPLOAD — expected load {plan_txt} "
                    "(plan placeholder, NOT a recorded value)"
                )
        if awaiting:
            lines.append(
                "The stages marked AWAITING UPLOAD have not reached the load model yet. Raviv uploads from "
                "the refuge in the evening and connectivity is intermittent, so a late or batched upload "
                "(two stages arriving together the next evening) is normal and expected. This is NOT "
                "missing, unprocessed or faulty data — do not report it as a warning, a problem, or "
                "something for him to fix. Use the plan's expected load as the placeholder for those days, "
                "and state plainly, in one line, that CTL/ATL/TSB currently UNDERSTATE his true fatigue "
                "until those files land. Never describe an un-uploaded stage as a rest day or as zero load."
            )

    # ── Trek mode — degraded data is expected, not a fault ───────────────────
    if on_trek:
        mode = plan.get("trek_mode") or {}
        lines += ["", "=== TREK MODE ACTIVE — DEGRADED DATA IS EXPECTED ==="]
        if mode.get("data_policy"):
            lines.append(mode["data_policy"])
        if mode.get("briefing_policy"):
            lines.append(mode["briefing_policy"])
        if trek.get("connectivity"):
            lines.append(f"Connectivity: {trek['connectivity']}")

    # ── Last 3 morning briefings ─────────────────────────────────────────────
    lines += ["", "=== LAST 3 MORNING BRIEFINGS ==="]
    found = 0
    for i in range(1, 14):
        if found >= 3:
            break
        d = (today_d - timedelta(days=i)).isoformat()
        text = load_briefing(d)
        if not text:
            # fall back to in-memory briefings stored in metrics.json
            text = (db.get("briefings") or {}).get(d, {}).get("markdown")
        if text:
            lines += [f"--- {d} ---", text.strip(), ""]
            found += 1
    if found == 0:
        lines.append("No previous briefings available.")

    return "\n".join(lines)
