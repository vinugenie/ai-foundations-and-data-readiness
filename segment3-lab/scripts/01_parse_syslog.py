"""
01_parse_syslog.py
==================
Parses semi-structured syslog text into a structured Pandas DataFrame.

Demonstrates Slide 11/13 from Segment 3:
    "The cost of semi-structured data is paid in regex."

Reads:   data/syslog.txt
Writes:  output/parsed_logs.csv

Each output row has the same fixed schema regardless of which vendor produced
the log line — that uniform schema is what makes the data usable downstream.
"""
import re
import os
import sys
import pandas as pd
from datetime import datetime

HERE       = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(HERE, "..", "data", "syslog.txt")
OUTPUT_DIR = os.path.join(HERE, "..", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Vendor patterns. Each entry produces a normalized event_type plus interface
# and (if relevant) a peer.  This is the parsing layer that costs 30-60% of
# pipeline engineering effort on a real project.
# ---------------------------------------------------------------------------
PATTERNS = [
    # --- Cisco IOS ----------------------------------------------------------
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%LINK-3-UPDOWN: Interface (\S+?), changed state to (up|down)"),
        "extract":  lambda m: {
            "event_type": "link_state_change",
            "interface":  m.group(1).rstrip(","),
            "new_state":  m.group(2),
            "peer":       None,
        },
    },
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%LINEPROTO-5-UPDOWN: Line protocol on Interface (\S+?), changed state to (up|down)"),
        "extract":  lambda m: {
            "event_type": "lineproto_change",
            "interface":  m.group(1).rstrip(","),
            "new_state":  m.group(2),
            "peer":       None,
        },
    },
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%BGP-5-ADJCHANGE: neighbor (\S+) (Up|Down)"),
        "extract":  lambda m: {
            "event_type": "bgp_adjacency_change",
            "interface":  None,
            "new_state":  m.group(2).lower(),
            "peer":       m.group(1),
        },
    },
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%OSPF-5-ADJCHG: .*Nbr (\S+) on (\S+)"),
        "extract":  lambda m: {
            "event_type": "ospf_adjacency_change",
            "interface":  m.group(2),
            "new_state":  "down",
            "peer":       m.group(1),
        },
    },
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%PM-4-ERR_DISABLE: link-flap error detected on (\S+)"),
        "extract":  lambda m: {
            "event_type": "err_disable",
            "interface":  m.group(1),
            "new_state":  "err-disable",
            "peer":       None,
        },
    },
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%SEC-6-IPACCESSLOGP"),
        "extract":  lambda m: {
            "event_type": "acl_deny",
            "interface":  None,
            "new_state":  None,
            "peer":       None,
        },
    },
    {
        "vendor":   "cisco",
        "regex":    re.compile(r"%SYS-5-CONFIG_I"),
        "extract":  lambda m: {
            "event_type": "config_change",
            "interface":  None,
            "new_state":  None,
            "peer":       None,
        },
    },

    # --- Juniper JUNOS ------------------------------------------------------
    {
        "vendor":   "juniper",
        "regex":    re.compile(r"SNMP_TRAP_LINK_(UP|DOWN): ifIndex \d+, ifName (\S+),"),
        "extract":  lambda m: {
            "event_type": "link_state_change",
            "interface":  m.group(2),
            "new_state":  m.group(1).lower(),
            "peer":       None,
        },
    },

    # --- Arista EOS ---------------------------------------------------------
    {
        "vendor":   "arista",
        "regex":    re.compile(r"%LINEPROTO-5-UPDOWN: Line protocol on Interface (\S+), changed state to (up|down)"),
        "extract":  lambda m: {
            "event_type": "lineproto_change",
            "interface":  m.group(1),
            "new_state":  m.group(2),
            "peer":       None,
        },
    },
]

TIMESTAMP_RE = re.compile(r"^([A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}:\d{2})\s+(\S+)\s*:\s*(.*)$")


def parse_line(line: str) -> dict | None:
    m = TIMESTAMP_RE.match(line)
    if not m:
        return None
    ts_str, host, body = m.group(1), m.group(2), m.group(3)
    # Real-world note: syslog timestamps lack a year — assume current year.
    # In production, derive the year from the file's modified time or from a
    # received-time header.
    year = 2026
    try:
        ts = datetime.strptime(f"{year} {ts_str}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None

    for pat in PATTERNS:
        m2 = pat["regex"].search(body)
        if m2:
            row = {
                "timestamp":  ts,
                "device":     host,
                "vendor":     pat["vendor"],
                "raw":        body,
            }
            row.update(pat["extract"](m2))
            return row

    # Unmatched logs are tracked too — the unmatched rate is itself a quality
    # metric your team should monitor over time.
    return {
        "timestamp":  ts,
        "device":     host,
        "vendor":     "unknown",
        "event_type": "unmatched",
        "interface":  None,
        "new_state":  None,
        "peer":       None,
        "raw":        body,
    }


def main():
    if not os.path.exists(INPUT_FILE):
        sys.exit(f"ERROR: {INPUT_FILE} not found. Run generate_sample_data.py first.")

    rows = []
    with open(INPUT_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parsed = parse_line(line)
            if parsed is not None:
                rows.append(parsed)

    df = pd.DataFrame(rows)

    total = len(df)
    matched = (df["event_type"] != "unmatched").sum()
    coverage = 100 * matched / total if total else 0
    print(f"Parsed {total:,} lines  ({matched:,} matched, {coverage:.1f}% coverage)")
    print()
    print("Event type distribution")
    print("-" * 40)
    print(df["event_type"].value_counts().to_string())
    print()
    print("Vendor distribution")
    print("-" * 40)
    print(df["vendor"].value_counts().to_string())

    out_path = os.path.join(OUTPUT_DIR, "parsed_logs.csv")
    df.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
