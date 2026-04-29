"""
Segment 4 — Helper: Generate the messy sample telemetry dataset
================================================================

Builds a realistic-looking interface-counter dataset that has all FOUR
data-quality pitfalls deliberately injected:

    1. MISSING DATA      — collector restart blanks out 12 minutes
                            on Sunday at 02:00 every week.
    2. SAMPLING ERROR    — half the dataset is reported in raw bytes,
                            the other half is reported as packet
                            samples at 1-in-1000 — without normalization.
    3. TIMESTAMP ISSUES  — one device has clock skew (+47 sec offset);
                            a DST transition creates a 1-hour gap.
    4. NORMALIZATION     — Cisco devices report bps; one Juniper device
                            reports kbps; one device is named four
                            different ways across the dataset.

Run:
    python generate_sample_data.py --output sample_telemetry.csv

Produces a deterministic dataset thanks to a fixed RNG seed, so every
participant in the lab is solving the same puzzle.
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Fleet of devices we'll simulate.  Note the deliberate inconsistencies.
# ---------------------------------------------------------------------------
DEVICES = [
    # (canonical_id, vendor, alias_name, units,        ifindex)
    ("dev-001", "cisco",   "core-rtr-01",      "bps",   2),
    ("dev-002", "cisco",   "CORE-RTR-02",      "bps",   2),  # uppercase variant
    ("dev-003", "cisco",   "core-rtr-03.example.com", "bps", 2),  # FQDN
    ("dev-004", "juniper", "edge-rtr-01",      "kbps",  515),  # kbps drift
    ("dev-005", "arista",  "spine-01",         "bps",   3),
    ("dev-006", "cisco",   "core-rtr-01",      "bps",   2),  # SAME alias as dev-001
]

# Sampling rates per device.  Most devices are 1:1 (raw counters); two
# of them export sampled NetFlow at 1:1000 — the "rate" column will
# already have been multiplied incorrectly elsewhere, so values look
# 1000× too small.
SAMPLING_RATES = {
    "dev-001": 1,
    "dev-002": 1,
    "dev-003": 1000,    # 1:1000 sFlow — values silently 1000× under-counted
    "dev-004": 1,
    "dev-005": 1000,    # also under-counted
    "dev-006": 1,
}


def generate(start_time: datetime, hours: int = 168, seed: int = 7) -> pd.DataFrame:
    """Generate hourly polls for `hours` (default 168 = 1 week) per device."""
    rng = np.random.default_rng(seed)
    rows: list[dict] = []

    for canonical_id, vendor, alias, units, ifindex in DEVICES:
        # Pick a baseline: cisco core ~ 600 Mbps, juniper edge ~ 200 Mbps,
        # arista spine ~ 2 Gbps
        if "core" in alias.lower():
            base_bps = 600e6
        elif "edge" in alias.lower():
            base_bps = 200e6
        elif "spine" in alias.lower():
            base_bps = 2.0e9
        else:
            base_bps = 100e6

        for h in range(hours):
            ts = start_time + timedelta(hours=h)

            # Diurnal-ish workday pattern (UTC-ish; not exact)
            hour_of_day = ts.hour
            workday_factor = 0.6 if hour_of_day < 8 or hour_of_day > 19 else 1.4
            weekend_factor = 0.7 if ts.weekday() >= 5 else 1.0

            bps = base_bps * workday_factor * weekend_factor
            bps *= rng.normal(1.0, 0.10)               # 10% noise
            bps = max(bps, 0)

            # PITFALL 2 — sampling error.  Devices with sampling_rate=1000
            # under-report by 1000× because somebody upstream forgot to
            # multiply through.
            sampling_rate = SAMPLING_RATES[canonical_id]
            reported_bps = bps / sampling_rate

            # PITFALL 4 — units drift for Juniper device
            if units == "kbps":
                reported_value = reported_bps / 1000.0
            else:
                reported_value = reported_bps

            # PITFALL 4 (continued) — alias inconsistency: every 6 hours,
            # device 002's reported name flips between cases
            row_alias = alias
            if canonical_id == "dev-002" and h % 6 == 0:
                row_alias = "core-rtr-02"

            # PITFALL 3 — clock skew on dev-005 (47 seconds fast)
            reported_ts = ts
            if canonical_id == "dev-005":
                reported_ts = ts + timedelta(seconds=47)

            rows.append({
                "timestamp_utc": reported_ts.isoformat(timespec="seconds"),
                "device_id":     canonical_id,
                "device_name":   row_alias,
                "vendor":        vendor,
                "ifindex":       ifindex,
                "units":         units,
                "sampling_rate": sampling_rate,
                "value":         round(reported_value, 2),
            })

    df = pd.DataFrame(rows)

    # PITFALL 1 — missing data.  Collector restarted every Sunday 02:00 UTC,
    # losing 12 minutes of data for ALL devices.  Since we sample hourly,
    # the closest poll either falls inside the gap and is dropped, or
    # is skipped entirely.  We'll drop Sunday 02:00 polls completely.
    sundays_at_02 = (
        df["timestamp_utc"].str.contains("T02:") &
        pd.to_datetime(df["timestamp_utc"]).dt.weekday.eq(6)
    )
    n_dropped = int(sundays_at_02.sum())
    df = df[~sundays_at_02].reset_index(drop=True)

    # PITFALL 3 (continued) — DST jump.  Pretend the local collector
    # forgot to handle DST: drop ONE hour of polls in the middle of the
    # week (simulates a "spring-forward" gap).
    dst_gap_ts = (start_time + timedelta(days=3, hours=2))   # Wed 02:00 + h*1
    dst_window_start = dst_gap_ts
    dst_window_end   = dst_gap_ts + timedelta(hours=1)
    in_dst_gap = (
        (pd.to_datetime(df["timestamp_utc"]) >= dst_window_start) &
        (pd.to_datetime(df["timestamp_utc"]) <  dst_window_end)
    )
    df = df[~in_dst_gap].reset_index(drop=True)

    print(f"  injected pitfalls:")
    print(f"    [missing]       dropped {n_dropped} Sunday-02:00 rows")
    print(f"    [missing/DST]   dropped Wed 02:00–03:00 rows ({int(in_dst_gap.sum())} rows)")
    print(f"    [sampling]      dev-003 and dev-005 have sampling_rate=1000")
    print(f"                    but their `value` column was NOT multiplied through")
    print(f"    [clock skew]    dev-005 timestamps are +47 seconds")
    print(f"    [units]         dev-004 reports in kbps; everyone else in bps")
    print(f"    [aliases]       dev-002 alternates between 'CORE-RTR-02' and 'core-rtr-02'")
    print(f"                    dev-006 uses the SAME alias as dev-001 ('core-rtr-01')")

    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="sample_telemetry.csv")
    p.add_argument("--hours", type=int, default=168, help="hours of data (default 1 week)")
    args = p.parse_args()

    print("Generating messy sample telemetry dataset ...")
    start = datetime(2026, 4, 20, 0, 0, 0, tzinfo=timezone.utc)
    df = generate(start, hours=args.hours)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"\n  wrote {args.output} ({len(df)} rows, {df['device_id'].nunique()} devices)")
    print(f"  date range: {df['timestamp_utc'].min()}  →  {df['timestamp_utc'].max()}")


if __name__ == "__main__":
    main()
