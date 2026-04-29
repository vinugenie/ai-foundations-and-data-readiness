"""
Segment 4 — Demo 2: The Aggregation Problem
============================================

Demonstrates the slide-deck claim that 5-minute SNMP averaging hides
microbursts.  Generates one hour of synthetic 1-second interface
counters with embedded microbursts, then aggregates the same data at
four different resolutions and plots them side-by-side.

The point: aggregation is irreversible.  Once you've stored only
5-minute averages, no model can ever recover the per-second peaks.

Run:
    python 02_aggregation_demo.py --output aggregation_demo.png

The script writes:
    * raw_1s_traffic.csv          — the underlying "ground truth"
    * aggregation_demo.png        — comparison plot
    * aggregation_summary.txt     — numeric summary
"""

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # headless backend
import matplotlib.pyplot as plt


def generate_one_hour_of_traffic(seed=20250428):
    """
    Produce a DataFrame with one row per SECOND for 60 minutes:
        timestamp, bps

    Mean baseline is ~4 Gbps with a slow diurnal-style sine wave.
    Microbursts: 25-second-long surges to ~9.6 Gbps (95%+ of a 10G link)
    happen ~6 times per hour, randomly placed.
    """
    rng = np.random.default_rng(seed)
    n_sec = 60 * 60                                  # 3600 samples
    t = pd.date_range("2026-04-28 14:00:00", periods=n_sec, freq="1s", tz="UTC")

    # Smooth baseline ~4 Gbps with mild oscillation
    baseline = 4.0e9 + 0.4e9 * np.sin(np.linspace(0, 4 * math.pi, n_sec))

    # Per-second jitter (Gaussian, sigma = 250 Mbps)
    noise = rng.normal(0, 0.25e9, n_sec)
    bps = baseline + noise

    # Inject 6 microbursts.  Each lasts 20–30 seconds and peaks at 9–9.7 Gbps.
    burst_starts = rng.choice(np.arange(60, n_sec - 60), size=6, replace=False)
    for start in burst_starts:
        length = int(rng.integers(20, 30))
        peak = rng.uniform(9.0e9, 9.7e9)
        # Triangular ramp up / ramp down so each burst has an obvious shape
        for k in range(length):
            shape = 1.0 - abs(k - length / 2) / (length / 2)   # 0..1..0
            bps[start + k] = max(bps[start + k], peak * shape + baseline[start + k] * (1 - shape))

    bps = np.clip(bps, 0, None)                      # no negative traffic

    df = pd.DataFrame({"timestamp": t, "bps": bps}).set_index("timestamp")
    return df, burst_starts


def aggregate(df, freq):
    """Resample bps to a coarser frequency, taking the MEAN (the SNMP default)."""
    return df["bps"].resample(freq).mean().to_frame("bps")


def aggregate_minmaxmean(df, freq):
    """Resample but keep min, max, mean — what we *should* be storing."""
    return df["bps"].resample(freq).agg(["min", "mean", "max"])


def main(output_png):
    print("Generating 1 hour of synthetic 1-second interface counters ...")
    df_1s, burst_starts = generate_one_hour_of_traffic()

    df_1s.to_csv("raw_1s_traffic.csv")
    print(f"  wrote raw_1s_traffic.csv ({len(df_1s)} rows)")

    # Aggregate the SAME data at three coarser resolutions, mean only
    df_10s = aggregate(df_1s, "10s")
    df_60s = aggregate(df_1s, "60s")
    df_5m  = aggregate(df_1s, "5min")

    # Plot all four on the same axes
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharey=True)
    fig.suptitle(
        "Same hour of traffic, four polling resolutions — peaks vanish as the bucket grows",
        fontsize=13, fontweight="bold"
    )

    panels = [
        (axes[0, 0], df_1s,  "1-second  (gNMI streaming)",            "#0891B2"),
        (axes[0, 1], df_10s, "10-second  (high-rate SNMP)",           "#059669"),
        (axes[1, 0], df_60s, "1-minute  (typical SNMP)",              "#F59E0B"),
        (axes[1, 1], df_5m,  "5-minute  (legacy SNMP / MRTG default)","#B91C1C"),
    ]

    for ax, frame, label, color in panels:
        ax.plot(frame.index, frame["bps"] / 1e9, color=color, linewidth=1.0)
        ax.axhline(9.5, ls="--", color="grey", lw=0.8)
        ax.text(frame.index[10], 9.55, "10G saturation (~9.5 Gbps)",
                fontsize=8, color="grey")
        ax.set_title(label, fontsize=11)
        ax.set_ylabel("Gbps")
        ax.set_ylim(0, 10.5)
        ax.grid(alpha=0.25)

    for ax in axes[1, :]:
        ax.set_xlabel("Time (UTC)")

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(output_png, dpi=130)
    print(f"  wrote {output_png}")

    # Also write a numeric summary so the takeaway is unambiguous
    raw_peak_gbps = df_1s["bps"].max() / 1e9
    p99_1s_gbps   = df_1s["bps"].quantile(0.99) / 1e9
    bursts_over_9 = int((df_1s["bps"] > 9.0e9).sum())

    with open("aggregation_summary.txt", "w") as f:
        f.write("Aggregation comparison — what each resolution would tell you\n")
        f.write("=" * 64 + "\n\n")
        f.write(f"Number of microbursts injected            : 6\n")
        f.write(f"Total seconds above 9.0 Gbps (>90% util)  : {bursts_over_9}\n\n")

        for label, frame in [("1-second  raw      ", df_1s),
                             ("10-second average  ", df_10s),
                             ("1-minute  average  ", df_60s),
                             ("5-minute  average  ", df_5m)]:
            peak = frame["bps"].max() / 1e9
            mean = frame["bps"].mean() / 1e9
            seen_over_9 = int((frame["bps"] > 9.0e9).sum())
            f.write(f"{label}  peak: {peak:5.2f} Gbps   "
                    f"mean: {mean:4.2f} Gbps   "
                    f"buckets > 9 Gbps: {seen_over_9}\n")

        f.write("\n")
        f.write("Lesson:  the same physical hour of traffic looks like a healthy\n")
        f.write("link at 5-minute resolution, and like a saturated link at\n")
        f.write("1-second resolution.  The data didn't change — the bucket did.\n")

    print("  wrote aggregation_summary.txt\n")
    print(open("aggregation_summary.txt").read())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Segment 4 Demo 2 — aggregation problem")
    p.add_argument("--output", default="aggregation_demo.png",
                   help="output PNG path")
    args = p.parse_args()
    main(args.output)
