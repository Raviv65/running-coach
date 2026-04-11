"""
Flask web app: dashboard + APScheduler (05:00 UTC pipeline, 05:30 UTC email).
"""

from __future__ import annotations

import logging
import os
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, render_template

from analyze import build_prompt, call_claude
from backup import push_metrics_backup
from compute import (
    ac_ratio,
    build_trimp_history,
    ctl_atl_tsb_series,
    enrich_metrics_history,
    expand_calendar,
    ramp_rate_ctl,
    recovery_score,
    daily_trimp_totals,
)
from email_sender import markdown_to_html, send_briefing_email
from storage import load_metrics, save_metrics
from sync import (
    RunalyzeClient,
    extract_daily_wellness,
    group_activities_by_date,
    merge_wellness_into_state,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("running-coach")

app = Flask(__name__)


def utc_today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _mean_field(
    metrics: dict[str, Any],
    keys_sorted: list[str],
    field: str,
    n: int,
) -> float | None:
    tail = keys_sorted[-n:]
    xs: list[float] = []
    for k in tail:
        rec = metrics.get(k) or {}
        v = rec.get(field)
        if v is None and field == "hrv_last":
            v = rec.get("hrv_rmssd")
        if v is not None:
            xs.append(float(v))
    if not xs:
        return None
    return sum(xs) / len(xs)


def _activities_from_db(db: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    raw = db.get("activities") or {}
    if not raw:
        return {}
    if isinstance(raw, list):
        by: dict[str, list[dict[str, Any]]] = {}
        for row in raw:
            d = row.get("date")
            if d:
                by.setdefault(d, []).append(row)
        return dict(sorted(by.items()))
    return dict(sorted(raw.items()))


def run_daily_pipeline(send_email_now: bool = False) -> dict[str, Any]:
    """Sync Runalyze → compute → Claude → save → GitHub backup. Optionally send email."""
    db = load_metrics()
    meta = db.setdefault("meta", {})
    athlete = meta.setdefault(
        "athlete",
        {
            "name": "Raviv",
            "goal": "10 km in 60 min",
            "watch": "Suunto Vertical",
            "threshold_hr": 160,
        },
    )
    metrics = db.setdefault("metrics", {})
    today = utc_today_iso()
    today_d = date.fromisoformat(today)

    client = RunalyzeClient()
    activities: list[dict[str, Any]] = []
    try:
        if client.token:
            activities = client.fetch_activities(days_back=120)
            if not client.ping():
                logger.warning("Runalyze ping failed; token may be invalid.")
        else:
            logger.warning("RUNALYZE_TOKEN not set; using stored activities only.")
    except Exception:
        logger.exception("Runalyze fetch error")

    if activities:
        grouped = group_activities_by_date(activities, running_only=True)
        db["activities"] = grouped
    else:
        grouped = _activities_from_db(db)
        logger.info("Using %s activity days from local JSON", len(grouped))

    wellness_rows: list[dict[str, Any]] = []
    try:
        if client.token:
            wellness_rows = client.fetch_wellness_snapshots()
    except Exception:
        logger.exception("Runalyze wellness fetch error")

    fresh_wellness = extract_daily_wellness(wellness_rows)
    today_well, est_flags = merge_wellness_into_state(metrics, fresh_wellness, today)

    daily_trimp = daily_trimp_totals(grouped)
    start_d = today_d - timedelta(days=119)
    expanded = expand_calendar(daily_trimp, start_d, today_d)

    seed_ctl = meta.get("seed_ctl")
    seed_atl = meta.get("seed_atl")
    series = ctl_atl_tsb_series(
        expanded,
        seed_ctl=float(seed_ctl) if seed_ctl is not None else None,
        seed_atl=float(seed_atl) if seed_atl is not None else None,
    )
    enrich_metrics_history(metrics, series)

    m_today = metrics.setdefault(today, {})
    m_today.update(today_well)
    m_today["estimated"] = est_flags

    keys_sorted = sorted(metrics.keys())
    a7 = _mean_field(metrics, keys_sorted, "hrv_last", 7)
    a30 = _mean_field(metrics, keys_sorted, "hrv_last", 30)
    m_today["hrv_7d_avg"] = round(a7, 2) if a7 is not None else None
    m_today["hrv_30d_avg"] = round(a30, 2) if a30 is not None else None

    load_today = series.get(today, {})
    ctl_v = load_today.get("ctl")
    atl_v = load_today.get("atl")
    tsb_v = load_today.get("tsb")
    if ctl_v is not None:
        m_today["ctl"] = round(ctl_v, 2)
    if atl_v is not None:
        m_today["atl"] = round(atl_v, 2)
    if tsb_v is not None:
        m_today["tsb"] = round(tsb_v, 2)

    rr = ramp_rate_ctl(series, 7)
    m_today["ramp_rate"] = round(rr, 3) if rr is not None else None
    ar = ac_ratio(float(ctl_v or 0), float(atl_v or 0)) if ctl_v else None
    m_today["ac_ratio"] = round(ar, 3) if ar is not None else None

    hrv_last = m_today.get("hrv_last")
    if hrv_last is None and m_today.get("hrv_rmssd") is not None:
        hrv_last = m_today.get("hrv_rmssd")

    rhr_30_vals = [
        float(metrics[k]["resting_hr"])
        for k in keys_sorted[-30:]
        if metrics.get(k, {}).get("resting_hr") is not None
    ]
    rhr_30_avg = sum(rhr_30_vals) / len(rhr_30_vals) if rhr_30_vals else None

    rec = recovery_score(
        float(hrv_last) if hrv_last is not None else None,
        m_today.get("hrv_30d_avg"),
        m_today.get("sleep_quality"),
        float(m_today["resting_hr"]) if m_today.get("resting_hr") is not None else None,
        rhr_30_avg,
        float(tsb_v) if tsb_v is not None else None,
    )
    m_today["recovery_score"] = round(rec, 1) if rec is not None else None

    meta["trimp_history"] = build_trimp_history(expanded, 42)
    meta["last_sync"] = datetime.now(timezone.utc).isoformat()

    try:
        text, model = call_claude(build_prompt(db, today))
    except Exception as e:
        logger.warning("Claude briefing failed: %s", e)
        text = f"**Briefing unavailable** ({e!s}). Check ANTHROPIC_API_KEY and model name."
        model = "error"
    briefings = db.setdefault("briefings", {})
    briefings[today] = {
        "markdown": text,
        "html": markdown_to_html(text),
        "model": model,
        "created_utc": datetime.now(timezone.utc).isoformat(),
    }

    save_metrics(db)

    try:
        if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPO"):
            push_metrics_backup(db)
    except Exception:
        logger.exception("GitHub backup failed")

    if send_email_now:
        try:
            subj = f"Running briefing — {today}"
            send_briefing_email(subj, text)
        except Exception:
            logger.exception("Email send failed")

    return {"ok": True, "today": today}


def scheduled_pipeline() -> None:
    try:
        run_daily_pipeline(send_email_now=False)
    except Exception:
        logger.error("scheduled_pipeline failed:\n%s", traceback.format_exc())


def scheduled_email() -> None:
    try:
        db = load_metrics()
        today = utc_today_iso()
        b = (db.get("briefings") or {}).get(today) or {}
        md = b.get("markdown")
        if not md:
            logger.warning("No briefing for %s; skipping email.", today)
            return
        subj = f"Running briefing — {today}"
        send_briefing_email(subj, md)
    except Exception:
        logger.exception("scheduled_email failed")


scheduler: BackgroundScheduler | None = None
_scheduler_started = False


def init_scheduler() -> None:
    global scheduler, _scheduler_started
    if os.environ.get("DISABLE_SCHEDULER", "").lower() in ("1", "true", "yes"):
        return
    if _scheduler_started:
        return
    _scheduler_started = True
    scheduler = BackgroundScheduler(timezone=timezone.utc)
    scheduler.add_job(scheduled_pipeline, "cron", hour=5, minute=0, id="daily_pipeline")
    scheduler.add_job(scheduled_email, "cron", hour=5, minute=30, id="daily_email")
    scheduler.start()
    logger.info("APScheduler started (05:00 UTC pipeline, 05:30 UTC email).")


@app.before_request
def _ensure_scheduler() -> None:
    init_scheduler()


@app.route("/")
def index():
    db = load_metrics()
    today = utc_today_iso()
    m = (db.get("metrics") or {}).get(today) or {}
    b = (db.get("briefings") or {}).get(today) or {}
    athlete = (db.get("meta") or {}).get("athlete") or {}
    return render_template(
        "index.html",
        today=today,
        metrics=m,
        briefing_html=b.get("html") or "",
        athlete=athlete,
        last_sync=(db.get("meta") or {}).get("last_sync"),
    )


@app.route("/history")
def history():
    db = load_metrics()
    end = date.fromisoformat(utc_today_iso())
    start = end - timedelta(days=89)
    labels: list[str] = []
    cur = start
    while cur <= end:
        labels.append(cur.isoformat())
        cur += timedelta(days=1)

    def series_for(field: str) -> list[float | None]:
        out: list[float | None] = []
        metrics = db.get("metrics") or {}
        for lab in labels:
            v = metrics.get(lab, {}).get(field)
            out.append(float(v) if v is not None else None)
        return out

    chart_payload = {
        "labels": labels,
        "ctl": series_for("ctl"),
        "atl": series_for("atl"),
        "tsb": series_for("tsb"),
        "hrv": series_for("hrv_last"),
        "sleep": series_for("sleep_duration_min"),
        "rhr": series_for("resting_hr"),
    }
    return render_template("history.html", chart_payload=chart_payload)


@app.route("/activity")
def activity_log():
    db = load_metrics()
    rows: list[dict[str, Any]] = []
    for d, acts in sorted((db.get("activities") or {}).items(), reverse=True):
        for a in acts:
            rows.append({"date": d, **a})
    return render_template("activity.html", rows=rows)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}



if __name__ == "__main__":
    init_scheduler()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
