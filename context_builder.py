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
# Main entry point
# ---------------------------------------------------------------------------

def build_context(db: dict[str, Any], today: str) -> str:
    """Return a fully formatted context string to prepend to the Claude prompt."""
    from training_load import get_training_load

    metrics = db.get("metrics") or {}
    activities = db.get("activities") or {}
    profile = load_athlete_profile()
    tl = get_training_load()
    today_d = date.fromisoformat(today)
    today_m = metrics.get(today) or {}

    lines: list[str] = []

    # ── Athlete profile ──────────────────────────────────────────────────────
    hz = profile.get("hr_zones", DEFAULT_PROFILE["hr_zones"])
    lines += [
        "=== ATHLETE PROFILE ===",
        f"Name: {profile.get('name', 'Raviv')} | Age: {profile.get('age', 60)} | Sex: {profile.get('sex', 'male').title()}",
        f"Sport: {profile.get('sport', 'treadmill running')}",
        f"Max HR: {profile.get('max_hr_bpm', 165)} bpm",
        (
            f"HR zones: Z1 <{hz.get('z1_max', 128)}, "
            f"Z2 {hz.get('z2_min', 128)}–{hz.get('z2_max', 140)}, "
            f"Z3 {hz.get('z3_min', 140)}–{hz.get('z3_max', 150)}, "
            f"Z4 {hz.get('z4_min', 150)}–{hz.get('z4_max', 158)}, "
            f"Z5 >{hz.get('z5_min', 158)} bpm"
        ),
        f"Training frequency: min {profile.get('training_frequency_min_per_week', 3)}×/week",
    ]
    if profile.get("notes"):
        lines.append(f"Notes: {profile['notes']}")

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

    # ── Training load ────────────────────────────────────────────────────────
    ctl = tl.get("ctl")
    atl = tl.get("atl")
    tsb = tl.get("tsb")
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
        lines.append(f"{'Date':<12} {'Sport':<14} {'Dist':>7} {'Dur':>7} {'AvgHR':>6} {'MaxHR':>6} {'TSS':>6}")
        lines.append("─" * 62)
        for day, a in recent[:28]:
            sport = (a.get("sport") or "—")[:13]
            dist_km = a.get("distance_km")
            dist = f"{dist_km:.1f}km" if dist_km else "—"
            dur_min = a.get("duration_min")
            dur = f"{int(dur_min)}min" if dur_min else "—"
            avg_hr = f"{int(a['avg_hr'])}" if a.get("avg_hr") else "—"
            max_hr = f"{int(a['max_hr'])}" if a.get("max_hr") else "—"
            tss = a.get("training_stress_score") or a.get("tss")
            tss_s = f"{float(tss):.0f}" if tss is not None else "—"
            lines.append(
                f"{day:<12} {sport:<14} {dist:>7} {dur:>7} {avg_hr:>6} {max_hr:>6} {tss_s:>6}"
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
