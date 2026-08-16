"""Claude prompt builder and Anthropic API call."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic


ATHLETE_PROFILE = """
Athlete: Raviv | Location: Israel
Long-term goal: Run 10 km in 60 minutes (10 km/h)
Current estimate: 10 km in ~73–77 min — gap of ~13–17 min to close
CURRENT PHASE / NUTRITION: see the CURRENT TRAINING PHASE section of the context — that is authoritative and changes over time. Do not assume a fat-loss deficit is still in effect.
GOAL EVENT: Carros de Foc circuit (6 days, ~60 km, ~3620 m ascent, starts 2026-08-28). During trek-prep and the trek itself, descending/climbing/time-on-feet outrank 10k speed development.
Training: 3 sessions/week — treadmill runs + hill/pack walks as trek prep (both count toward load)
Max HR: ~160 bpm | Aerobic base HR: 130–135 bpm | Threshold HR: 155–160 bpm
Cardiac drift: typically 6–10 bpm over 40 min (target: reduce below 5 bpm)
HRV range: 23–40 | Average: ~29–33 | High HRV = green light for quality work
Optimal TSB zone: -5 to 0 | Avoid stacking intensity when TSB < -10
Performs best when: HRV >= 30, TSB between -5 and 0, sleep quality high
Strengths: aerobic base, pacing discipline, consistency, data-driven approach
Weaknesses: speed endurance, moderate cardiac drift, threshold underdeveloped
Injury history: none
Typical run: 10 min warmup @ 6.3 km/h → 40 min @ 7.4 km/h → 10-15 min @ 8.0-8.2 km/h → 5 min cooldown
Development priorities: extend duration at 8.0-8.5 km/h, tempo runs, intervals at 9-10 km/h, reduce cardiac drift
Weekly structure: Easy run / Steady run / Quality run / Hill or pack walk
INTENSITY POLICY: HRV/TSB recovery gates apply to HIGH-intensity runs (threshold, intervals). Hill walks and pack walks are LOW intensity — never block them with the same HRV/TSB thresholds as hard runs. Check the planned session's intensity before applying any gate.
"""


def hr_zones(threshold_hr: int) -> dict[str, str]:
    """Zones from anaerobic threshold (e.g. 160 → Z1<128, Z2 128–144, Z3 144–152, Z4 152–160, Z5>160)."""
    th = float(threshold_hr)
    z1_hi = int(th * 0.80)
    z2_hi = int(th * 0.90)
    z3_hi = int(th * 0.95)
    return {
        "Z1": f"<{z1_hi} bpm",
        "Z2": f"{z1_hi}–{z2_hi} bpm",
        "Z3": f"{z2_hi}–{z3_hi} bpm",
        "Z4": f"{z3_hi}–{int(th)} bpm",
        "Z5": f">{int(th)} bpm",
    }


def build_prompt(db: dict[str, Any], today: str, context: str = "") -> str:
    meta = db.get("meta") or {}
    athlete = meta.get("athlete") or {}
    name = athlete.get("name", "Athlete")
    goal = athlete.get("goal", "10 km in 60 min")
    watch = athlete.get("watch", "")
    th = int(athlete.get("threshold_hr") or 160)
    zones = hr_zones(th)
    ztext = ", ".join(f"{k}: {v}" for k, v in zones.items())

    from datetime import date as _date
    today_d_local = _date.fromisoformat(today)
    today_weekday = today_d_local.strftime("%A")
    today_formatted = today_d_local.strftime("%-d %B %Y")  # e.g. "29 July 2026"

    m = (db.get("metrics") or {}).get(today) or {}
    metrics_sorted = sorted((db.get("metrics") or {}).keys())

    def collect(field: str, n: int) -> list[float | None]:
        out: list[float | None] = []
        for k in metrics_sorted[-n:]:
            v = (db.get("metrics") or {}).get(k, {}).get(field)
            out.append(float(v) if v is not None else None)
        return out

    h7 = collect("hrv_last", 7)
    h30 = collect("hrv_last", 30)
    slp7 = collect("sleep_duration_min", 7)
    slp30 = collect("sleep_duration_min", 30)
    rhr7 = collect("resting_hr", 7)
    ctl30 = collect("ctl", 30)
    atl30 = collect("atl", 30)
    tsb30 = collect("tsb", 30)

    est = m.get("estimated") or {}
    est_note = (
        f"Fields marked estimated (carried forward): {json.dumps(est)}"
        if est  # only True entries are stored; non-empty means something was carried forward
        else "No estimated carry-forward for today."
    )

    context_section = f"\n{context}\n" if context else ""
    prompt = f"""You are an expert running coach. Produce TODAY's training briefing for {name}.

BRIEFING DATE: {today_weekday}, {today_formatted} (ISO: {today}). This is the date of THIS briefing — do not add or subtract any days.
Start your response with EXACTLY this heading (copy it verbatim):
# Daily Briefing — {today_weekday}, {today_formatted}
{context_section}
## ATHLETE PROFILE
{ATHLETE_PROFILE}
Watch: {watch} | HR zones: {ztext}

Today is {today} ({today_weekday}).
Today's computed metrics ({today}):
{json.dumps(m, indent=2)}

Context — last 7 values (oldest→newest) for trends:
- HRV RMSSD: {h7}
- Sleep duration (min): {slp7}
- Resting HR: {rhr7}
- CTL: {collect('ctl', 7)}
- ATL: {collect('atl', 7)}
- TSB: {collect('tsb', 7)}

Context — last 30 values (oldest→newest):
- HRV (RMSSD): {h30}
- Sleep duration (min): {slp30}
- Resting HR: {collect('resting_hr', 30)}
- CTL: {ctl30}
- ATL: {atl30}
- TSB: {tsb30}

Data quality note: {est_note}

Output format (use these exact section headings in Markdown):

## Traffic lights
- Load / freshness / recovery (each: Green / Amber / Red with one line rationale)

## Limiting factor
One sentence.

## Conclusion
2–4 sentences tying metrics to today's decision.

## Option A (recommended)
Warm-up / main set / cool-down with:
- Pace targets in km/h AND approximate HR range using the zones above
- Keep total volume sensible for ~3×/week ~8 km habits unless clearly inappropriate

## Option B (fallback)
Easier alternative with pace (km/h) + HR range.

## What to avoid
Bullet list.

## Next session outlook
Short paragraph about the next planned session (check the PLANNED — NEXT 7 DAYS section in context for what's scheduled tomorrow).

PLAN AUTHORITY — these rules override the generic format above:
- The PLANNED — NEXT 7 DAYS section is the SOURCE OF TRUTH for what is scheduled on any date. Prescribe what is planned. Never invent, substitute or upgrade a session that is not listed there.
- TAPER DAYS: prescribe exactly the planned easy or rest session and nothing more. Do not add hill walks, pack walks, quality runs or intervals. Where the plan marks a session OPTIONAL, say plainly that skipping it is a valid choice. Freshness is the priority; nutrition is maintenance — no deficit.
- TRAVEL DAY: do not prescribe training of any kind. Replace Option A / Option B with travel and recovery guidance — hydration, carb-loading and meal timing, sleep protection, and light movement around the flight and drive.
- TREK DAYS: Option A / Option B describe how to execute TODAY'S STAGE on the mountain — pacing, fuelling, hydration, hazard management — not a treadmill or gym session. Fuel = EAT at maintenance-plus; never recommend a calorie deficit during the trek.
- TOMORROW'S STAGE: when the context contains a TOMORROW'S TREK STAGE block, the outlook must brief it — distance, ascent, expected time on feet, key hazard — with concrete fuel, pacing, hydration and start-time guidance. If the stage is flagged DAWN START, say so explicitly and give a wake/depart time.
- DEGRADED DATA DURING THE TREK is expected, not a fault. Do not raise missing-file, unprocessed-file or missing-data warnings for trek dates, do not treat an absent activity as a rest day, and do not zero-fill load. Stage load estimates from the itinerary are planning figures — use them as a stand-in, but never present them as measured TSS.
- If metrics are stale or absent, say so in one plain line and give plan-based guidance from the itinerary. Do not error out and do not invent numbers.
- TREK LOAD IS MEANT TO ACCUMULATE. Across the trek, ATL rises and TSB falls day on day. This is a known, planned multi-day event, so rising load is EXPECTED ACCUMULATION and must be framed that way. Do not raise fatigue, overreaching or A:C-ratio alarms, do not colour a traffic light Amber or Red, and do not suggest shortening the trek or skipping a stage, on account of climbing ATL or deeply negative TSB alone. Judge readiness against the stage ahead — can he safely start and complete it — and reserve Amber/Red for real warning signs: injury, illness, GI trouble, HRV collapse WITH symptoms, badly broken sleep, dehydration.
- EVENING BRIEFINGS: on the trek, Raviv uploads each stage's FIT in the evening from the refuge and regenerates the briefing on demand. When the context says today's stage is ALREADY COMPLETED AND UPLOADED, this is an evening briefing — debrief the stage he just walked (recorded load vs plan, what it implies for tomorrow) and give tonight's refuge recovery actions, then make TOMORROW's stage the main event. Never prescribe a stage he has already finished.
- LATE AND BATCHED UPLOADS ARE NORMAL. A refuge with no signal means a stage's FIT may arrive a day late, or two may arrive together. Stages listed as AWAITING UPLOAD are not missing, unprocessed or faulty data — never report them as a warning or as something to fix. Use the plan's expected stage load as the placeholder, never zero and never a rest day, and note in one line that CTL/ATL/TSB understate true fatigue until those files land.

Be specific with numbers. Do not invent raw metrics not shown; if a metric is null, acknowledge uncertainty."""
    return prompt


def call_claude(prompt: str) -> tuple[str, str]:
    """Returns (plain_text, model_id used)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    parts: list[str] = []
    for block in msg.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    text = "\n".join(parts).strip()
    return text, model
