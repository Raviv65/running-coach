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


def _filter_suunto_json(raw_bytes: bytes) -> bytes:
    """Strip unneeded fields from a Suunto DeviceLog JSON before storage.

    Keeps: Header, Windows, Device (intact).
    Samples: retains only TimeISO8601 + HR — drops Battery, Pressure,
    Temperature, Speed, Altitude, Cadence, Power, VerticalSpeed, etc.
    Typical reduction: ~85% (1.1 MB → ~150 KB).
    """
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
        dl = data.get("DeviceLog", {})
        samples = dl.get("Samples", [])
        _KEEP = {"TimeISO8601", "HR", "Events"}
        dl["Samples"] = [
            {k: v for k, v in s.items() if k in _KEEP}
            for s in samples
            if "HR" in s or "Events" in s  # drop timestamp-only rows
        ]
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
