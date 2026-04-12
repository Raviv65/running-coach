"""
Runalyze Personal API client.

Tries several read endpoints and field shapes (Hydra / API Platform, plain JSON).
Supporter/Premium tokens expose read routes; exact paths may vary by account version.
Override with env RUNALYZE_ACTIVITY_URLS (comma-separated paths, relative to base).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

DEFAULT_ACTIVITY_URLS = (
    "/activity",
    "/activities",
    "/sport-activities",
    "/training/activities",
)

DEFAULT_WELLNESS_URLS = (
    "/days",
    "/daily-metrics",
    "/statistics/daily",
    "/wellness",
    "/metrics/daily",
)


class RunalyzeClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self.token = token or os.environ.get("RUNALYZE_TOKEN", "")
        self.base_url = (base_url or os.environ.get(
            "RUNALYZE_API_BASE", "https://runalyze.com/api/v1"
        )).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "token": self.token,
                "Accept": "application/json",
                "User-Agent": "running-coach/1.0",
            }
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        url = path if path.startswith("http") else urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            r = self.session.get(url, params=params or {}, timeout=60)
            ct = r.headers.get("content-type", "")
            if "json" in ct:
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, {"_raw": r.text[:2000]}
            return r.status_code, {"_raw": r.text[:2000]}
        except requests.RequestException as e:
            logger.warning("Runalyze GET failed %s: %s", path, e)
            return 0, {"error": str(e)}

    def ping(self) -> bool:
        code, _ = self._get("/ping")
        return code == 200

    def _iter_members(self, payload: Any) -> list[dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("hydra:member", "member", "items", "data", "results"):
            if key in payload and isinstance(payload[key], list):
                return [x for x in payload[key] if isinstance(x, dict)]
        if "activities" in payload and isinstance(payload["activities"], list):
            return [x for x in payload["activities"] if isinstance(x, dict)]
        return []

    def _paginate(self, path: str, max_pages: int = 50) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            code, body = self._get(path, {"page": page, "itemsPerPage": 100})
            if code != 200:
                break
            rows = self._iter_members(body)
            if not rows:
                code, body = self._get(path, {"page": page})
                if code != 200:
                    break
                rows = self._iter_members(body)
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < 100:
                break
        return all_rows

    def fetch_activities(self, days_back: int = 120) -> list[dict[str, Any]]:
        """Return normalized activity dicts with trimp, date, sport, ids."""
        since = (
            datetime.now(timezone.utc).date() - timedelta(days=days_back)
        ).isoformat()
        urls_env = os.environ.get("RUNALYZE_ACTIVITY_URLS")
        paths = [p.strip() for p in urls_env.split(",")] if urls_env else list(DEFAULT_ACTIVITY_URLS)

        for path in paths:
            for params in (
                {"order[time]": "desc", "time[after]": since},
                {"startedAt[after]": since},
                {"from": since},
                {},
            ):
                code, body = self._get(path, params)
                if code == 200:
                    rows = self._iter_members(body)
                    if not rows and isinstance(body, dict):
                        rows = self._iter_members(body.get("@graph"))
                    if rows:
                        logger.info("Runalyze activities: using %s (%s rows)", path, len(rows))
                        return [self._normalize_activity(r) for r in rows]
            full = self._paginate(path)
            if full:
                logger.info("Runalyze activities: paginated %s (%s rows)", path, len(full))
                return [self._normalize_activity(r) for r in full]

        logger.warning("Runalyze: no activities endpoint returned data; check token scopes.")
        return []

    def _normalize_activity(self, raw: dict[str, Any]) -> dict[str, Any]:
        trimp = _pick_float(
            raw,
            (
                "trimp",
                "trainingImpulse",
                "training_impulse",
                "trimpScore",
                # "value" and "s" intentionally omitted: "s" = seconds (duration),
                # "value" is too generic. Both cause false TRIMP hits.
            ),
        )
        tss = _pick_float(raw, ("tss", "stress", "trainingStressScore", "training_stress"))
        # "date_time" is Runalyze v1's datetime field; keep legacy names as fallbacks
        started = _pick_str(
            raw,
            ("date_time", "time", "startTime", "startedAt", "date", "datetime", "begin", "start"),
        )
        # sport and type can be nested objects {id, name, category} — extract name string
        raw_sport = raw.get("sport")
        if isinstance(raw_sport, dict):
            sport = (raw_sport.get("category") or raw_sport.get("name") or "").lower()
        else:
            sport = (_pick_str(raw, ("sport", "sportid", "activityType")) or "").lower()
        # title: prefer explicit title/note, fall back to activity type name
        raw_type = raw.get("type")
        type_name = raw_type.get("name") if isinstance(raw_type, dict) else None
        title = _pick_str(raw, ("title", "name", "note")) or type_name or "Activity"
        aid = raw.get("id") or raw.get("@id") or raw.get("uuid")
        dist = _pick_float(raw, ("distance", "route", "kilometer"))
        if dist and dist > 200:  # meters → km
            dist = dist / 1000.0
        dur = _pick_float(raw, ("duration", "elapsed_time", "elapsedTime", "s"))
        if dur and dur >= 600:  # values ≥ 600 are in seconds (≥10-min run); realistic minutes are <600
            dur = dur / 60.0
        d_iso = _parse_activity_date(started)
        return {
            "id": str(aid) if aid is not None else started or "unknown",
            "title": title or "Activity",
            "sport": sport,
            "started_at": started,
            "date": d_iso,
            "trimp": trimp,
            "tss": tss,
            "distance_km": dist,
            "duration_min": dur,
            "raw_keys": list(raw.keys())[:20],
        }

    def debug_activity_fields(self) -> None:
        """Fetch the first available activity and log its raw keys and values."""
        urls_env = os.environ.get("RUNALYZE_ACTIVITY_URLS")
        paths = [p.strip() for p in urls_env.split(",")] if urls_env else list(DEFAULT_ACTIVITY_URLS)
        for path in paths:
            code, body = self._get(path, {"itemsPerPage": 1, "page": 1})
            if code != 200:
                continue
            rows = self._iter_members(body)
            if not rows and isinstance(body, dict):
                rows = self._iter_members(body.get("@graph"))
            if rows:
                raw = rows[0]
                logger.info("[debug_activity_fields] endpoint: %s", path)
                for k, v in raw.items():
                    logger.info("  %r: %r", k, v)
                return
        logger.warning("[debug_activity_fields] no activity found across known endpoints")

    def fetch_wellness_snapshots(self) -> list[dict[str, Any]]:
        """Pull HRV and sleep from dedicated Runalyze endpoints and merge by date."""
        merged: dict[str, dict[str, Any]] = {}

        # HRV
        code, body = self._get("/metrics/hrv")
        if code == 200:
            rows = body if isinstance(body, list) else self._iter_members(body)
            for row in rows:
                raw_date = _pick_str(row, ("date_time", "date", "day"))
                d = raw_date[:10] if raw_date else None
                if d:
                    merged.setdefault(d, {})["rmssd"] = row.get("value") or row.get("rmssd")

        # Sleep
        code, body = self._get("/metrics/sleep")
        if code == 200:
            rows = body if isinstance(body, list) else self._iter_members(body)
            for row in rows:
                raw_date = _pick_str(row, ("date_time", "date", "day"))
                d = raw_date[:10] if raw_date else None
                if d:
                    merged.setdefault(d, {}).update({
                        "sleepDuration": row.get("duration"),
                        "sleepQuality": row.get("quality_100"),
                        "heartRateRest": row.get("hr_lowest"),
                    })

        return [{"date": d, **v} for d, v in merged.items()]


def _pick_float(d: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None and "/" in k:
            p = k.split("/")
            cur: Any = d
            for part in p:
                cur = cur.get(part) if isinstance(cur, dict) else None
            v = cur
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _pick_str(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        return str(v)
    return None


def _parse_activity_date(started: str | None) -> str | None:
    if not started:
        return None
    s = started.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date().isoformat()
    except ValueError:
        pass
    try:
        return date.fromisoformat(started[:10]).isoformat()
    except ValueError:
        return None


def _is_running_activity(a: dict[str, Any]) -> bool:
    sport = (a.get("sport") or "").lower().strip()
    if not sport:
        return True
    if "run" in sport or sport in ("running", "jogging", "trail", "track"):
        return True
    if sport == "1":
        return True
    if sport.isdigit():
        return False
    return False


def group_activities_by_date(
    activities: list[dict[str, Any]], running_only: bool = True
) -> dict[str, list[dict[str, Any]]]:
    by: dict[str, list[dict[str, Any]]] = {}
    for a in activities:
        d = a.get("date")
        if not d:
            continue
        if running_only and not _is_running_activity(a):
            continue
        by.setdefault(d, []).append(a)
    return dict(sorted(by.items()))


def extract_daily_wellness(
    snapshots: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Best-effort map date -> hrv (RMSSD), sleep duration, quality, resting HR."""
    out: dict[str, dict[str, Any]] = {}
    for row in snapshots:
        d = _row_date(row)
        if not d:
            continue
        hrv = _pick_float(row, ("rmssd", "hrv", "HRV", "hrvRmssd", "hrv_rmssd"))
        sleep_min = _pick_float(
            row,
            ("sleepDuration", "sleep_duration", "duration", "sleep_duration_min"),
        )
        quality = _pick_float(row, ("sleepQuality", "sleep_quality", "quality"))
        rhr = _pick_float(
            row,
            ("restingHeartRate", "resting_hr", "heartRateRest", "heart_rate_rest", "rhr", "hr_lowest"),
        )
        entry = {k: v for k, v in {
            "hrv_last": hrv,
            "sleep_duration_min": sleep_min,
            "sleep_quality": quality,
            "resting_hr": rhr,
        }.items() if v is not None}
        if entry:
            out[d] = entry
    return out


def _row_date(row: dict[str, Any]) -> str | None:
    s = _pick_str(
        row,
        ("date", "day", "time", "startTime", "date_time", "datetime", "startedAt"),
    )
    if not s:
        return None
    return _parse_activity_date(s)


def merge_wellness_into_state(
    existing_metrics: dict[str, Any],
    fresh: dict[str, dict[str, Any]],
    today: str,
) -> tuple[dict[str, Any], dict[str, bool]]:
    """
    For missing keys today, carry forward last known non-null (flagged estimated).
    Returns (today_record, estimated_flags).
    """
    fields = ("hrv_last", "sleep_duration_min", "sleep_quality", "resting_hr")
    estimated: dict[str, bool] = {f: False for f in fields}
    last: dict[str, float | None] = {f: None for f in fields}

    for d in sorted(existing_metrics.keys()):
        rec = existing_metrics[d]
        for f in fields:
            v = rec.get(f)
            if v is None and f == "hrv_last":
                v = rec.get("hrv_rmssd")
            if v is not None:
                last[f] = float(v)

    today_rec: dict[str, Any] = dict(fresh.get(today, {}))
    for f in fields:
        if today_rec.get(f) is None and last.get(f) is not None:
            today_rec[f] = last[f]
            estimated[f] = True
    return today_rec, estimated
