"""
CTL / ATL / TSB training-load tracker matching Suunto's algorithm.

State is stored in GCS as ``training_load.json`` (separate from metrics.json).

Formula — applied once per calendar day, floats kept throughout:
    Load = training_stress_score * 1.45  (from FIT session message)
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


def _recompute(state: dict[str, Any], target_date_str: str) -> tuple[float, float, int]:
    """
    Recompute CTL/ATL/TSB from the anchor through target_date_str.

    Uses the reseed anchor (if one exists and target_date >= reseed_date) so
    the app matches watch values by construction from that date forward.
    Falls back to the original April seed for historical targets.

    Returns (ctl_eod, atl_eod, tsb_morning) where:
    - ctl_eod / atl_eod are end-of-day floats after target_date's update
    - tsb_morning = lag-1 value captured before target_date's load/decay
    """
    target_d = date.fromisoformat(target_date_str)

    load_by_date: dict[str, float] = {}
    for a in state.get("activities", []):
        if a["date"] >= state["seed_date"]:
            load_by_date[a["date"]] = load_by_date.get(a["date"], 0.0) + a["load"]

    reseed_date_str = state.get("reseed_date")
    if reseed_date_str:
        reseed_d = date.fromisoformat(reseed_date_str)
        if reseed_d <= target_d:
            # Reseed anchor stores EoD values for reseed_date.
            ctl = float(state["reseed_ctl"])
            atl = float(state["reseed_atl"])
            # TSB for reseed_date: back-compute from EoD anchor (rest-day case).
            if load_by_date.get(reseed_date_str, 0.0) == 0:
                morning_tsb = round(ctl / _DECAY_CTL - atl / _DECAY_ATL)
            else:
                morning_tsb = round(ctl - atl)
            cur = reseed_d + timedelta(days=1)
            while cur <= target_d:
                ds = cur.isoformat()
                morning_tsb = round(ctl - atl)
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
            return ctl, atl, morning_tsb

    seed_d = date.fromisoformat(state["seed_date"])
    ctl = float(state["seed_ctl"])
    atl = float(state["seed_atl"])
    cur = seed_d
    morning_tsb = round(ctl - atl)
    while cur <= target_d:
        ds = cur.isoformat()
        morning_tsb = round(ctl - atl)  # before any update
        load = load_by_date.get(ds, 0.0)
        if load > 0:
            ctl += (load - ctl) * _K_CTL   # apply load
            atl += (load - atl) * _K_ATL
            ctl *= _DECAY_CTL               # overnight decay (Suunto two-step)
            atl *= _DECAY_ATL
        else:
            ctl *= _DECAY_CTL
            atl *= _DECAY_ATL
        cur += timedelta(days=1)

    return ctl, atl, morning_tsb


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
    Register a FIT upload's load. Idempotent — skips if the same date+load
    already exists (within 0.01). Returns True if a new entry was added.
    Use set_activity() when uploading FIT files to replace stale approximations.
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


def set_activity(date_str: str, load: float) -> bool:
    """
    Set the authoritative FIT-native load for a date, replacing ALL existing
    entries (including stale EPOC approximations). Unlike add_activity, this
    always produces exactly one entry per date. Returns True if anything changed.
    """
    state = _load_state()
    if not state.get("seed_date"):
        logger.warning("training_load: set_activity called before seed — ignoring")
        return False
    if date_str < state["seed_date"]:
        return False
    activities = state.setdefault("activities", [])
    load_f = round(float(load), 2)
    current = round(sum(a["load"] for a in activities if a["date"] == date_str), 2)
    if abs(current - load_f) < 0.01:
        return False  # already correct
    state["activities"] = [a for a in activities if a["date"] != date_str]
    state["activities"].append({"date": date_str, "load": load_f})
    state["activities"].sort(key=lambda a: a["date"])
    _save_state(state)
    logger.info("training_load: set activity date=%s load=%.2f (was %.2f)", date_str, load_f, current)
    return True


def reseed(date_str: str, ctl: float, atl: float) -> None:
    """
    Re-anchor to the watch's displayed EoD values for date_str.

    Stores {reseed_date, reseed_ctl, reseed_atl} alongside the original April
    seed. _recompute() and history_series() use this anchor for date_str and
    all future dates, while history before date_str continues to use the April
    seed. Call monthly (or whenever drift appears) to keep the app aligned with
    the watch without discarding backfill history.

    Args:
        date_str: ISO date the watch values were read, e.g. "2026-07-27"
        ctl:      Watch's displayed CTL for that date (EoD value)
        atl:      Watch's displayed ATL for that date (EoD value)
    """
    state = _load_state()
    state["reseed_date"] = date_str
    state["reseed_ctl"] = float(ctl)
    state["reseed_atl"] = float(atl)
    _save_state(state)
    logger.info("training_load reseeded at %s: CTL=%.1f ATL=%.1f", date_str, ctl, atl)


def history_series(target_date_str: str | None = None) -> dict[str, dict[str, float]]:
    """
    Return a per-day series {date_str: {ctl, atl, tsb}} from seed through
    target_date_str (defaults to today).

    If a reseed anchor exists:
    - Dates before reseed_date use the original April seed (historical charts).
    - reseed_date itself emits the anchor values directly.
    - Dates after reseed_date compute forward from the anchor (matches watch).
    """
    state = _load_state()
    if not state.get("seed_date"):
        return {}
    if target_date_str is None:
        from datetime import date as _date
        target_date_str = _date.today().isoformat()

    seed_d = date.fromisoformat(state["seed_date"])
    target_d = date.fromisoformat(target_date_str)

    load_by_date: dict[str, float] = {}
    for a in state.get("activities", []):
        if a["date"] >= state["seed_date"]:
            load_by_date[a["date"]] = load_by_date.get(a["date"], 0.0) + a["load"]

    out: dict[str, dict[str, float]] = {}

    def _run(start_ctl: float, start_atl: float, from_d: date, to_d: date) -> None:
        ctl, atl = start_ctl, start_atl
        cur = from_d
        while cur <= to_d:
            ds = cur.isoformat()
            tsb_morning = ctl - atl
            load = load_by_date.get(ds, 0.0)
            if load > 0:
                ctl += (load - ctl) * _K_CTL
                atl += (load - atl) * _K_ATL
                ctl *= _DECAY_CTL
                atl *= _DECAY_ATL
            else:
                ctl *= _DECAY_CTL
                atl *= _DECAY_ATL
            out[ds] = {"ctl": round(ctl, 2), "atl": round(atl, 2), "tsb": round(tsb_morning, 2)}
            cur += timedelta(days=1)

    reseed_date_str = state.get("reseed_date")
    reseed_d = date.fromisoformat(reseed_date_str) if reseed_date_str else None

    if reseed_d and reseed_d <= target_d:
        # Segment 1: original seed → day before reseed
        if reseed_d - timedelta(days=1) >= seed_d:
            _run(float(state["seed_ctl"]), float(state["seed_atl"]),
                 seed_d, reseed_d - timedelta(days=1))
        # Reseed day: emit anchor values; derive lag-1 TSB by back-computing from EoD anchor.
        r_ctl = float(state["reseed_ctl"])
        r_atl = float(state["reseed_atl"])
        if load_by_date.get(reseed_date_str, 0.0) == 0:
            tsb_reseed = round(r_ctl / _DECAY_CTL - r_atl / _DECAY_ATL, 2)
        else:
            tsb_reseed = round(r_ctl - r_atl, 2)
        out[reseed_date_str] = {"ctl": round(r_ctl, 2), "atl": round(r_atl, 2), "tsb": tsb_reseed}
        # Segment 2: reseed + 1 → target
        if reseed_d + timedelta(days=1) <= target_d:
            _run(r_ctl, r_atl, reseed_d + timedelta(days=1), target_d)
    else:
        _run(float(state["seed_ctl"]), float(state["seed_atl"]), seed_d, target_d)

    return out


def update_to_date(target_date_str: str) -> None:
    """
    Recompute CTL/ATL/TSB from the seed through target_date_str and persist.
    Always correct — replays from the seed each time so late FIT uploads are
    automatically included regardless of when this was last called.
    """
    state = _load_state()
    if not state.get("seed_date"):
        return

    ctl, atl, tsb = _recompute(state, target_date_str)
    state["ctl"] = ctl
    state["atl"] = atl
    state["tsb"] = tsb
    state["last_updated"] = target_date_str
    _save_state(state)
    logger.info("training_load updated to %s: CTL=%.2f ATL=%.2f TSB(morning)=%d",
                target_date_str, ctl, atl, tsb)


def get_training_load() -> dict[str, Any]:
    """Return current {ctl, atl, tsb, last_updated} — rounded at display time."""
    state = _load_state()
    ctl = state.get("ctl", 0.0)
    atl = state.get("atl", 0.0)
    tsb = state.get("tsb")  # pre-computed morning value; fall back if absent
    return {
        "ctl": round(float(ctl)),
        "atl": round(float(atl)),
        "tsb": int(tsb) if tsb is not None else round(float(ctl) - float(atl)),
        "last_updated": state.get("last_updated"),
    }


def registered_activity_dates() -> set[str]:
    """Return the set of dates for which load has been registered in training_load.json."""
    state = _load_state()
    return {a["date"] for a in state.get("activities", [])}


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

    elif "--reseed" in args:
        idx = args.index("--reseed")
        value = args[idx + 1] if idx + 1 < len(args) else ""
        parts = value.strip().split(",")
        if len(parts) != 3:
            print("Usage: python training_load.py --reseed YYYY-MM-DD,CTL,ATL")
            sys.exit(1)
        rd, rc, ra = parts[0].strip(), float(parts[1]), float(parts[2])
        reseed(rd, rc, ra)
        update_to_date(date.today().isoformat())
        tl = get_training_load()
        print(f"Reseeded: date={rd}  CTL={rc}  ATL={ra}")
        print(f"Current:  CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}")

    elif "--dump" in args:
        state = _load_state()
        print(f"Seed: date={state.get('seed_date')}  CTL={state.get('seed_ctl')}  ATL={state.get('seed_atl')}")
        if state.get("reseed_date"):
            print(f"Reseed anchor: date={state['reseed_date']}  CTL={state['reseed_ctl']}  ATL={state['reseed_atl']}")
        print(f"Current: CTL={state.get('ctl')}  ATL={state.get('atl')}  TSB={state.get('tsb')}  updated={state.get('last_updated')}")
        acts = state.get("activities", [])
        print(f"\nActivities in training_load.json ({len(acts)} entries):")
        by_date: dict[str, float] = {}
        for a in acts:
            by_date[a["date"]] = by_date.get(a["date"], 0.0) + a["load"]
        for ds in sorted(by_date):
            total_load = by_date[ds]
            tss_approx = round(total_load / 1.45, 1)
            entries = [a for a in acts if a["date"] == ds]
            detail = "  ".join(f"load={e['load']:.2f}" for e in entries)
            print(f"  {ds}: total_load={total_load:.2f}  TSS≈{tss_approx}  [{detail}]")

    elif "--fix-from-metrics" in args:
        # Repair training_load.json using native TSS values already stored in metrics.json.
        # Use set_activity (not add_activity) so stale EPOC loads are replaced, not summed.
        from storage import load_metrics as _load_metrics_gcs
        state = _load_state()
        if not state.get("seed_date"):
            print("ERROR: no seed set. Run --seed first.")
            sys.exit(1)
        print(f"Seed: date={state['seed_date']}  CTL={state['seed_ctl']}  ATL={state['seed_atl']}")

        db = _load_metrics_gcs()
        acts = db.get("activities", {})
        changed = 0
        skipped = 0
        for day in sorted(acts.keys()):
            if day <= state["seed_date"]:
                continue
            day_acts = acts[day]
            day_tss = sum(
                float(a["training_stress_score"])
                for a in day_acts
                if a.get("training_stress_score") is not None
            )
            if day_tss <= 0:
                skipped += 1
                continue
            load = round(day_tss * 1.45, 2)
            if set_activity(day, load):
                print(f"  {day}: UPDATED  load={load:.2f}  TSS={day_tss:.1f}")
                changed += 1
            else:
                print(f"  {day}: OK       load={load:.2f}  TSS={day_tss:.1f}")

        print(f"\n{changed} date(s) updated, {skipped} date(s) skipped (no TSS in metrics.json)")
        if changed or True:
            update_to_date(date.today().isoformat())
        tl = get_training_load()
        print(f"Result: CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}")

    elif "--validate" in args:
        # Validate GCS backfill: load all FIT files from GCS, compute series,
        # assert today's values match the watch (CTL≈32, ATL≈42, TSB≈-16 as of 2026-07-26).
        # Usage: python training_load.py --validate [YYYY-MM-DD]
        idx = args.index("--validate")
        target = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith("--") else date.today().isoformat()

        from storage import list_fit_dates_from_gcs, download_fit_from_gcs
        from fit_parser import parse_fit

        print(f"Validating GCS backfill through {target} ...")
        state = _load_state()
        if not state.get("seed_date"):
            print("ERROR: no seed set. Run --seed first.")
            sys.exit(1)
        print(f"Seed: date={state['seed_date']} CTL={state['seed_ctl']} ATL={state['seed_atl']}")

        fit_dates = list_fit_dates_from_gcs()
        stored_load: dict[str, float] = {}
        for a in state.get("activities", []):
            if a["date"] >= state["seed_date"]:
                stored_load[a["date"]] = stored_load.get(a["date"], 0.0) + a["load"]

        print(f"Checking {len(fit_dates)} GCS FIT file(s) against training_load.json ...")
        changed_any = False
        for fd in sorted(fit_dates):
            raw = download_fit_from_gcs(fd)
            if not raw:
                print(f"  {fd}: download failed")
                continue
            try:
                pf = parse_fit(raw)
                tss_val = pf.get("training_stress_score")
                if tss_val is None:
                    print(f"  {fd}: no TSS in FIT — skipped")
                    continue
                correct_load = round(float(tss_val) * 1.45, 2)
                current_load = round(stored_load.get(fd, 0.0), 2)
                if abs(current_load - correct_load) >= 0.01:
                    set_activity(fd, correct_load)
                    print(f"  {fd}: FIXED  TSS={tss_val:.1f} load={correct_load:.2f} (was {current_load:.2f})")
                    changed_any = True
                else:
                    print(f"  {fd}: OK     TSS={tss_val:.1f} load={correct_load:.2f}")
            except Exception as e:
                print(f"  {fd}: parse error — {e}")

        if changed_any:
            state = _load_state()  # reload after fixes

        update_to_date(target)
        tl = get_training_load()
        print(f"\nResult for {target}:")
        print(f"  CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}")
        print(f"  Expected: CTL≈32(±1)  ATL≈42(±1)  TSB≈-16(±2)")

        ok = (abs(tl['ctl'] - 32) <= 1 and abs(tl['atl'] - 42) <= 1 and abs(tl['tsb'] - (-16)) <= 2)
        if ok:
            print("PASS: within tolerance of watch values.")
        else:
            print("FAIL: outside tolerance — printing daily series:")
            series = history_series(target)
            for d in sorted(series.keys()):
                v = series[d]
                print(f"  {d}: CTL={v['ctl']:.2f} ATL={v['atl']:.2f} TSB={v['tsb']:.2f}")
        sys.exit(0 if ok else 1)

    else:
        tl = get_training_load()
        print(f"CTL={tl['ctl']}  ATL={tl['atl']}  TSB={tl['tsb']}  last_updated={tl['last_updated']}")
