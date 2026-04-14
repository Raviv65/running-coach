"""Atomic JSON load/save — backed by Google Cloud Storage."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

GCS_BUCKET = os.environ.get("GCS_BUCKET", "running-coach-data-uplifted")
GCS_OBJECT = os.environ.get("GCS_OBJECT", "metrics.json")


def _client():
    from google.cloud import storage
    return storage.Client()


def load_metrics() -> dict[str, Any]:
    try:
        client = _client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_OBJECT)
        if not blob.exists():
            logger.warning("metrics.json not found in GCS; returning default")
            return default_structure()
        content = blob.download_as_text(encoding="utf-8")
        return json.loads(content)
    except Exception as e:
        logger.error("GCS load failed: %s; returning default", e)
        return default_structure()


def save_metrics(data: dict[str, Any]) -> None:
    try:
        client = _client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(GCS_OBJECT)
        blob.upload_from_string(
            json.dumps(data, indent=2, ensure_ascii=False),
            content_type="application/json"
        )
        logger.info("metrics.json saved to GCS")
    except Exception as e:
        logger.error("GCS save failed: %s", e)
        raise


# Suunto ActivityType codes for indoor/treadmill activities.
# Speed, Altitude, Distance etc. are meaningless on a treadmill so we strip them.
# Outdoor activities keep all sensor data for future analysis.
_TREADMILL_ACTIVITY_TYPES = {
    11,   # Treadmill Running
    79,   # Indoor Running (some Suunto firmware versions)
}


def _is_treadmill(data: dict) -> bool:
    activity_type = data.get("DeviceLog", {}).get("Header", {}).get("ActivityType")
    return activity_type in _TREADMILL_ACTIVITY_TYPES


def _filter_suunto_json(raw_bytes: bytes) -> bytes:
    """Strip unneeded fields from a Suunto DeviceLog JSON before storage.

    Only applied for treadmill activities (ActivityType in _TREADMILL_ACTIVITY_TYPES).
    For outdoor activities the file is saved as-is.

    Treadmill filter keeps: Header, Windows, Device (intact).
    Samples: retains only TimeISO8601 + HR — drops Battery, Pressure,
    Temperature, Speed, Altitude, Cadence, Power, VerticalSpeed, etc.
    Typical reduction: ~80% (1.1 MB → ~200 KB).
    """
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
        if not _is_treadmill(data):
            logger.info("Non-treadmill activity — saving JSON unfiltered")
            return raw_bytes
        dl = data.get("DeviceLog", {})
        samples = dl.get("Samples", [])
        _KEEP = {"TimeISO8601", "HR", "Events"}
        dl["Samples"] = [
            {k: v for k, v in s.items() if k in _KEEP}
            for s in samples
            if "HR" in s or "Events" in s  # drop timestamp-only rows
        ]
        logger.info(
            "Treadmill activity (type %s) — filtered samples from %d to %d",
            data["DeviceLog"]["Header"].get("ActivityType"),
            len(samples),
            len(dl["Samples"]),
        )
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except Exception as e:
        logger.warning("Could not filter Suunto JSON, saving raw: %s", e)
        return raw_bytes


def save_activity_json_to_gcs(raw_bytes: bytes, activity_date_str: str) -> None:
    """Save filtered Suunto JSON to GCS at activities/DDMMYYYY.json.

    activity_date_str should be in YYYY-MM-DD format (e.g. '2026-04-12').
    The GCS path will be activities/12042026.json.
    """
    try:
        # Convert YYYY-MM-DD -> DDMMYYYY
        parts = activity_date_str.split("-")
        ddmmyyyy = parts[2] + parts[1] + parts[0] if len(parts) == 3 else activity_date_str
        gcs_path = f"activities/{ddmmyyyy}.json"
        filtered = _filter_suunto_json(raw_bytes)
        original_kb = len(raw_bytes) // 1024
        filtered_kb = len(filtered) // 1024
        client = _client()
        bucket = client.bucket(GCS_BUCKET)
        blob = bucket.blob(gcs_path)
        blob.upload_from_string(filtered, content_type="application/json")
        logger.info(
            "Activity JSON saved to GCS at %s (%d KB → %d KB)",
            gcs_path, original_kb, filtered_kb,
        )
    except Exception as e:
        logger.error("Failed to save activity JSON to GCS: %s", e)


def _date_to_ddmmyyyy(date_str: str) -> str:
    parts = date_str.split("-")
    return parts[2] + parts[1] + parts[0] if len(parts) == 3 else date_str


def load_activity_json_from_gcs(activity_date_str: str) -> dict | None:
    """Load a saved Suunto JSON from GCS for a given YYYY-MM-DD date.
    Returns the parsed DeviceLog Header + hr_timeseries, or None if not found.
    """
    try:
        ddmmyyyy = _date_to_ddmmyyyy(activity_date_str)
        client = _client()
        blob = client.bucket(GCS_BUCKET).blob(f"activities/{ddmmyyyy}.json")
        if not blob.exists():
            return None
        data = json.loads(blob.download_as_text(encoding="utf-8"))
        return data
    except Exception as e:
        logger.warning("Could not load activity JSON from GCS for %s: %s", activity_date_str, e)
        return None


def list_activity_json_dates() -> set[str]:
    """Return the set of YYYY-MM-DD dates that have a saved JSON in GCS."""
    try:
        client = _client()
        blobs = client.bucket(GCS_BUCKET).list_blobs(prefix="activities/")
        dates = set()
        for blob in blobs:
            name = blob.name.split("/")[-1].replace(".json", "")  # DDMMYYYY
            if len(name) == 8:
                dd, mm, yyyy = name[:2], name[2:4], name[4:]
                dates.add(f"{yyyy}-{mm}-{dd}")
        return dates
    except Exception as e:
        logger.warning("Could not list activity JSONs from GCS: %s", e)
        return set()


def restore_from_github() -> bool:
    """No-op — GCS is now the source of truth."""
    return False


def default_structure() -> dict[str, Any]:
    return {
        "metrics": {},
        "activities": {},
        "briefings": {},
        "meta": {
            "last_sync": None,
            "trimp_history": [],
            "athlete": {
                "name": "Raviv",
                "goal": "10 km in 60 min",
                "watch": "Suunto Vertical",
                "threshold_hr": 160,
            },
        },
    }
