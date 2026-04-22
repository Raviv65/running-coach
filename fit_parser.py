"""Parse Suunto FIT files — reads training_stress_score directly from the session."""

from __future__ import annotations

import fitparse


def parse_fit(data: bytes) -> dict:
    import io
    f = fitparse.FitFile(io.BytesIO(data))

    session = {}
    for msg in f.get_messages("session"):
        session = {d.name: d.value for d in msg.fields if d.value is not None}
        break  # one session per file

    # HR timeseries from record messages
    hr_pts = []
    t0 = None
    for msg in f.get_messages("record"):
        rec = {d.name: d.value for d in msg.fields if d.value is not None}
        ts = rec.get("timestamp")
        hr = rec.get("heart_rate")
        if ts and hr:
            if t0 is None:
                t0 = ts
            hr_pts.append({"t": round((ts - t0).total_seconds()), "hr": float(hr)})

    # Sample to max 200 points
    step = max(1, len(hr_pts) // 200)
    hr_sampled = hr_pts[::step]

    start = session.get("start_time")
    act_date = start.strftime("%Y-%m-%d") if start else None

    duration_s = session.get("total_elapsed_time") or session.get("total_timer_time")
    distance_m = session.get("total_distance")

    zones_raw = session.get("time_in_hr_zone") or (0, 0, 0, 0, 0)
    zones = list(zones_raw) + [0] * (5 - len(zones_raw))

    return {
        "date": act_date,
        "training_stress_score": round(float(session["training_stress_score"]), 1) if session.get("training_stress_score") else None,
        "duration_min": round(duration_s / 60, 1) if duration_s else None,
        "distance_km": round(distance_m / 1000, 2) if distance_m else None,
        "avg_hr": float(session.get("avg_heart_rate")) if session.get("avg_heart_rate") else None,
        "max_hr": float(session.get("max_heart_rate")) if session.get("max_heart_rate") else None,
        "calories_kcal": int(session.get("total_calories")) if session.get("total_calories") else None,
        "epoc": float(session.get("peak_epoc")) if session.get("peak_epoc") else None,
        "peak_training_effect": float(session.get("total_training_effect")) if session.get("total_training_effect") else None,
        "recovery_time_hrs": round(float(session["recovery_time"]) / 3600, 1) if session.get("recovery_time") else None,
        "hr_zones": {
            "z1": round(zones[0] / 60, 1),
            "z2": round(zones[1] / 60, 1),
            "z3": round(zones[2] / 60, 1),
            "z4": round(zones[3] / 60, 1),
            "z5": round(zones[4] / 60, 1),
        },
        "hr_timeseries": hr_sampled,
        "title": "Running",
        "sport": "running",
        "source": "suunto_fit",
    }
