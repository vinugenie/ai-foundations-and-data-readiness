"""
00_collect_from_device.py
=========================
Optional starter script: how to pull telemetry off a real device into the
same CSV/text formats the rest of the pipeline expects.

Three collection modes are illustrated, in order of increasing modernity:

    A. SNMP polling    (legacy, near-universal)
    B. Syslog ingest   (push from device to a UDP listener)
    C. gNMI streaming  (modern, structured, Cisco / Juniper / Arista / Nokia)

Pick whichever your lab device supports. For the lab walkthrough we recommend
SNMP because it works on practically every router/switch ever built and only
needs read-only credentials.

Skip this script entirely if you are running the demo against the simulator —
generate_sample_data.py produces equivalent files without touching a network.
"""
import csv
import os
import sys
from datetime import datetime

HERE     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "..", "data")
os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# A.  SNMP polling — pulls interface-utilization counters via pysnmp.
# ---------------------------------------------------------------------------
# Install once:    pip install pysnmp
# Device prereq:   `snmp-server community <ro_string> RO` (Cisco IOS) or the
#                  equivalent on your platform.
#
# The OIDs below are standard IF-MIB:
#    1.3.6.1.2.1.31.1.1.1.6  ifHCInOctets   (64-bit RX byte counter)
#    1.3.6.1.2.1.31.1.1.1.10 ifHCOutOctets  (64-bit TX byte counter)
#    1.3.6.1.2.1.31.1.1.1.15 ifHighSpeed    (link speed in Mbps)
#
# We compute utilization locally from two snapshots taken 60 seconds apart.
# This is exactly the "counter wraparound" trap from Slide 8 — handle 64-bit
# wraparound (rare but real) and missed samples gracefully in production.

def snmp_collect(host: str, community: str, port: int = 161,
                 interface_index: int = 1, samples: int = 7,
                 interval_sec: int = 60) -> None:
    try:
        from pysnmp.hlapi import (
            getCmd, SnmpEngine, CommunityData, UdpTransportTarget,
            ContextData, ObjectType, ObjectIdentity,
        )
    except ImportError:
        sys.exit("pysnmp not installed. Run:  pip install pysnmp")
    import time

    in_oid    = f"1.3.6.1.2.1.31.1.1.1.6.{interface_index}"
    out_oid   = f"1.3.6.1.2.1.31.1.1.1.10.{interface_index}"
    speed_oid = f"1.3.6.1.2.1.31.1.1.1.15.{interface_index}"

    def get(oid):
        it = getCmd(SnmpEngine(),
                    CommunityData(community, mpModel=1),  # SNMPv2c
                    UdpTransportTarget((host, port), timeout=2, retries=1),
                    ContextData(),
                    ObjectType(ObjectIdentity(oid)))
        err_indication, err_status, _err_idx, varbinds = next(it)
        if err_indication or err_status:
            raise RuntimeError(f"SNMP error: {err_indication or err_status.prettyPrint()}")
        return int(varbinds[0][1])

    print(f"Collecting {samples} samples from {host} ifIndex={interface_index}")
    speed_mbps = get(speed_oid)
    if speed_mbps == 0:
        sys.exit("Interface speed reported as 0 — check ifIndex.")
    print(f"  Link speed: {speed_mbps} Mbps")

    rows = []
    prev_in = get(in_oid)
    prev_out = get(out_oid)
    for i in range(samples):
        time.sleep(interval_sec)
        cur_in  = get(in_oid)
        cur_out = get(out_oid)

        # Bytes per second; multiply by 8 for bits, divide by link capacity.
        delta_bytes = (cur_in - prev_in) + (cur_out - prev_out)
        if delta_bytes < 0:                # 64-bit counter rollover protection
            delta_bytes = 0
        bits_per_sec = (delta_bytes * 8) / interval_sec
        capacity_bps = speed_mbps * 1_000_000
        util_pct     = min(100.0, 100.0 * bits_per_sec / capacity_bps)

        rows.append({
            "timestamp":       datetime.now().isoformat(timespec="seconds"),
            "device":          host,
            "interface":       f"ifIndex.{interface_index}",
            "utilization_pct": round(util_pct, 2),
        })
        print(f"  [{i+1}/{samples}]  {util_pct:5.1f}%")

        prev_in, prev_out = cur_in, cur_out

    out_path = os.path.join(DATA_DIR, "metrics.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {out_path}")


# ---------------------------------------------------------------------------
# B.  Syslog UDP listener — captures pushed events and writes to text file.
# ---------------------------------------------------------------------------
# Device prereq:  `logging host <listener_ip> transport udp port 514` (Cisco)
# Run as root or with CAP_NET_BIND_SERVICE if listening on UDP/514.

def syslog_listen(bind_addr: str = "0.0.0.0", port: int = 5140,
                  duration_sec: int = 60) -> None:
    import socket
    import time

    print(f"Listening for syslog on {bind_addr}:{port} for {duration_sec}s")
    print("On the device, configure:  logging host <THIS_IP> transport udp port", port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    sock.settimeout(2.0)

    out_path  = os.path.join(DATA_DIR, "syslog.txt")
    end_t     = time.time() + duration_sec
    received  = 0
    with open(out_path, "a") as f:
        while time.time() < end_t:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            ts = datetime.now().strftime("%b %d %H:%M:%S")
            line = data.decode("utf-8", errors="replace").strip()
            f.write(f"{ts} {addr[0]} : {line}\n")
            received += 1
    sock.close()
    print(f"\nReceived {received} messages, appended to {out_path}")


# ---------------------------------------------------------------------------
# C.  gNMI streaming subscription — modern, structured, push-based.
# ---------------------------------------------------------------------------
# Install once:   pip install pygnmi
# Device prereq:  enable the gRPC server with TLS or set the box to insecure
#                 mode for lab use.  Cisco IOS-XE: `gnmi-yang`,
#                 Juniper: `set system services extension-service ...`,
#                 Arista: `service gnmi`.
#
# Why gNMI over SNMP? Look at Slide 13 — gNMI returns a structured JSON path
# instead of a regex'd syslog message. No parsing layer.

def gnmi_subscribe(host: str, port: int, username: str, password: str,
                   xpath: str = "/interfaces/interface[name=GigabitEthernet0/1]"
                                "/state/counters",
                   sample_interval_sec: int = 10,
                   duration_sec: int = 60) -> None:
    try:
        from pygnmi.client import gNMIclient
    except ImportError:
        sys.exit("pygnmi not installed. Run:  pip install pygnmi")
    import time

    print(f"Subscribing to {xpath} on {host}:{port}")
    out_path = os.path.join(DATA_DIR, "metrics.csv")
    end_t = time.time() + duration_sec

    with gNMIclient(target=(host, port), username=username, password=password,
                    insecure=True) as client, \
         open(out_path, "w", newline="") as f:

        w = csv.DictWriter(f,
                           fieldnames=["timestamp", "device",
                                       "interface", "utilization_pct"])
        w.writeheader()

        # Subscribe SAMPLE mode pushes the values on a fixed interval.
        sub = {
            "subscription": [{
                "path":            xpath,
                "mode":            "sample",
                "sample_interval": sample_interval_sec * 1_000_000_000,
            }],
            "mode":     "stream",
            "encoding": "json",
        }
        for update in client.subscribe(subscribe=sub):
            if time.time() > end_t:
                break
            # Each gNMI update is already structured — no regex involved.
            for u in update.get("update", {}).get("update", []):
                row = {
                    "timestamp":       datetime.now().isoformat(timespec="seconds"),
                    "device":          host,
                    "interface":       xpath,
                    "utilization_pct": u.get("val", {}).get("in-octets", 0),
                }
                w.writerow(row)
                f.flush()
                print(f"  {row}")

    print(f"\nWrote {out_path}")


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Pull telemetry from a real device")
    sub = p.add_subparsers(dest="cmd", required=True)

    snmp = sub.add_parser("snmp",   help="SNMP-poll interface counters")
    snmp.add_argument("--host",      required=True)
    snmp.add_argument("--community", default="public")
    snmp.add_argument("--ifindex",   type=int, default=1)
    snmp.add_argument("--samples",   type=int, default=7)

    sl = sub.add_parser("syslog",   help="Listen for syslog UDP")
    sl.add_argument("--port",        type=int, default=5140)
    sl.add_argument("--duration",    type=int, default=60)

    gn = sub.add_parser("gnmi",     help="Subscribe to gNMI counters")
    gn.add_argument("--host",        required=True)
    gn.add_argument("--port",        type=int, default=57400)
    gn.add_argument("--user",        required=True)
    gn.add_argument("--password",    required=True)

    args = p.parse_args()
    if args.cmd == "snmp":
        snmp_collect(args.host, args.community,
                     interface_index=args.ifindex, samples=args.samples)
    elif args.cmd == "syslog":
        syslog_listen(port=args.port, duration_sec=args.duration)
    elif args.cmd == "gnmi":
        gnmi_subscribe(args.host, args.port, args.user, args.password)
