"""
CTL / ATL / TSB training-load tracker that matches Suunto's integer-rounding formula.

State is stored in GCS as ``training_load.json`` (separate from metrics.json).

Formula — applied once per calendar day, rounding after every step:
    CTL = round(CTL + (tss - CTL) * (1 - exp(-1/42)))
    ATL = round(ATL + (tss - ATL) * (1 - exp(-1/7)))
    TSB = CTL - ATL

The seed (date, CTL, ATL) is stored separately from the running totals.
update_to_date() always recomputes forward from the seed using all stored
activities, so uploading a FIT for any past date is always correctly applied
regardless of when update_to_date() was last called.

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
            return {"seed_date": None, "seed_ctl": 0, "seed_atl": 0, "activities": []}
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as e:
        logger.error("training_load: GCS load failed: %s", e)
        return {"seed_date": None, "seed_ctl": 0, "seed_atl": 0, "activities": []}


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


def _recompute(state: dict[str, Any], target_date_str: str) -> tuple[int, int]:
    """
    Recompute CTL/ATL from the seed through target_date_str using all stored
    activities. Always starts fresh from the seed so the result is idempotent
    regardless of when activities were added relative to update_to_date calls.
    """
    seed_d = date.fromisoformat(state["seed_date"])
    target_d = date.fromisoformat(target_date_str)

    tss_by_date: dict[str, int] = {}
    for a in state.get("activities", []):
        if a["date"] > state["seed_date"]:
            tss_by_date[a["date"]] = tss_by_date.get(a["date"], 0) + a["tss"]

    ctl = state["seed_ctl"]
    atl = state["seed_atl"]
    cur = seed_d + timedelta(days=1)
    while cur <= target_d:
        ds = cur.isoformat()
        tss = tss_by_date.get(ds, 0)
        ctl = round(ctl + (tss - ctl) * _K_CTL)
        atl = round(atl + (tss - atl) * _K_ATL)
        cur += timedelta(days=1)

    return ctl, atl


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed(date_str: str, ctl: int, atl: int) -> None:
    """
    Bootstrap from known Suunto values. Discards any stored activities on or
    before date_str (already baked into the seed) and keeps later ones.

    Args:
        date_str: ISO date string representing the morning anchor, e.g. "2026-04-19"
        ctl: Suunto's current CTL (integer)
        atl: Suunto's current ATL (integer)
    """
    state = _load_state()
    state["seed_date"] = date_str
    state["seed_ctl"] = int(ctl)
    state["seed_atl"] = int(atl)
    # Drop activities on or before the seed — they're already reflected in the seed values.
    state["activities"] = [
        a for a in state.get("activities", [])
        if a["date"] > date_str
    ]
    _save_state(state)
    logger.info("training_load seeded: date=%s CTL=%d ATL=%d", date_str, ctl, atl)


def add_activity(date_str: str, tss: float) -> bool:
    """
    Register a FIT upload's TSS. Idempotent — skips if the same date+tss
    already exists. Returns True if a new entry was added.
    """
    state = _load_state()
    if not state.get("seed_date"):
        logger.warning("training_load: add_activity called before seed — ignoring")
        return False
    if date_str <= state["seed_date"]:
        logger.warning("training_load: activity on/before seed date %s ignored", state["seed_date"])
        return False
    activities = state.setdefault("activities", [])
    tss_int = round(float(tss))
    if any(a["date"] == date_str and a["tss"] == tss_int for a in activities):
        return False
    activities.append({"date": date_str, "tss": tss_int})
    activities.sort(key=lambda a: a["date"])
    _save_state(state)
    logger.info("training_load: added activity date=%s tss=%d", date_str, tss_int)
    return True


def update_to_date(target_date_str: str) -> None:
    """
    Recompute CTL/ATL/TSB from the seed through target_date_str and persist.
    Safe to call multiple times or after late FIT uploads — always gives the
    correct answer because it replays from the seed each time.
    """
    state = _load_state()
    if not state.get("seed_date"):
        return  # not seeded yet

    ctl, atl = _recompute(state, target_date_str)
    state["ctl"] = ctl
    state["atl"] = atl
    state["last_updated"] = target_date_str
    _save_state(state)
    logger.info("training_load updated to %s: CTL=%d ATL=%d TSB=%d",
                target_date_str, ctl, atl, ctl - atl)


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
        value = seed_arg.split("=", 1)[-1] if "=" in seed_arg else args[args.index("--seed") + 1]
        parts = value.strip().split(",")
        if len(parts) != 3:
            print("Usage: python training_load.py --seed YYYY-MM-DD,CTL,ATL")
            sys.exit(1)
        sd, sc, sa = parts[0].strip(), int(parts[1]), int(parts[2])
        seed(sd, sc, sa)
        print(f"Seeded: date={sd} CTL={sc} ATL={sa} TSB={sc - sa}")
    else:
        tl = get_training_load()
        print(f"CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}  last_updated={tl['last_updated']}")
