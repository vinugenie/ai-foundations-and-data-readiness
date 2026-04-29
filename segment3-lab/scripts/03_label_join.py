"""
03_label_join.py
================
Demonstrates the labeling tactic from Slide 20 of Segment 3:
"Mine ticketing systems — join ticket timestamps to telemetry windows."

Inputs:
    data/tickets.csv            (sparse, human-filed labels)
    output/parsed_logs.csv      (structured logs from step 01)
    output/metric_anomalies.csv (z-score anomalies from step 02)

Output:
    output/labeled_events.csv

Each row in the output is one ticket joined to the events and anomalies that
occurred inside its time window. This is how you turn a handful of tickets into
labeled training data for a supervised model.

Real-world caveats — see Slide 19:
  * Filed-at timestamps are imprecise (humans round to 5 minutes).
  * Tickets may not even be network-related.
  * Multiple tickets can overlap; we keep the join simple here.
"""
import os
import sys
import pandas as pd
from datetime import timedelta

HERE        = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(HERE, "..", "data")
OUTPUT_DIR  = os.path.join(HERE, "..", "output")

TICKETS     = os.path.join(DATA_DIR,   "tickets.csv")
LOGS        = os.path.join(OUTPUT_DIR, "parsed_logs.csv")
ANOMALIES   = os.path.join(OUTPUT_DIR, "metric_anomalies.csv")

# Pad the ticket window because human timestamps are imprecise. This is the
# +/- 30 minutes Slide 20 mentions.
PADDING_MIN = 30


def main():
    for path in (TICKETS, LOGS, ANOMALIES):
        if not os.path.exists(path):
            sys.exit(f"ERROR: {path} missing. Run earlier scripts in order.")

    tickets = pd.read_csv(TICKETS,
                          parse_dates=["filed_at", "started_at", "resolved_at"])
    logs    = pd.read_csv(LOGS,      parse_dates=["timestamp"])
    anom    = pd.read_csv(ANOMALIES, parse_dates=["timestamp"])

    print(f"Loaded {len(tickets)} tickets, {len(logs):,} log rows, "
          f"{len(anom)} metric anomalies")
    print()

    # Filter to network-impacting tickets only — applying the 'category' field
    # as a coarse first filter is exactly what mature teams do.
    net_tickets = tickets[tickets["category"] == "Network-Link"].copy()
    print(f"  {len(net_tickets)} of {len(tickets)} tickets are network-link related")

    rows = []
    for _, t in net_tickets.iterrows():
        # Build the window with padding.
        win_start = t["started_at"]  - timedelta(minutes=PADDING_MIN)
        win_end   = t["resolved_at"] + timedelta(minutes=PADDING_MIN)

        # Logs for the same device that fall within the window.
        log_match = logs[
            (logs["device"]    == t["device"]) &
            (logs["timestamp"] >= win_start) &
            (logs["timestamp"] <= win_end) &
            (logs["event_type"] != "unmatched") &
            (logs["event_type"] != "config_change") &
            (logs["event_type"] != "acl_deny")
        ]

        # Metric anomalies for the same device + interface in the window.
        anom_match = anom[
            (anom["device"]    == t["device"])    &
            (anom["interface"] == t["interface"]) &
            (anom["timestamp"] >= win_start)      &
            (anom["timestamp"] <= win_end)
        ]

        rows.append({
            "ticket_id":         t["ticket_id"],
            "severity":          t["severity"],
            "device":            t["device"],
            "interface":         t["interface"],
            "window_start":      win_start,
            "window_end":        win_end,
            "log_event_count":   len(log_match),
            "distinct_event_types": log_match["event_type"].nunique(),
            "metric_anomaly_count": len(anom_match),
            "max_z_score":       float(anom_match["z_score"].abs().max())
                                 if len(anom_match) else 0.0,
            "label":             "incident" if (len(log_match) > 0 or len(anom_match) > 0)
                                 else "no_evidence",
        })

    out = pd.DataFrame(rows)
    print()
    print("Ticket-to-telemetry join")
    print("-" * 72)
    print(out.to_string(index=False))

    out_path = os.path.join(OUTPUT_DIR, "labeled_events.csv")
    out.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")
    print()
    print("Reading these results")
    print("-" * 72)
    print("  * Tickets with high event counts AND metric anomalies are gold-")
    print("    standard 'incident' labels — exactly what supervised models need.")
    print("  * Tickets with no_evidence are either filed against the wrong CI,")
    print("    have wrong timestamps, or were resolved before any telemetry")
    print("    showed it. In production these go to a manual review queue.")


if __name__ == "__main__":
    main()
