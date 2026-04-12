"""Claude prompt builder and Anthropic API call."""

from __future__ import annotations

import json
import os
from typing import Any

import anthropic


ATHLETE_PROFILE = """
Athlete: Raviv | Location: Israel
Primary Goal: Run 10 km in 60 minutes (10 km/h)
Current estimate: 10 km in ~73–77 min — gap of ~13–17 min to close
Training: 3 runs/week (treadmill) + hiking as cross-training (counts toward load)
Max HR: ~160 bpm | Aerobic base HR: 130–135 bpm | Threshold HR: 155–160 bpm
Cardiac drift: typically 6–10 bpm over 40 min (target: reduce below 5 bpm)
HRV range: 23–40 | Average: ~29–33 | High HRV = green light for quality work
Optimal TSB zone: -5 to 0 | Avoid stacking intensity when TSB < -10
Performs best when: HRV >= 30, TSB between -5 and 0, sleep quality high
Strengths: aerobic base, pacing discipline, consistency, data-driven approach
Weaknesses: speed endurance, moderate cardiac drift, threshold underdeveloped
Injury history: none
Typical session: 10 min warmup @ 6.3 km/h → 40 min @ 7.4 km/h → 10-15 min @ 8.0-8.2 km/h → 5 min cooldown
Development priorities: extend duration at 8.0-8.5 km/h, tempo runs, intervals at 9-10 km/h, reduce cardiac drift
Weekly structure: Easy run / Steady run / Quality run / Hiking
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


def build_prompt(db: dict[str, Any], today: str) -> str:
    meta = db.get("meta") or {}
    athlete = meta.get("athlete") or {}
    name = athlete.get("name", "Athlete")
    goal = athlete.get("goal", "10 km in 60 min")
    watch = athlete.get("watch", "")
    th = int(athlete.get("threshold_hr") or 160)
    zones = hr_zones(th)
    ztext = ", ".join(f"{k}: {v}" for k, v in zones.items())

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

    prompt = f"""You are an expert running coach. Produce TODAY's training briefing for {name}.

## ATHLETE PROFILE
{ATHLETE_PROFILE}
Watch: {watch} | HR zones: {ztext}

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

## Tomorrow outlook
Short paragraph.

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
