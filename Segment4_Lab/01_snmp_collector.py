"""
Segment 4 — Demo 1: SNMP Polling Collector
============================================

Polls an interface counter (ifInOctets / ifOutOctets) every N seconds,
computes the bytes-per-second rate, handles 32-bit counter wrap and
device-reboot resets, and writes the result to a CSV file.

Three modes:
    --mode device       Poll a real network device (requires reachable SNMP host)
    --mode simulator    Poll snmpsim or any SNMP responder running on localhost
    --mode offline      No network needed — generates realistic synthetic counters
                        with embedded microbursts.  Use this if you can't reach
                        a device during the lab.

Examples
--------
    # Real Cisco / Juniper / Linux box at 10.0.0.1, community "public",
    # interface ifIndex 2, polled every 5 seconds for 60 seconds:
    python 01_snmp_collector.py --mode device --host 10.0.0.1 \\
        --community public --ifindex 2 --interval 5 --duration 60

    # No device available — run the offline simulator instead:
    python 01_snmp_collector.py --mode offline --interval 5 --duration 60
"""

import argparse
import asyncio
import csv
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# OIDs we will poll.  These are standard IF-MIB OIDs — every SNMP-capable
# device implements them.  We use the 64-bit "HC" (high-capacity) variants
# because 32-bit counters wrap every ~3.4 seconds on a 10G link.
# ---------------------------------------------------------------------------
OID_IF_DESCR    = "1.3.6.1.2.1.2.2.1.2"        # ifDescr.<ifIndex>     — name
OID_IF_HC_IN    = "1.3.6.1.2.1.31.1.1.1.6"     # ifHCInOctets.<ifIndex>
OID_IF_HC_OUT   = "1.3.6.1.2.1.31.1.1.1.10"    # ifHCOutOctets.<ifIndex>
OID_SYS_UPTIME  = "1.3.6.1.2.1.1.3.0"          # sysUpTimeInstance


# ---------------------------------------------------------------------------
# 1. SNMP polling against a real device (or snmpsim simulator)
# ---------------------------------------------------------------------------
async def snmp_get(host: str, community: str, oids: list[str], port: int = 161,
                   timeout: int = 2) -> dict[str, int | str | None]:
    """
    Issue a single SNMP GET and return {oid: value} for the requested OIDs.
    Returns None for any OID that errored.
    """
    # pysnmp >= 7.0 sync-via-asyncio API
    from pysnmp.hlapi.v1arch.asyncio import (
        SnmpDispatcher, CommunityData, UdpTransportTarget,
        ObjectType, ObjectIdentity, get_cmd
    )

    transport = await UdpTransportTarget.create(
        (host, port), timeout=timeout, retries=1
    )
    object_types = [ObjectType(ObjectIdentity(o)) for o in oids]

    error_indication, error_status, error_index, var_binds = await get_cmd(
        SnmpDispatcher(),
        CommunityData(community),
        transport,
        *object_types,
    )

    if error_indication:
        raise RuntimeError(f"SNMP error: {error_indication}")
    if error_status:
        raise RuntimeError(f"SNMP error_status: {error_status.prettyPrint()}")

    out = {}
    for vb in var_binds:
        oid_str = str(vb[0])
        raw_val = vb[1]
        # Coerce numeric values to int; leave strings (like ifDescr) as str
        try:
            out[oid_str] = int(raw_val)
        except (ValueError, TypeError):
            out[oid_str] = str(raw_val)
    return out


def poll_device(host, community, ifindex, interval, duration, output_csv):
    """Poll a real device every `interval` seconds for `duration` seconds."""

    in_oid  = f"{OID_IF_HC_IN}.{ifindex}"
    out_oid = f"{OID_IF_HC_OUT}.{ifindex}"
    descr_oid = f"{OID_IF_DESCR}.{ifindex}"
    uptime_oid = OID_SYS_UPTIME

    # First, fetch the interface description so the operator can
    # confirm they're polling the right interface.
    print(f"[init] querying {host} for ifIndex {ifindex} description ...")
    try:
        descr_resp = asyncio.run(snmp_get(host, community, [descr_oid]))
        ifdescr = descr_resp.get(descr_oid, "?")
        print(f"[init] interface name: {ifdescr}")
    except Exception as e:
        print(f"[fatal] cannot reach device {host}: {e}", file=sys.stderr)
        sys.exit(2)

    # Open the CSV, write header
    csv_path = Path(output_csv)
    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp_utc", "host", "ifindex", "ifdescr",
            "in_octets", "out_octets",
            "in_bps", "out_bps",
            "uptime_ticks", "wrap_or_reset"
        ])
        f.flush()

        prev = None  # (in_octets, out_octets, uptime, monotonic_time)
        t_end = time.monotonic() + duration
        sample_idx = 0

        while time.monotonic() < t_end:
            t_now = time.monotonic()
            ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

            try:
                resp = asyncio.run(snmp_get(host, community, [in_oid, out_oid, uptime_oid]))
            except Exception as e:
                # Mark missing data explicitly — do NOT fill in a fake value
                print(f"[{ts_iso}] poll failed: {e}")
                writer.writerow([ts_iso, host, ifindex, ifdescr,
                                 "", "", "", "", "", "poll_failed"])
                f.flush()
                # Still advance the prev-state so we don't compute a bogus rate
                prev = None
                time.sleep(max(0, interval - (time.monotonic() - t_now)))
                sample_idx += 1
                continue

            in_oct  = resp.get(in_oid)
            out_oct = resp.get(out_oid)
            uptime  = resp.get(uptime_oid)

            in_bps = out_bps = ""
            wrap_flag = ""

            if prev is not None:
                p_in, p_out, p_uptime, p_t = prev

                # Detect counter reset: device rebooted (uptime decreased) OR
                # the new value is smaller than the previous one (32-bit wrap
                # for HC counters is unreachable in any human timeframe, so a
                # decrease means a reset, not a wrap).
                dt = t_now - p_t
                if uptime is not None and p_uptime is not None and uptime < p_uptime:
                    wrap_flag = "device_reboot"
                elif in_oct < p_in or out_oct < p_out:
                    wrap_flag = "counter_reset"
                else:
                    in_bps  = round((in_oct  - p_in)  / dt, 2)
                    out_bps = round((out_oct - p_out) / dt, 2)

            writer.writerow([ts_iso, host, ifindex, ifdescr,
                             in_oct, out_oct, in_bps, out_bps,
                             uptime, wrap_flag])
            f.flush()

            print(f"[{ts_iso}] in={in_oct} out={out_oct} "
                  f"in_bps={in_bps} out_bps={out_bps} {wrap_flag}")

            prev = (in_oct, out_oct, uptime, t_now)
            sample_idx += 1
            # Sleep so we hit the requested interval as closely as possible
            time.sleep(max(0, interval - (time.monotonic() - t_now)))

    print(f"\n[done] wrote {sample_idx} samples to {csv_path}")


# ---------------------------------------------------------------------------
# 2. Offline simulator — same code path, no network required
# ---------------------------------------------------------------------------
def poll_offline(ifindex, interval, duration, output_csv, seed=42):
    """
    Generate realistic interface counters WITHOUT touching the network.
    Simulates: ~50 Mbps baseline traffic, diurnal pattern, occasional
    microbursts, one counter reset partway through.

    Identical CSV schema as poll_device() — downstream tools can't tell
    the difference.
    """
    rng = random.Random(seed)
    csv_path = Path(output_csv)

    # Counter state (in bytes, monotonically increasing except on reset)
    in_octets  = rng.randint(10_000_000_000, 20_000_000_000)
    out_octets = rng.randint(10_000_000_000, 20_000_000_000)
    uptime_ticks = 100_000_000  # SNMP TimeTicks (1/100 sec)

    # Inject a counter reset roughly halfway through
    reset_at = duration / 2 + rng.uniform(-5, 5)

    with csv_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp_utc", "host", "ifindex", "ifdescr",
            "in_octets", "out_octets",
            "in_bps", "out_bps",
            "uptime_ticks", "wrap_or_reset"
        ])

        prev = None
        elapsed = 0.0

        while elapsed < duration:
            ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # Baseline rate: ~50 Mbps with diurnal bump
            t_of_day = (time.time() % 86400) / 86400.0
            diurnal = 1.0 + 0.4 * math.sin(2 * math.pi * t_of_day)
            base_in_bps  = 6_250_000 * diurnal      # 50 Mbps
            base_out_bps = 4_500_000 * diurnal      # 36 Mbps

            # 8% chance of a microburst this interval (5–9× normal)
            if rng.random() < 0.08:
                burst_factor = rng.uniform(5, 9)
                base_in_bps  *= burst_factor
                base_out_bps *= burst_factor

            # Counter reset event
            wrap_flag = ""
            if elapsed >= reset_at and prev is not None and "reset_done" not in dir(poll_offline):
                in_octets  = rng.randint(0, 1_000_000)
                out_octets = rng.randint(0, 1_000_000)
                uptime_ticks = rng.randint(0, 1000)
                poll_offline.reset_done = True   # one-shot

            in_octets   += int(base_in_bps  * interval)
            out_octets  += int(base_out_bps * interval)
            uptime_ticks += int(interval * 100)

            in_bps = out_bps = ""
            if prev is not None:
                p_in, p_out, p_uptime = prev
                if uptime_ticks < p_uptime:
                    wrap_flag = "device_reboot"
                elif in_octets < p_in or out_octets < p_out:
                    wrap_flag = "counter_reset"
                else:
                    in_bps  = round((in_octets  - p_in)  / interval, 2)
                    out_bps = round((out_octets - p_out) / interval, 2)

            writer.writerow([
                ts_iso, "offline-sim", ifindex, "GigabitEthernet0/1",
                in_octets, out_octets, in_bps, out_bps,
                uptime_ticks, wrap_flag
            ])
            print(f"[{ts_iso}] in={in_octets} out={out_octets} "
                  f"in_bps={in_bps} out_bps={out_bps} {wrap_flag}")

            prev = (in_octets, out_octets, uptime_ticks)
            elapsed += interval
            time.sleep(interval)

    print(f"\n[done] offline-sim wrote samples to {csv_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Segment 4 Demo 1 — SNMP poller")
    p.add_argument("--mode", choices=["device", "simulator", "offline"],
                   default="offline",
                   help="device = real SNMP host, simulator = local snmpsim, "
                        "offline = no network (default)")
    p.add_argument("--host", default="127.0.0.1",
                   help="device IP / hostname (mode=device or simulator)")
    p.add_argument("--community", default="public", help="SNMPv2c community")
    p.add_argument("--ifindex", type=int, default=2,
                   help="ifIndex of interface to poll (find via snmpwalk)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="poll interval seconds (try 5 vs 60 to see the lesson)")
    p.add_argument("--duration", type=float, default=60.0,
                   help="total polling duration in seconds")
    p.add_argument("--output", default="snmp_samples.csv",
                   help="output CSV path")
    args = p.parse_args()

    print(f"\n=== Demo 1: SNMP Polling Collector ===")
    print(f"mode={args.mode}  interval={args.interval}s  duration={args.duration}s")
    print(f"output={args.output}\n")

    if args.mode in ("device", "simulator"):
        poll_device(args.host, args.community, args.ifindex,
                    args.interval, args.duration, args.output)
    else:
        poll_offline(args.ifindex, args.interval, args.duration, args.output)


if __name__ == "__main__":
    main()
