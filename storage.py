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
