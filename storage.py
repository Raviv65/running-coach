"""Atomic JSON load/save for the metrics database."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def metrics_path() -> Path:
    base = os.environ.get("DATA_DIR", Path(__file__).resolve().parent)
    p = os.environ.get("METRICS_FILE", str(Path(base) / "data" / "metrics.json"))
    return Path(p)


def load_metrics() -> dict[str, Any]:
    path = metrics_path()
    if not path.exists():
        return default_structure()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_metrics(data: dict[str, Any]) -> None:
    path = metrics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".metrics_", suffix=".tmp", text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


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
