# Running coach (Flask)

Daily automated training briefing for a runner targeting **10 km in 60 minutes**, with Runalyze sync, Banister-style **CTL / ATL / TSB**, a **Claude Sonnet 4.6** briefing, **Gmail** delivery, and optional **GitHub** backup of `metrics.json`.

## Features

- **05:00 UTC** — Pull Runalyze data, recompute load metrics, call Claude, save JSON, push to GitHub (if configured).
- **05:30 UTC** — Send the same briefing by email (HTML + plain text).
- **Dashboard** — Today’s metrics and briefing; **History** — 90-day Chart.js trends; **Activities** — log with TRIMP/TSS.

Scheduler timezone is **UTC** (05:00 UTC = **08:00** Israel standard time; adjust mentally for DST).

## Local setup

1. **Python 3.12+** recommended.

2. Create a virtualenv and install dependencies:

   ```bash
   cd running-coach
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in secrets (see below).

4. Run the app:

   ```bash
   export FLASK_DEBUG=1
   python app.py
   ```

   Open `http://127.0.0.1:5000`.

5. **One-off pipeline** (sync + compute + Claude + save + email optional) from a Python shell:

   ```python
   from app import run_daily_pipeline
   run_daily_pipeline(send_email_now=True)
   ```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `RUNALYZE_TOKEN` | Personal API token; header `token: …`. Read endpoints need Supporter/Premium-style read scopes. |
| `ANTHROPIC_API_KEY` | Claude API key. |
| `ANTHROPIC_MODEL` | Default `claude-sonnet-4-6`. |
| `GMAIL_USER` | Gmail address used to send mail. |
| `GMAIL_APP_PASSWORD` | [App password](https://support.google.com/accounts/answer/185833) (not your normal password). |
| `RECIPIENT_EMAIL` | Inbox that receives the daily briefing. |
| `GITHUB_TOKEN` | `repo` scope for private repo push. |
| `GITHUB_REPO` | `owner/name` of the backup repository. |
| `DATA_DIR` / `METRICS_FILE` | Optional paths for `metrics.json` (see Render below). |
| `DISABLE_SCHEDULER` | Set to `1` to disable APScheduler (e.g. local dev). |

### Runalyze notes

- Base URL: `https://runalyze.com/api/v1`, auth header **`token: {RUNALYZE_TOKEN}`** (see [Personal API](https://runalyze.com/help/article/personal-api)).
- Read routes differ by account tier; if activities are empty, set `RUNALYZE_ACTIVITY_URLS` in `.env` to the paths shown in your Runalyze API docs (comma-separated).
- If HRV/sleep/RHR are missing from the API response, the app **carries forward the last known value** and marks the field as **estimated** in that day’s `metrics` record.

### CTL seed

`data/metrics.json` includes `meta.seed_ctl` and `meta.seed_atl` so the exponential filters do not start from absolute zero. Tune these to match your current fitness/fatigue if needed.

## JSON layout

Keys under `metrics` are **ISO dates** (UTC calendar day used for the pipeline). Each day can include:

`ctl`, `atl`, `tsb`, `ramp_rate`, `ac_ratio`, `hrv_last`, `hrv_7d_avg`, `hrv_30d_avg`, `sleep_duration_min`, `sleep_quality`, `resting_hr`, `recovery_score`, `estimated` (per-field flags when values are carried forward).

## Deploy on Render

1. Push this repo to GitHub.

2. In Render: **New → Blueprint**, connect the repo, and apply `render.yaml`.

3. **Disk**: The sample blueprint mounts a **1 GB disk** at `/var/data` and sets `METRICS_FILE=/var/data/metrics.json` so data survives redeploys.

4. In the Render dashboard, set **secret** env vars: `RUNALYZE_TOKEN`, `ANTHROPIC_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`, `GITHUB_TOKEN`, `GITHUB_REPO`.

5. Use **exactly one Gunicorn worker** (`--workers 1` as in `render.yaml`) so a single process owns APScheduler.

6. Optional: add a **Cron Job** that `curl`s your `/healthz` periodically if the service sleeps on a free plan—paid/always-on instances are better for reliable 05:00 / 05:30 UTC jobs.

## Project files

| File | Role |
|------|------|
| `app.py` | Flask routes, scheduler, daily pipeline orchestration. |
| `sync.py` | Runalyze HTTP client and activity/wellness normalization. |
| `compute.py` | CTL/ATL/TSB, ramp rate, A:C, recovery score. |
| `analyze.py` | Claude prompt + API call. |
| `email_sender.py` | SMTP HTML email via Gmail. |
| `backup.py` | GitHub Contents API backup. |
| `storage.py` | Atomic JSON load/save. |

## License

Private / personal use.
