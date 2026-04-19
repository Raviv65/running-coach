"""
CTL / ATL / TSB training-load tracker that matches Suunto's integer-rounding formula.

State is stored in GCS as ``training_load.json`` (separate from metrics.json).

Formula — applied once per calendar day, rounding after every step:
    CTL = round(CTL + (tss - CTL) * (1 - exp(-1/42)))
    ATL = round(ATL + (tss - ATL) * (1 - exp(-1/7)))
    TSB = CTL - ATL

Usage from other modules:
    from training_load import seed, add_activity, update_to_date, get_training_load

CLI:
    python training_load.py --seed 2026-04-19,39,34   # bootstrap from Suunto values
    python training_load.py                            # print current CTL/ATL/TSB
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from math import exp
from typing import Any

logger = logging.getLogger(__name__)

GCS_BUCKET = os.environ.get("GCS_BUCKET", "running-coach-data-uplifted")
_TL_OBJECT = "training_load.json"

_K_CTL = 1.0 - exp(-1.0 / 42.0)
_K_ATL = 1.0 - exp(-1.0 / 7.0)


# ---------------------------------------------------------------------------
# GCS helpers
# ---------------------------------------------------------------------------

def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def _load_state() -> dict[str, Any]:
    """Load training_load.json from GCS. Returns empty state on first run."""
    try:
        client = _gcs_client()
        blob = client.bucket(GCS_BUCKET).blob(_TL_OBJECT)
        if not blob.exists():
            return {"ctl": 0, "atl": 0, "last_updated": None, "activities": []}
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as e:
        logger.error("training_load: GCS load failed: %s", e)
        return {"ctl": 0, "atl": 0, "last_updated": None, "activities": []}


def _save_state(state: dict[str, Any], retries: int = 3) -> None:
    payload = json.dumps(state, indent=2, ensure_ascii=False)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            client = _gcs_client()
            client.bucket(GCS_BUCKET).blob(_TL_OBJECT).upload_from_string(
                payload, content_type="application/json"
            )
            return
        except Exception as e:
            last_exc = e
            logger.warning("training_load: GCS save attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed(date_str: str, ctl: int, atl: int) -> None:
    """
    Bootstrap from known Suunto values. Discards any stored activities after
    date_str so they don't re-apply load already baked into the seed.

    Args:
        date_str: ISO date string, e.g. "2026-04-19"
        ctl: Suunto's current CTL (integer)
        atl: Suunto's current ATL (integer)
    """
    state = _load_state()
    # Keep activities on or before seed date; newer ones would be double-counted.
    state["activities"] = [
        a for a in state.get("activities", [])
        if a["date"] <= date_str
    ]
    state["ctl"] = int(ctl)
    state["atl"] = int(atl)
    state["last_updated"] = date_str
    _save_state(state)
    logger.info("training_load seeded: date=%s CTL=%d ATL=%d", date_str, ctl, atl)


def add_activity(date_str: str, tss: float) -> bool:
    """
    Register a FIT upload's TSS. Idempotent — skips if the same date+tss
    already exists. Returns True if a new entry was added.
    """
    state = _load_state()
    activities = state.setdefault("activities", [])
    tss_int = round(float(tss))
    # Deduplicate by date+tss
    if any(a["date"] == date_str and a["tss"] == tss_int for a in activities):
        return False
    activities.append({"date": date_str, "tss": tss_int})
    activities.sort(key=lambda a: a["date"])
    _save_state(state)
    logger.info("training_load: added activity date=%s tss=%d", date_str, tss_int)
    return True


def update_to_date(target_date_str: str) -> None:
    """
    Advance CTL/ATL/TSB from last_updated up to target_date_str, applying
    Suunto's integer-rounding formula once per calendar day.

    If last_updated is None (unseeded state) this is a no-op.
    """
    state = _load_state()
    last = state.get("last_updated")
    if not last:
        return  # not seeded yet

    last_d = date.fromisoformat(last)
    target_d = date.fromisoformat(target_date_str)
    if target_d <= last_d:
        return  # already up to date

    # Build a lookup: date → total TSS for that day
    tss_by_date: dict[str, int] = {}
    for a in state.get("activities", []):
        if a["date"] > last:
            tss_by_date[a["date"]] = tss_by_date.get(a["date"], 0) + a["tss"]

    ctl = state["ctl"]
    atl = state["atl"]
    cur = last_d + timedelta(days=1)
    while cur <= target_d:
        ds = cur.isoformat()
        tss = tss_by_date.get(ds, 0)
        ctl = round(ctl + (tss - ctl) * _K_CTL)
        atl = round(atl + (tss - atl) * _K_ATL)
        cur += timedelta(days=1)

    state["ctl"] = ctl
    state["atl"] = atl
    state["last_updated"] = target_date_str
    _save_state(state)
    logger.info("training_load updated to %s: CTL=%d ATL=%d TSB=%d", target_date_str, ctl, atl, ctl - atl)


def get_training_load() -> dict[str, Any]:
    """Return current {ctl, atl, tsb, last_updated}."""
    state = _load_state()
    ctl = state.get("ctl", 0)
    atl = state.get("atl", 0)
    return {
        "ctl": ctl,
        "atl": atl,
        "tsb": ctl - atl,
        "last_updated": state.get("last_updated"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    args = sys.argv[1:]
    seed_arg = next((a for a in args if a.startswith("--seed")), None)

    if seed_arg:
        # --seed YYYY-MM-DD,CTL,ATL
        value = seed_arg.split("=", 1)[-1] if "=" in seed_arg else args[args.index("--seed") + 1]
        parts = value.strip().split(",")
        if len(parts) != 3:
            print("Usage: python training_load.py --seed YYYY-MM-DD,CTL,ATL")
            sys.exit(1)
        seed_date, seed_ctl, seed_atl = parts[0].strip(), int(parts[1]), int(parts[2])
        seed(seed_date, seed_ctl, seed_atl)
        print(f"Seeded: date={seed_date} CTL={seed_ctl} ATL={seed_atl} TSB={seed_ctl - seed_atl}")
    else:
        tl = get_training_load()
        print(f"CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}  last_updated={tl['last_updated']}")
