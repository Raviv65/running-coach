"""CTL / ATL / TSB, ramp rate, A:C ratio, recovery proxy score."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any


TAU_CTL = 42.0
TAU_ATL = 7.0


def _decay(tau: float) -> tuple[float, float]:
    a = math.exp(-1.0 / tau)
    b = 1.0 - a
    return a, b


def daily_trimp_totals(activities_by_date: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d, acts in activities_by_date.items():
        s = 0.0
        for a in acts:
            t = a.get("trimp")
            if t is not None:
                s += float(t)
        out[d] = s
    return out


def expand_calendar(
    daily_trimp: dict[str, float], start: date, end: date
) -> dict[str, float]:
    """Fill missing days with 0 TRIMP between start and end inclusive."""
    cur = start
    filled: dict[str, float] = {}
    while cur <= end:
        ds = cur.isoformat()
        filled[ds] = float(daily_trimp.get(ds, 0.0))
        cur += timedelta(days=1)
    return filled


def ctl_atl_tsb_series(
    daily_trimp: dict[str, float],
    seed_ctl: float | None = None,
    seed_atl: float | None = None,
) -> dict[str, dict[str, float]]:
    """
    Banister-style exponential filters (per user spec).
    seed_ctl / seed_atl initialize state before the first day in sorted keys.
    """
    a_c, b_c = _decay(TAU_CTL)
    a_a, b_a = _decay(TAU_ATL)
    ctl = float(seed_ctl or 0.0)
    atl = float(seed_atl or 0.0)
    out: dict[str, dict[str, float]] = {}
    for d in sorted(daily_trimp.keys()):
        t = float(daily_trimp.get(d, 0.0) or 0.0)
        ctl = ctl * a_c + t * b_c
        atl = atl * a_a + t * b_a
        out[d] = {"ctl": ctl, "atl": atl, "tsb": ctl - atl}
    return out


def ramp_rate_ctl(series: dict[str, dict[str, float]], days: int = 7) -> float | None:
    keys = sorted(series.keys())
    if len(keys) < days + 1:
        return None
    today = keys[-1]
    past = keys[-1 - days]
    return series[today]["ctl"] - series[past]["ctl"]


def ac_ratio(ctl: float, atl: float) -> float | None:
    if ctl <= 0:
        return None
    return atl / ctl


def recovery_score(
    hrv_last: float | None,
    hrv_30d: float | None,
    sleep_quality: float | None,
    rhr_last: float | None,
    rhr_30d: float | None,
    tsb: float | None,
) -> float | None:
    """
    Weighted proxy 0-100:
    HRV vs 30d (40%), sleep quality (30%), RHR trend vs 30d (20%), TSB (10%).
    """
    parts: list[tuple[float, float]] = []

    if hrv_last is not None and hrv_30d and hrv_30d > 0:
        ratio = max(0.5, min(1.5, hrv_last / hrv_30d))
        hrv_score = (ratio - 0.5) / 1.0 * 100.0
        parts.append((0.4, max(0.0, min(100.0, hrv_score))))

    if sleep_quality is not None:
        sq = max(0.0, min(10.0, float(sleep_quality)))
        parts.append((0.3, (sq / 10.0) * 100.0))

    if rhr_last is not None and rhr_30d and rhr_30d > 0:
        # Lower than baseline is better
        diff = (rhr_30d - rhr_last) / rhr_30d
        rhr_score = 50.0 + diff * 200.0
        parts.append((0.2, max(0.0, min(100.0, rhr_score))))

    if tsb is not None:
        # Map -40..+20 to 0..100
        t = max(-40.0, min(20.0, float(tsb)))
        tsb_score = (t + 40.0) / 60.0 * 100.0
        parts.append((0.1, max(0.0, min(100.0, tsb_score))))

    if not parts:
        return None
    wsum = sum(w for w, _ in parts)
    if wsum <= 0:
        return None
    return sum(w * s for w, s in parts) / wsum


def build_trimp_history(daily_trimp: dict[str, float], days: int = 42) -> list[dict[str, Any]]:
    keys = sorted(daily_trimp.keys())[-days:]
    return [{"date": k, "trimp": daily_trimp[k]} for k in keys]


def enrich_metrics_history(
    metrics: dict[str, Any],
    series: dict[str, dict[str, float]],
) -> None:
    """Add ctl/atl/tsb to historical metric rows where dates align."""
    for d, load in series.items():
        m = metrics.setdefault(d, {})
        m["ctl"] = round(load["ctl"], 2)
        m["atl"] = round(load["atl"], 2)
        m["tsb"] = round(load["tsb"], 2)
