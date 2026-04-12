"""
One-time script to seed running-coach data/metrics.json with historical
CTL/ATL/TSB/HRV/sleep data AND backfill TRIMP into activities from Excel.

Run from the running-coach directory:
  python3 seed_historical.py
"""

import json
import os
import pandas as pd

EXCEL_PATH = os.path.join(os.path.dirname(__file__), "running-coach_data.xlsx")
METRICS_PATH = os.path.join(os.path.dirname(__file__), "data", "metrics.json")


def extract_excel_data(path: str) -> dict:
    df = pd.read_excel(path, sheet_name=0, header=None)

    dates_row = df.iloc[7]
    ctl_row   = df.iloc[14]
    atl_row   = df.iloc[15]
    tsb_row   = df.iloc[16]
    hrv_row   = df.iloc[11]
    sleep_dur = df.iloc[21]
    sleep_q   = df.iloc[22]
    trimp_row = df.iloc[40]
    tss_row   = df.iloc[35]  # TSS per activity — use as TRIMP proxy where TRIMP is missing

    records = {}
    for col in range(2, 113):
        date_val = dates_row[col]
        if pd.isna(date_val):
            continue
        try:
            d = pd.Timestamp(date_val).strftime('%Y-%m-%d')
        except Exception:
            continue

        rec = {}

        # CTL / ATL / TSB
        for field, row in [('ctl', ctl_row), ('atl', atl_row), ('tsb', tsb_row),
                            ('hrv_last', hrv_row), ('sleep_quality', sleep_q)]:
            v = row[col]
            if pd.notna(v):
                try:
                    rec[field] = round(float(v), 2)
                except Exception:
                    pass

        # TRIMP — prefer explicit TRIMP, fall back to TSS
        trimp_v = trimp_row[col]
        tss_v   = tss_row[col]
        if pd.notna(trimp_v):
            try:
                rec['trimp'] = round(float(trimp_v), 1)
            except Exception:
                pass
        elif pd.notna(tss_v):
            try:
                rec['trimp'] = round(float(tss_v), 1)
            except Exception:
                pass

        # Sleep duration → minutes
        v = sleep_dur[col]
        if pd.notna(v):
            try:
                t = pd.Timestamp(v)
                mins = t.hour * 60 + t.minute
                if mins > 0:
                    rec['sleep_duration_min'] = mins
            except Exception:
                pass

        if rec:
            records[d] = rec

    return records


def load_db(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_db(path: str, db: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(db, f, indent=2, default=str)


def main():
    print(f"Reading Excel: {EXCEL_PATH}")
    historical = extract_excel_data(EXCEL_PATH)
    print(f"Extracted {len(historical)} dates from Excel")

    print(f"Loading existing DB: {METRICS_PATH}")
    db = load_db(METRICS_PATH)
    metrics = db.setdefault("metrics", {})
    activities = db.setdefault("activities", {})

    # 1. Merge metrics (CTL/ATL/TSB/HRV/sleep)
    # Excel values are ground truth — always overwrite CTL/ATL/TSB/HRV/sleep
    # so that a stale pipeline run can't permanently corrupt the seeded history.
    added = updated = 0
    for d, rec in historical.items():
        if d not in metrics:
            metrics[d] = {k: v for k, v in rec.items() if k != 'trimp'}
            added += 1
        else:
            before = dict(metrics[d])
            for k, v in rec.items():
                if k != 'trimp':
                    metrics[d][k] = v  # Excel always wins
            if metrics[d] != before:
                updated += 1

    # 2. Backfill TRIMP into activities
    # For dates where we have TRIMP from Excel but no activity with TRIMP in DB,
    # inject a synthetic activity record so ctl_atl_tsb_series() gets real load values
    trimp_injected = 0
    for d, rec in historical.items():
        trimp = rec.get('trimp')
        if trimp is None or trimp == 0:
            continue

        existing = activities.get(d, [])
        # Check if any existing activity already has a real TRIMP
        has_trimp = any(
            a.get('trimp') is not None and float(a.get('trimp', 0)) > 0
            for a in existing
        )
        if has_trimp:
            continue

        # Inject synthetic activity with TRIMP from Excel
        existing.append({
            "id": f"excel-seed-{d}",
            "title": "Historical (Excel seed)",
            "sport": "running",
            "date": d,
            "trimp": trimp,
            "source": "excel_seed"
        })
        activities[d] = existing
        trimp_injected += 1

    # 3. Set seed CTL/ATL from earliest Excel date (warm-start the Banister filter)
    #    Also record the last Excel date so the pipeline knows where ground-truth ends.
    sorted_dates = sorted(historical.keys())
    first_d = sorted_dates[0]
    first = historical[first_d]
    last_d = sorted_dates[-1]
    meta = db.setdefault("meta", {})
    if "ctl" in first and "atl" in first:
        meta["seed_ctl"] = first["ctl"]
        meta["seed_atl"] = first["atl"]
        print(f"Seed set: CTL={first['ctl']}, ATL={first['atl']} from {first_d}")
    meta["last_excel_seed_date"] = last_d
    print(f"last_excel_seed_date set to {last_d}")

    save_db(METRICS_PATH, db)

    print(f"\nMetrics: added={added}, updated={updated}, total={len(metrics)}")
    print(f"TRIMP backfilled into {trimp_injected} activity days")
    print(f"\nLatest 5 dates:")
    for d in sorted(historical.keys())[-5:]:
        m = historical[d]
        print(f"  {d}: CTL={m.get('ctl')} ATL={m.get('atl')} TSB={m.get('tsb')} TRIMP={m.get('trimp')} HRV={m.get('hrv_last')}")


if __name__ == "__main__":
    main()
