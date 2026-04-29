"""
Segment 4 — Demo 3: The Data-Quality Detective
================================================

Loads sample_telemetry.csv (one week of hourly polls from a small fleet)
and runs four diagnostic checks, one for each pitfall covered in the
slides:

    1.  Missing data        — completeness dashboard
    2.  Sampling errors     — detect under-counted devices
    3.  Timestamp issues    — clock skew, gaps, alignment
    4.  Normalization       — unit drift, alias chaos

Each check prints a finding AND, where possible, suggests a remediation.

Run:
    python 03_data_quality_check.py --input sample_telemetry.csv
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def banner(text):
    line = "─" * len(text)
    print(f"\n{line}\n{text}\n{line}")


def load(path):
    df = pd.read_csv(path)
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)
    return df


# ---------------------------------------------------------------------------
# CHECK 1 — Missing data (completeness dashboard)
# ---------------------------------------------------------------------------
def check_completeness(df, expected_interval_min=60):
    """
    For each device, compute % of expected polls received per day.

    'Expected' = continuous polling at `expected_interval_min` minutes.
    Anything below 100% means we lost at least one poll that day.
    """
    banner("CHECK 1 — Missing-data / completeness dashboard")

    expected_per_day = int(24 * 60 / expected_interval_min)
    rows = []

    for dev, grp in df.groupby("device_id"):
        # Count polls per UTC date
        per_day = grp.set_index("timestamp_utc").resample("1D").size()
        for date, count in per_day.items():
            rows.append({
                "device_id": dev,
                "date": date.date(),
                "received": int(count),
                "expected": expected_per_day,
                "completeness_pct": round(100 * count / expected_per_day, 1),
            })

    completeness = pd.DataFrame(rows)
    bad = completeness[completeness["completeness_pct"] < 100]
    print(f"\nDays with incomplete polling (any device < 100% of expected polls):\n")
    if bad.empty:
        print("  none.")
    else:
        # Group by date and show which devices were affected
        for date, grp in bad.groupby("date"):
            print(f"  {date}  →  ", end="")
            shortfalls = [
                f"{r.device_id}: {int(r.received)}/{int(r.expected)} ({r.completeness_pct}%)"
                for r in grp.itertuples()
            ]
            print(", ".join(shortfalls[:3]) + (" ..." if len(shortfalls) > 3 else ""))

        # Are the gaps correlated across devices? (collector outage signature)
        gap_dates = bad["date"].value_counts()
        same_day_drops = gap_dates[gap_dates >= 3]
        if not same_day_drops.empty:
            print(f"\n  ⚠ CORRELATED GAP — {len(same_day_drops)} day(s) where 3+ devices "
                  f"all lost polls simultaneously.")
            print(f"    This is the fingerprint of a collector / pipeline outage,")
            print(f"    not a per-device fault.  Suspect: schema change, broker")
            print(f"    backpressure, scheduled maintenance window.")

        print("\n  → Recommendation: store nulls (don't forward-fill); add an")
        print("    `is_imputed` boolean feature for any model that consumes this.")


# ---------------------------------------------------------------------------
# CHECK 2 — Sampling errors
# ---------------------------------------------------------------------------
def check_sampling(df):
    """
    Detect devices whose `value` column is silently 1000× too low because
    nobody multiplied through the sampling rate.

    Heuristic: compare each device's median reported value to the median
    of its peers within the same vendor / role.  If sampling_rate > 1
    AND the value column is in the same numeric range as devices with
    sampling_rate=1, the multiply-through almost certainly never happened.
    """
    banner("CHECK 2 — Sampling-rate consistency")

    # Median per device
    medians = (df.groupby(["device_id", "sampling_rate"])["value"]
                 .median()
                 .reset_index()
                 .rename(columns={"value": "median_value"}))
    print("\nMedian reported value per device:")
    print(medians.to_string(index=False))

    sampled = medians[medians["sampling_rate"] > 1]
    unsampled = medians[medians["sampling_rate"] == 1]

    if sampled.empty:
        print("\n  no sampled devices in dataset.")
        return

    median_unsampled = unsampled["median_value"].median()
    print(f"\nReference median (sampling_rate=1 devices): {median_unsampled:,.0f}")

    print(f"\nSuspicious devices (sampled rate, but raw value still in unsampled range):")
    for _, row in sampled.iterrows():
        ratio = row["median_value"] / median_unsampled if median_unsampled else 0
        adjusted = row["median_value"] * row["sampling_rate"]
        adj_ratio = adjusted / median_unsampled if median_unsampled else 0
        flag = ""
        # Two failure modes:
        #   (1) raw value is ~1000× smaller than peers — multiply-through
        #       was forgotten; the after-multiply value matches peers.
        #   (2) raw value already matches peers — multiply-through was
        #       applied but the sampling_rate is also stored, so a downstream
        #       model might multiply AGAIN.
        if ratio < 0.1 and 0.2 <= adj_ratio <= 5.0:
            flag = "⚠ UNDER-COUNTED — multiply by sampling_rate to match peers"
        elif 0.2 <= ratio <= 5.0:
            flag = "⚠ AMBIGUOUS — value matches peers, risk of DOUBLE multiplication downstream"
        print(f"  {row['device_id']}  sampling_rate={int(row['sampling_rate']):>4}  "
              f"median={row['median_value']:>14,.0f}  "
              f"after-multiply={adjusted:>14,.0f}  {flag}")

    print("\n  → Recommendation: any model that consumes `value` must multiply")
    print("    by `sampling_rate` first, OR the pipeline must store a single")
    print("    canonical `bps` column with the multiplication already applied.")


# ---------------------------------------------------------------------------
# CHECK 3 — Timestamp issues
# ---------------------------------------------------------------------------
def check_timestamps(df, expected_interval_min=60):
    """
    Three sub-checks:

       (a) Per-device clock skew — do the second-of-minute values cluster
           around 0, or does some device consistently drift?

       (b) Inter-poll gaps — are there gaps significantly larger than
           expected_interval_min?  (DST transitions, collector restarts.)

       (c) Cross-device alignment — at any given polling minute, how many
           devices reported?  If consistently fewer than expected, the
           timestamp grid isn't aligned.
    """
    banner("CHECK 3 — Timestamp issues")

    # (a) Clock skew detector — for hourly polls we expect minute=0, second=0
    print("\n(a) Per-device clock alignment (expected: minute=0, second=0):")
    for dev, grp in df.groupby("device_id"):
        secs = grp["timestamp_utc"].dt.second
        mins = grp["timestamp_utc"].dt.minute
        median_sec = int(secs.median())
        median_min = int(mins.median())
        flag = "" if (median_sec == 0 and median_min == 0) else "⚠ DRIFT"
        print(f"  {dev}  median minute:second = {median_min:02d}:{median_sec:02d}   {flag}")
        if median_sec != 0 or median_min != 0:
            print(f"             → {dev}'s clock looks {median_sec}s "
                  f"{'fast' if median_sec > 30 else 'off'} from the rest of the fleet.")
            print(f"             → Re-check NTP on this device before correlating events.")

    # (b) Look for gaps > expected interval
    print(f"\n(b) Polling gaps larger than {expected_interval_min} minutes:")
    found_any_gap = False
    for dev, grp in df.groupby("device_id"):
        ts = grp["timestamp_utc"].sort_values().reset_index(drop=True)
        deltas = ts.diff().dt.total_seconds() / 60   # minutes
        gaps = deltas[deltas > expected_interval_min * 1.5]
        if not gaps.empty:
            found_any_gap = True
            for idx, gap_min in gaps.items():
                print(f"  {dev}  gap of {gap_min:.0f} min ending at "
                      f"{ts.iloc[idx]}  ({gap_min // 60:.0f}h missing)")
    if not found_any_gap:
        print("  none found.")
    else:
        print("\n  → Cross-reference these gaps with maintenance-window logs.")
        print("    Gaps that align with DST transitions are a normalization bug,")
        print("    not a real outage — store all timestamps in UTC to avoid this.")

    # (c) Alignment grid
    print("\n(c) Polling-grid alignment across devices:")
    grid = (df.assign(minute_bucket=df["timestamp_utc"].dt.floor("h"))
              .groupby("minute_bucket")["device_id"].nunique())
    n_devices = df["device_id"].nunique()
    aligned = (grid == n_devices).sum()
    total = len(grid)
    print(f"  {aligned} of {total} hourly buckets had ALL {n_devices} devices reporting "
          f"({100*aligned/total:.1f}%)")
    if aligned / total < 0.95:
        print("  ⚠ Devices are reporting on shifted grids — joins across devices")
        print("    will silently lose rows.  Use resample() to align before any")
        print("    cross-device correlation.")


# ---------------------------------------------------------------------------
# CHECK 4 — Normalization (units + identity)
# ---------------------------------------------------------------------------
def check_normalization(df):
    """
    Two sub-checks:

       (a) Unit drift — are some devices reporting kbps while others
           report bps?  Spot it by looking for devices whose median value
           is ~1000× off from peers of the same role.

       (b) Identity chaos — is the same canonical device reported under
           multiple aliases?  Are different devices using the same alias?
    """
    banner("CHECK 4 — Normalization issues (units + identity)")

    # (a) Units
    print("\n(a) Unit declaration vs reported magnitude:")
    units_summary = (df.groupby(["device_id", "vendor", "units"])["value"]
                       .median()
                       .reset_index())
    print(units_summary.to_string(index=False))

    # If any device reports in kbps while peers report in bps, flag it
    for unit in df["units"].unique():
        if unit == "bps":
            continue
        rogue = units_summary[units_summary["units"] == unit]
        if not rogue.empty:
            print(f"\n  ⚠ {len(rogue)} device(s) report in '{unit}' while the rest of the")
            print(f"    fleet reports in 'bps'.  Joining these without conversion")
            print(f"    produces silently-wrong rates.")
            print(f"    Affected: {', '.join(rogue['device_id'].tolist())}")
            print(f"    → Convert on ingest:  if units=='kbps': value *= 1000")

    # (b) Identity / alias chaos
    print("\n(b) Device-name (alias) consistency:")

    # Each canonical device should have exactly one normalized alias
    alias_counts = (df.groupby("device_id")["device_name"]
                      .nunique()
                      .sort_values(ascending=False))
    multi_alias = alias_counts[alias_counts > 1]
    if not multi_alias.empty:
        print(f"\n  ⚠ {len(multi_alias)} device(s) reported under multiple alias spellings:")
        for dev_id in multi_alias.index:
            aliases = sorted(df[df["device_id"] == dev_id]["device_name"].unique())
            print(f"    {dev_id}  →  {aliases}")

    # Each alias should map to exactly one canonical device
    alias_to_devs = (df.groupby("device_name")["device_id"]
                       .nunique()
                       .sort_values(ascending=False))
    shared_alias = alias_to_devs[alias_to_devs > 1]
    if not shared_alias.empty:
        print(f"\n  ⚠ {len(shared_alias)} alias(es) used by more than one device:")
        for alias in shared_alias.index:
            devs = sorted(df[df["device_name"] == alias]["device_id"].unique())
            print(f"    '{alias}'  →  used by {devs}")
        print(f"\n    Models that key on `device_name` will SILENTLY MERGE these")
        print(f"    distinct devices into one synthetic entity.")

    if multi_alias.empty and shared_alias.empty:
        print("  no naming chaos detected.")
    else:
        print("\n  → Recommendation: pick ONE canonical identifier (chassis serial,")
        print("    management IP, or an explicit `device_id` column) and resolve")
        print("    every alias to it on ingest.  Throw away the human-readable")
        print("    name for ML purposes — keep it only for dashboards.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="sample_telemetry.csv",
                   help="CSV produced by generate_sample_data.py")
    p.add_argument("--interval-min", type=int, default=60,
                   help="expected polling interval in minutes (default 60)")
    args = p.parse_args()

    if not Path(args.input).exists():
        print(f"input file not found: {args.input}", file=sys.stderr)
        print("run `python generate_sample_data.py` first.", file=sys.stderr)
        sys.exit(2)

    df = load(args.input)
    print(f"Loaded {len(df):,} rows  ({df['device_id'].nunique()} devices, "
          f"{df['timestamp_utc'].min()}  →  {df['timestamp_utc'].max()})")

    check_completeness(df, args.interval_min)
    check_sampling(df)
    check_timestamps(df, args.interval_min)
    check_normalization(df)

    print("\n" + "=" * 60)
    print("Done.  Review each section's recommendations before letting any")
    print("ML model touch this dataset.")
    print("=" * 60)


if __name__ == "__main__":
    main()
