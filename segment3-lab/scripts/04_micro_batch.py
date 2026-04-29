"""
04_micro_batch.py
=================
Simulates the "micro-batch" architecture from Slide 16 of Segment 3.

Real streaming systems use Kafka + Flink. Most enterprises don't need that
complexity. A simple loop that re-runs every 60 seconds on the last 5 minutes
of data delivers most of the value at a fraction of the operational cost.

This script demonstrates the pattern by replaying our 7-day metrics through
a 60-second-tick simulator and detecting anomalies in real time.

Reads:   data/metrics.csv
Prints:  alert lines as they occur
Writes:  output/streaming_alerts.csv

For demo speed we replay 7 days in ~5 seconds. In production the loop sleeps
for 60 seconds between ticks and pulls fresh data from a TSDB or message bus.
"""
import os
import sys
import time
import pandas as pd
import numpy as np
from datetime import timedelta

HERE        = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE  = os.path.join(HERE, "..", "data",   "metrics.csv")
OUTPUT_DIR  = os.path.join(HERE, "..", "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "streaming_alerts.csv")

WINDOW_MIN     = 5     # look at last 5 minutes
TICK_MIN       = 1     # advance 1 minute per iteration
DEMO_SLEEP_SEC = 0.0   # set to 60 in production; 0 makes the demo finish quickly
SATURATION_PCT = 90    # alert threshold for "approaching saturation"


def main():
    if not os.path.exists(INPUT_FILE):
        sys.exit(f"ERROR: {INPUT_FILE} not found. Run generate_sample_data.py first.")

    df = pd.read_csv(INPUT_FILE, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    series = df.set_index("timestamp")["utilization_pct"]

    start_t = series.index.min() + timedelta(minutes=WINDOW_MIN)
    end_t   = series.index.max()
    cursor  = start_t

    alerts = []
    print(f"Micro-batch loop starting "
          f"(window={WINDOW_MIN}m, tick={TICK_MIN}m)")
    print("-" * 72)

    while cursor <= end_t:
        win = series.loc[cursor - timedelta(minutes=WINDOW_MIN):cursor]
        if len(win) >= 2:
            mean_   = float(win.mean())
            max_    = float(win.max())
            stddev_ = float(win.std())

            triggered, reason = False, ""
            if max_ >= SATURATION_PCT:
                triggered, reason = True, f"saturation: max={max_:.1f}%"
            elif stddev_ > 25:
                # Sustained instability — like the Friday flap event.
                triggered, reason = True, f"instability: stddev={stddev_:.1f}"

            if triggered:
                alert = {
                    "fired_at":      cursor,
                    "window_mean":   round(mean_, 2),
                    "window_max":    round(max_, 2),
                    "window_stddev": round(stddev_, 2),
                    "reason":        reason,
                }
                alerts.append(alert)
                print(f"  ALERT  {cursor:%Y-%m-%d %H:%M}  "
                      f"mean={mean_:5.1f}%  max={max_:5.1f}%  "
                      f"std={stddev_:4.1f}  ->  {reason}")

        cursor += timedelta(minutes=TICK_MIN)
        if DEMO_SLEEP_SEC:
            time.sleep(DEMO_SLEEP_SEC)

    print("-" * 72)
    print(f"Loop complete. {len(alerts)} alert(s) fired.")

    out = pd.DataFrame(alerts)
    out.to_csv(OUTPUT_FILE, index=False)
    print(f"Wrote {OUTPUT_FILE}")
    print()
    print("Note: in a real deployment this loop would run forever, sleeping")
    print("60 seconds per tick, and write to a queue or alerting system.")


if __name__ == "__main__":
    main()
