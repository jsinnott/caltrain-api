#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Download Caltrain's static GTFS feed and report what's in it.

Why: before designing the Schedule layer we need facts, not assumptions --
which files exist, how routes/service types are named, whether stops use
parent stations + platforms, whether shapes carry shape_dist_traveled (so we
know whether milepost math can lean on it or must compute distance itself),
whether any shape covers the whole SF->Gilroy corridor, how the service
calendar and holiday exceptions look, and whether stop_times run past 24:00.

Usage:
    uv run tools/one-off-explore-static-gtfs.py [--refresh]

The zip is cached at .explore/static/caltrain-gtfs.zip (gitignored) and read
in-memory with zipfile; it is never extracted. A successful run prints a
series of labelled sections ending in "DONE". Nothing is judged pass/fail --
this is a survey whose output we read.
"""

import argparse
import csv
import io
import math
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

# Linked from https://www.caltrain.com/developer-resources
GTFS_URL = "https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip"
CACHE = Path(__file__).resolve().parent.parent / ".explore" / "static" / "caltrain-gtfs.zip"


def haversine_mi(lat1, lon1, lat2, lon2):
    r = 3958.7613  # mean Earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def read_table(zf, name):
    if name not in zf.namelist():
        return None
    with zf.open(name) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    args = ap.parse_args()

    if args.refresh or not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading {GTFS_URL}")
        with urllib.request.urlopen(GTFS_URL) as resp:
            print(f"  Last-Modified: {resp.headers.get('Last-Modified')}")
            CACHE.write_bytes(resp.read())
    print(f"Using {CACHE} ({CACHE.stat().st_size:,} bytes)")

    zf = zipfile.ZipFile(CACHE)

    section("Files and row counts")
    tables = {}
    for name in sorted(zf.namelist()):
        rows = read_table(zf, name) if name.endswith(".txt") else None
        tables[name] = rows
        cols = ", ".join(rows[0].keys()) if rows else ""
        print(f"{name:28} {len(rows) if rows is not None else '-':>7} rows  [{cols}]")

    section("feed_info / agency")
    for t in ("feed_info.txt", "agency.txt"):
        for row in tables.get(t) or []:
            print(t, dict(row))

    section("routes")
    for r in tables["routes.txt"]:
        print({k: v for k, v in r.items() if v})

    section("stops: location_type breakdown")
    stops = tables["stops.txt"]
    print(Counter(s.get("location_type") or "0(blank)" for s in stops))
    by_id = {s["stop_id"]: s for s in stops}
    children = defaultdict(list)
    for s in stops:
        if s.get("parent_station"):
            children[s["parent_station"]].append(s)
    parents = [s for s in stops if s.get("location_type") == "1"]
    print(f"{len(parents)} parent stations; showing each with its child platforms")
    for p in sorted(parents, key=lambda s: -float(s["stop_lat"])):
        kids = "; ".join(
            f"{c['stop_id']}({c.get('platform_code') or c['stop_name']})" for c in children[p["stop_id"]]
        )
        print(f"  {p['stop_id']:>12}  {p['stop_name']:38} {float(p['stop_lat']):.5f},{float(p['stop_lon']):.5f}  -> {kids}")

    section("trips")
    trips = tables["trips.txt"]
    print("per route_id:", Counter(t["route_id"] for t in trips))
    print("per service_id:", Counter(t["service_id"] for t in trips))
    print("per direction_id:", Counter(t.get("direction_id") for t in trips))
    print("distinct shape_ids:", len({t.get("shape_id") for t in trips}))
    print("sample trips:")
    for t in trips[:5]:
        print("  ", dict(t))
    same = sum(1 for t in trips if t.get("trip_short_name") and t["trip_short_name"] == t["trip_id"])
    print(f"trip_id == trip_short_name for {same}/{len(trips)} trips")
    # Caltrain convention: odd train numbers northbound, even southbound. Check it.
    parity = Counter(
        (t.get("direction_id"), int(t["trip_short_name"]) % 2)
        for t in trips if (t.get("trip_short_name") or "").isdigit()
    )
    print("(direction_id, train# parity) counts:", dict(parity))

    section("shapes")
    shapes = defaultdict(list)
    has_dist = 0
    for row in tables["shapes.txt"]:
        shapes[row["shape_id"]].append(row)
        if row.get("shape_dist_traveled"):
            has_dist += 1
    print(f"{len(shapes)} shapes; {has_dist}/{len(tables['shapes.txt'])} points have shape_dist_traveled")
    sf = next((p for p in parents if "San Francisco" in p["stop_name"]), None)
    for sid, pts in sorted(shapes.items(), key=lambda kv: -len(kv[1])):
        pts.sort(key=lambda r: int(r["shape_pt_sequence"]))
        length = sum(
            haversine_mi(float(a["shape_pt_lat"]), float(a["shape_pt_lon"]),
                         float(b["shape_pt_lat"]), float(b["shape_pt_lon"]))
            for a, b in zip(pts, pts[1:])
        )
        first, last = pts[0], pts[-1]
        print(f"  {sid:>14}: {len(pts):5} pts, {length:6.2f} mi, "
              f"start {float(first['shape_pt_lat']):.4f},{float(first['shape_pt_lon']):.4f} "
              f"end {float(last['shape_pt_lat']):.4f},{float(last['shape_pt_lon']):.4f}"
              + (f", last dist={last.get('shape_dist_traveled')}" if last.get("shape_dist_traveled") else ""))
    if sf:
        print(f"SF parent station: {sf['stop_name']} {sf['stop_lat']},{sf['stop_lon']}")

    section("calendar / calendar_dates")
    for row in tables.get("calendar.txt") or []:
        print(dict(row))
    cd = tables.get("calendar_dates.txt") or []
    print(f"{len(cd)} calendar_dates exceptions:")
    for row in sorted(cd, key=lambda r: r["date"]):
        print(f"  {row['date']} service={row['service_id']} exception_type={row['exception_type']}")

    section("stop_times")
    st = tables["stop_times.txt"]
    max_t = max(r["departure_time"] for r in st if r.get("departure_time"))
    over24 = sum(1 for r in st if r.get("departure_time", "") >= "24:00:00")
    print(f"max departure_time {max_t}; {over24} rows at/after 24:00:00")
    print("pickup_type:", Counter(r.get("pickup_type") for r in st))
    print("drop_off_type:", Counter(r.get("drop_off_type") for r in st))
    print("timepoint:", Counter(r.get("timepoint") for r in st))
    print("rows with shape_dist_traveled:", sum(1 for r in st if r.get("shape_dist_traveled")))
    # Which stop_ids do stop_times reference -- platforms or parents?
    ref_types = Counter(by_id.get(r["stop_id"], {}).get("location_type") or "0" for r in st)
    print("location_type of stop_ids referenced by stop_times:", dict(ref_types))
    # Do any trips list stops they pass without stopping (express skip-stops)?
    per_trip = Counter(r["trip_id"] for r in st)
    print("stops per trip (min/max):", min(per_trip.values()), max(per_trip.values()))
    sample = trips[0]["trip_id"]
    print(f"stop_times for trip {sample}:")
    for r in sorted((r for r in st if r["trip_id"] == sample), key=lambda r: int(r["stop_sequence"])):
        name = by_id.get(r["stop_id"], {}).get("stop_name")
        print(f"  {r['stop_sequence']:>3} {r['arrival_time']} {r['departure_time']} {r['stop_id']:>8} {name}")

    print("\nDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
