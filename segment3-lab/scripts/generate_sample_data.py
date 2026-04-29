"""
generate_sample_data.py
=========================
Generates realistic-looking sample telemetry from a simulated router so
the demo can run end-to-end even when no real device is available.

Produces three files in ./data/ :
    syslog.txt     - 7 days of syslog (semi-structured, mixed vendors)
    metrics.csv    - 7 days of 1-minute interface utilization
    tickets.csv    - a small set of incident tickets to use as labels

Usage:
    python generate_sample_data.py
"""
import random
import csv
import os
from datetime import datetime, timedelta

random.seed(42)
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. SYSLOG (semi-structured)
# ---------------------------------------------------------------------------
SYSLOG_PATH = os.path.join(DATA_DIR, "syslog.txt")

# Three vendor formats for the same kind of event — see Slide 13 of the deck.
CISCO_LINK_DOWN  = "%LINK-3-UPDOWN: Interface GigabitEthernet0/{port}, changed state to down"
CISCO_LINK_UP    = "%LINK-3-UPDOWN: Interface GigabitEthernet0/{port}, changed state to up"
CISCO_LINEPROTO  = "%LINEPROTO-5-UPDOWN: Line protocol on Interface Gi0/{port}, changed state to {state}"
CISCO_BGP        = "%BGP-5-ADJCHANGE: neighbor 10.10.0.{nbr} {state}"
CISCO_OSPF       = "%OSPF-5-ADJCHG: Process 1, Nbr 10.20.0.{nbr} on Gi0/{port} from FULL to DOWN, Neighbor Down"
CISCO_CRC        = "%PM-4-ERR_DISABLE: link-flap error detected on Gi0/{port}, putting Gi0/{port} in err-disable state"
CISCO_AUTH       = "%SEC-6-IPACCESSLOGP: list 101 denied tcp 192.168.{a}.{b}(54321) -> 10.0.0.5(22)"
CISCO_INFO       = "%SYS-5-CONFIG_I: Configured from console by admin on vty0"

JUNIPER_LINK     = "mib2d[1873]: SNMP_TRAP_LINK_{state_upper}: ifIndex {idx}, ifName ge-0/0/{port}, ifAdminStatus up, ifOperStatus {state}"
ARISTA_LINK      = "%LINEPROTO-5-UPDOWN: Line protocol on Interface Ethernet{port}, changed state to {state}"

start = datetime(2026, 4, 14, 0, 0, 0)
lines = []

def add_line(t, host, msg):
    ts = t.strftime("%b %d %H:%M:%S")
    lines.append(f"{ts} {host} : {msg}")

# Generate 7 days of mostly-routine logs with one real incident on Friday
host = "rtr-bom-01"
for day in range(7):
    base = start + timedelta(days=day)
    # Routine background: a handful of innocuous logs scattered through the day
    for _ in range(random.randint(40, 70)):
        t = base + timedelta(seconds=random.randint(0, 86399))
        choice = random.random()
        if choice < 0.45:
            add_line(t, host, CISCO_AUTH.format(a=random.randint(1, 9), b=random.randint(2, 254)))
        elif choice < 0.75:
            add_line(t, host, CISCO_INFO)
        else:
            # Brief, recovered link blip — the kind of "is it an incident?" noise
            port = random.randint(2, 24)
            add_line(t, host, CISCO_LINK_DOWN.format(port=port))
            add_line(t + timedelta(seconds=random.randint(1, 8)),
                     host, CISCO_LINEPROTO.format(port=port, state="down"))
            add_line(t + timedelta(seconds=random.randint(10, 45)),
                     host, CISCO_LINK_UP.format(port=port))
            add_line(t + timedelta(seconds=random.randint(46, 75)),
                     host, CISCO_LINEPROTO.format(port=port, state="up"))

# Friday 14:32 incident: real, sustained outage on Gi0/1 with cascading effects
incident_start = datetime(2026, 4, 18, 14, 32, 0)
add_line(incident_start, host, CISCO_LINK_DOWN.format(port=1))
add_line(incident_start + timedelta(seconds=2),  host, CISCO_LINEPROTO.format(port=1, state="down"))
add_line(incident_start + timedelta(seconds=8),  host, CISCO_BGP.format(nbr=5, state="Down - interface flap"))
add_line(incident_start + timedelta(seconds=12), host, CISCO_OSPF.format(nbr=12, port=1))
add_line(incident_start + timedelta(seconds=15), host, CISCO_CRC.format(port=1))
# repeated flaps for ~6 minutes
for i in range(8):
    t = incident_start + timedelta(minutes=1, seconds=i * 40)
    add_line(t, host, CISCO_LINK_DOWN.format(port=1))
    add_line(t + timedelta(seconds=3), host, CISCO_LINK_UP.format(port=1))

# Another vendor's logs to demonstrate format diversity
juniper_host = "rtr-fra-02"
for day in range(7):
    base = start + timedelta(days=day)
    for _ in range(random.randint(15, 25)):
        t = base + timedelta(seconds=random.randint(0, 86399))
        port = random.randint(0, 12)
        idx = 500 + port
        state_pair = random.choice([("DOWN", "down"), ("UP", "up")])
        add_line(t, juniper_host,
                 JUNIPER_LINK.format(state_upper=state_pair[0], idx=idx,
                                     port=port, state=state_pair[1]))

arista_host = "sw-core-03"
for day in range(7):
    base = start + timedelta(days=day)
    for _ in range(random.randint(10, 20)):
        t = base + timedelta(seconds=random.randint(0, 86399))
        port = random.randint(1, 48)
        state = random.choice(["up", "down"])
        add_line(t, arista_host, ARISTA_LINK.format(port=port, state=state))

# Sort chronologically (real syslog arrives roughly in order, with jitter)
lines.sort()

with open(SYSLOG_PATH, "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"  wrote {len(lines):,} log lines  ->  {SYSLOG_PATH}")

# ---------------------------------------------------------------------------
# 2. METRICS (structured time series)
# ---------------------------------------------------------------------------
METRICS_PATH = os.path.join(DATA_DIR, "metrics.csv")

# 7 days of 1-minute interface utilization for one WAN port.
# Pattern: weekday-elevated, weekend-low, with a Friday spike + the Friday incident.
records = []
for day in range(7):
    for minute in range(24 * 60):
        t = start + timedelta(days=day, minutes=minute)
        hour = t.hour
        weekday = t.weekday()  # 0=Mon, 6=Sun

        # Base diurnal pattern: low overnight, peak mid-afternoon
        diurnal = 25 + 25 * max(0, 1 - abs(hour - 14) / 8.0)

        # Weekday vs weekend
        if weekday >= 5:  # Sat/Sun
            base = diurnal * 0.45
        else:
            base = diurnal

        # Friday afternoon legitimate spike (backups + reporting batch)
        if weekday == 4 and 13 <= hour <= 16:
            base += 35

        # The 14:32 Friday incident — sudden saturation while link flaps
        in_incident = (
            t >= incident_start and t < incident_start + timedelta(minutes=18)
        )
        if in_incident:
            base = random.choice([2, 4, 98, 99, 100, 1, 0])  # erratic during flaps

        # Add jitter
        value = max(0, min(100, base + random.gauss(0, 2.5)))
        records.append((t.isoformat(), "rtr-bom-01", "GigabitEthernet0/1", round(value, 2)))

with open(METRICS_PATH, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["timestamp", "device", "interface", "utilization_pct"])
    w.writerows(records)

print(f"  wrote {len(records):,} metric rows ->  {METRICS_PATH}")

# ---------------------------------------------------------------------------
# 3. TICKETS (sparse labels — see Slides 18-19 of the deck)
# ---------------------------------------------------------------------------
TICKETS_PATH = os.path.join(DATA_DIR, "tickets.csv")

tickets = [
    # Real Friday incident — note the imprecise human-filed timestamp
    {
        "ticket_id":   "INC0042189",
        "filed_at":    "2026-04-18T14:48:00",  # filed 16 min after symptoms
        "summary":     "WAN link rtr-bom-01 Gi0/1 unstable, BGP down",
        "category":    "Network-Link",
        "severity":    "P2",
        "device":      "rtr-bom-01",
        "interface":   "GigabitEthernet0/1",
        "started_at":  "2026-04-18T14:30:00",  # rounded to nearest 5 min
        "resolved_at": "2026-04-18T15:05:00",
    },
    # Distractor: filed but NOT a network event (will be filtered later)
    {
        "ticket_id":   "INC0042190",
        "filed_at":    "2026-04-15T09:12:00",
        "summary":     "Slow login from VPN",
        "category":    "Identity",
        "severity":    "P3",
        "device":      "",
        "interface":   "",
        "started_at":  "2026-04-15T09:00:00",
        "resolved_at": "2026-04-15T10:30:00",
    },
    # Older ticket from before our window (won't match any telemetry)
    {
        "ticket_id":   "INC0041992",
        "filed_at":    "2026-04-10T11:00:00",
        "summary":     "WAN circuit reset overnight",
        "category":    "Network-Link",
        "severity":    "P3",
        "device":      "rtr-bom-01",
        "interface":   "GigabitEthernet0/2",
        "started_at":  "2026-04-10T03:15:00",
        "resolved_at": "2026-04-10T03:45:00",
    },
]

with open(TICKETS_PATH, "w", newline="") as f:
    fieldnames = list(tickets[0].keys())
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    for t in tickets:
        w.writerow(t)

print(f"  wrote {len(tickets):,} tickets       ->  {TICKETS_PATH}")
print()
print("Done. Sample data ready in ./data/ — now run pipeline scripts in order.")
