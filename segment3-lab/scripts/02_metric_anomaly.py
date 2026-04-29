"""
02_metric_anomaly.py
====================
Reads the structured metrics time series and runs a simple, explainable
anomaly detector on it.

Demonstrates Slide 8 (metrics are the ML-friendly pillar) and Slide 16
(batch is the right architecture for this kind of analysis).

Method: rolling-baseline z-score by hour-of-week. We learn what each (weekday,
hour) bucket "normally" looks like, then flag points that deviate by more than
3 standard deviations from that bucket's mean.

Why this method? It is:
  1. Explainable  — every alert says exactly what was unusual.
  2. Cheap        — a single Pandas groupby; no GPU required.
  3. Resilient    — captures both diurnal and weekly seasonality.

Reads:   data/metrics.csv
Writes:  output/metric_anomalies.csv
         output/metric_chart.png
"""
import os
import sys
import pandas as pd
import numpy as np
import matplotlib

matplotlib.use("Agg")  # headless rendering for the lab environment
import matplotlib.pyplot as plt

HERE       = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(HERE, "..", "data", "metrics.csv")
OUTPUT_DIR = os.path.join(HERE, "..", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

Z_THRESHOLD = 3.0


def main():
    if not os.path.exists(INPUT_FILE):
        sys.exit(f"ERROR: {INPUT_FILE} not found. Run generate_sample_data.py first.")

    # 1. Load. CSV is the canonical "structured" format from Slide 10.
    df = pd.read_csv(INPUT_FILE, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    print(f"Loaded {len(df):,} metric points "
          f"({df['timestamp'].min()} -> {df['timestamp'].max()})")

    # 2. Build the per-(weekday, hour) baseline.
    df["weekday"] = df["timestamp"].dt.weekday
    df["hour"]    = df["timestamp"].dt.hour

    bucket_stats = (
        df.groupby(["weekday", "hour"])["utilization_pct"]
          .agg(["mean", "std"])
          .rename(columns={"mean": "bucket_mean", "std": "bucket_std"})
          .reset_index()
    )
    df = df.merge(bucket_stats, on=["weekday", "hour"], how="left")

    # 3. Z-score against the bucket the point belongs to.
    # Guard against zero std (constant buckets) so we never divide by zero.
    safe_std = df["bucket_std"].replace(0, np.nan)
    df["z_score"]    = (df["utilization_pct"] - df["bucket_mean"]) / safe_std
    df["is_anomaly"] = df["z_score"].abs() > Z_THRESHOLD

    n_anom = int(df["is_anomaly"].sum())
    print(f"Flagged {n_anom} points above |z| > {Z_THRESHOLD}")

    # 4. Save anomalies-only CSV for the labeling step downstream.
    anom_cols = ["timestamp", "device", "interface",
                 "utilization_pct", "bucket_mean", "z_score"]
    anom_df = df.loc[df["is_anomaly"], anom_cols].copy()
    anom_path = os.path.join(OUTPUT_DIR, "metric_anomalies.csv")
    anom_df.to_csv(anom_path, index=False)
    print(f"Wrote {anom_path}")

    # 5. A picture is worth a thousand words. Plot the whole week with
    #    anomalies highlighted.
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(df["timestamp"], df["utilization_pct"],
            color="#1C7293", linewidth=0.6, label="Utilization")
    ax.scatter(anom_df["timestamp"], anom_df["utilization_pct"],
               color="#B23A48", s=20, zorder=3, label=f"Anomaly (|z|>{Z_THRESHOLD})")

    ax.set_title("WAN interface utilization — rtr-bom-01 Gi0/1", fontsize=12)
    ax.set_ylabel("Utilization (%)")
    ax.set_ylim(0, 105)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")
    fig.tight_layout()

    chart_path = os.path.join(OUTPUT_DIR, "metric_chart.png")
    fig.savefig(chart_path, dpi=110)
    print(f"Wrote {chart_path}")


if __name__ == "__main__":
    main()
