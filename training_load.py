"""
CTL / ATL / TSB training-load tracker matching Suunto's algorithm.

State is stored in GCS as ``training_load.json`` (separate from metrics.json).

Formula — applied once per calendar day, floats kept throughout:
    Load = training_stress_score * 1.4  (from FIT session message)
    k_ctl   = 1 - exp(-1/42)
    k_atl   = 1 - exp(-1/7)
    decay_ctl = exp(-1/42)
    decay_atl = exp(-1/7)

    Activity day:
        ctl += (load - ctl) * k_ctl   # apply load
        atl += (load - atl) * k_atl
        ctl *= decay_ctl               # overnight decay
        atl *= decay_atl

    Rest day:
        ctl *= decay_ctl               # overnight decay only
        atl *= decay_atl

    Multiple activities on the same day: sum their load before applying.
    Rounding happens only at display time in get_training_load().

The seed (date, CTL, ATL) is stored as the anchor; update_to_date() always
recomputes forward from the seed using all stored activities so late FIT
uploads are always correctly applied regardless of prior Sync calls.

Usage from other modules:
    from training_load import seed, add_activity, update_to_date, get_training_load

CLI:
    python training_load.py --seed 2026-04-20,41.0,43.0
    python training_load.py --status
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

_K_CTL    = 1.0 - exp(-1.0 / 42.0)
_K_ATL    = 1.0 - exp(-1.0 / 7.0)
_DECAY_CTL = exp(-1.0 / 42.0)
_DECAY_ATL = exp(-1.0 / 7.0)


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
            return {"seed_date": None, "seed_ctl": 0.0, "seed_atl": 0.0, "activities": []}
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as e:
        logger.error("training_load: GCS load failed: %s", e)
        return {"seed_date": None, "seed_ctl": 0.0, "seed_atl": 0.0, "activities": []}


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


def _recompute(state: dict[str, Any], target_date_str: str) -> tuple[float, float]:
    """
    Recompute CTL/ATL from the seed through target_date_str using all stored
    activities. Always starts fresh from the seed so the result is idempotent
    regardless of when activities were added relative to update_to_date calls.

    Activity day: apply load then overnight decay.
    Rest day: overnight decay only.
    """
    seed_d = date.fromisoformat(state["seed_date"])
    target_d = date.fromisoformat(target_date_str)

    load_by_date: dict[str, float] = {}
    for a in state.get("activities", []):
        if a["date"] >= state["seed_date"]:
            load_by_date[a["date"]] = load_by_date.get(a["date"], 0.0) + a["load"]

    ctl = float(state["seed_ctl"])
    atl = float(state["seed_atl"])
    cur = seed_d
    while cur <= target_d:
        ds = cur.isoformat()
        load = load_by_date.get(ds, 0.0)
        if load > 0:
            ctl += (load - ctl) * _K_CTL
            atl += (load - atl) * _K_ATL
            ctl *= _DECAY_CTL
            atl *= _DECAY_ATL
        else:
            ctl *= _DECAY_CTL
            atl *= _DECAY_ATL
        cur += timedelta(days=1)

    return ctl, atl


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def seed(date_str: str, ctl: float, atl: float) -> None:
    """
    Bootstrap from known Suunto values. Keeps activities after date_str so
    they will be replayed correctly on the next update_to_date() call.

    Args:
        date_str: ISO date string, e.g. "2026-04-20"
        ctl: Suunto's CTL as a float, e.g. 41.0
        atl: Suunto's ATL as a float, e.g. 43.0
    """
    state = _load_state()
    state["seed_date"] = date_str
    state["seed_ctl"] = float(ctl)
    state["seed_atl"] = float(atl)
    state["activities"] = [
        a for a in state.get("activities", [])
        if a["date"] >= date_str
    ]
    _save_state(state)
    logger.info("training_load seeded: date=%s CTL=%.1f ATL=%.1f", date_str, ctl, atl)


def add_activity(date_str: str, load: float) -> bool:
    """
    Register a FIT upload's load (= peak_epoc / 1.1). Idempotent — skips if
    the same date+load already exists. Returns True if a new entry was added.
    """
    state = _load_state()
    if not state.get("seed_date"):
        logger.warning("training_load: add_activity called before seed — ignoring")
        return False
    if date_str < state["seed_date"]:
        logger.warning("training_load: activity on/before seed date %s ignored", state["seed_date"])
        return False
    activities = state.setdefault("activities", [])
    load_f = float(load)
    if any(a["date"] == date_str and abs(a["load"] - load_f) < 0.01 for a in activities):
        return False
    activities.append({"date": date_str, "load": round(load_f, 2)})
    activities.sort(key=lambda a: a["date"])
    _save_state(state)
    logger.info("training_load: added activity date=%s load=%.2f", date_str, load_f)
    return True


def update_to_date(target_date_str: str) -> None:
    """
    Recompute CTL/ATL/TSB from the seed through target_date_str and persist.
    Always correct — replays from the seed each time so late FIT uploads are
    automatically included regardless of when this was last called.
    """
    state = _load_state()
    if not state.get("seed_date"):
        return

    ctl, atl = _recompute(state, target_date_str)
    state["ctl"] = ctl
    state["atl"] = atl
    state["last_updated"] = target_date_str
    _save_state(state)
    logger.info("training_load updated to %s: CTL=%.2f ATL=%.2f TSB=%.2f",
                target_date_str, ctl, atl, ctl - atl)


def get_training_load() -> dict[str, Any]:
    """Return current {ctl, atl, tsb, last_updated} — rounded at display time."""
    state = _load_state()
    ctl = state.get("ctl", 0.0)
    atl = state.get("atl", 0.0)
    return {
        "ctl": round(float(ctl)),
        "atl": round(float(atl)),
        "tsb": round(float(ctl) - float(atl)),
        "last_updated": state.get("last_updated"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    args = sys.argv[1:]

    if "--seed" in args:
        idx = args.index("--seed")
        value = args[idx + 1] if idx + 1 < len(args) else ""
        parts = value.strip().split(",")
        if len(parts) != 3:
            print("Usage: python training_load.py --seed YYYY-MM-DD,CTL,ATL")
            sys.exit(1)
        sd, sc, sa = parts[0].strip(), float(parts[1]), float(parts[2])
        seed(sd, sc, sa)
        print(f"Seeded: date={sd} CTL={sc} ATL={sa} TSB={round(sc - sa)}")

    elif "--status" in args:
        tl = get_training_load()
        print(f"CTL: {tl['ctl']} | ATL: {tl['atl']} | TSB: {tl['tsb']} | Last updated: {tl['last_updated']}")

    else:
        tl = get_training_load()
        print(f"CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}  last_updated={tl['last_updated']}")
