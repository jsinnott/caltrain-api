#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["gtfs-realtime-bindings", "httpx"]
# ///
"""Sample 511's Caltrain real-time feeds and report what they actually contain.

Why: the design hinges on whether VehiclePositions gives true GPS positions or
positions snapped to stations (a first spot-check showed every train sitting
exactly on a station), how often positions change, whether real-time trip_ids
match the static GTFS, what TripUpdates carries (delays vs absolute times,
skipped stops), and how 511 reports rate limits. We also peek at the SIRI
VehicleMonitoring endpoint in case it carries better positions.

Usage:
    CALTRAIN_511_API_KEY=... uv run tools/one-off-explore-realtime.py [--samples N] [--interval SECS]

Requires the static zip cached by tools/one-off-explore-static-gtfs.py.
Request budget: samples + 3 calls (511 default limit is ~60/hour) -- the
defaults use 13. The API key is never printed.

Reading the output: the per-vehicle summary at the end is the main result.
"snapped" = within 0.05 mi of a station. If nearly every observation is
snapped and positions only change station-to-station, the feed is
station-level, not GPS.
"""

import argparse
import csv
import io
import json
import math
import os
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

ROOT = Path(__file__).resolve().parent.parent
STATIC_ZIP = ROOT / ".explore" / "static" / "caltrain-gtfs.zip"
OUT_DIR = ROOT / ".explore" / "realtime"
BASE = "https://api.511.org/Transit"
TZ = ZoneInfo("America/Los_Angeles")
SNAP_MI = 0.05


def haversine_mi(lat1, lon1, lat2, lon2):
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_static():
    zf = zipfile.ZipFile(STATIC_ZIP)

    def table(name):
        with zf.open(name) as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

    stops = table("stops.txt")
    stations = [(s["stop_name"], float(s["stop_lat"]), float(s["stop_lon"]))
                for s in stops if s["location_type"] == "1"]
    stop_names = {s["stop_id"]: s["stop_name"] for s in stops}
    trips = {t["trip_id"]: t for t in table("trips.txt")}
    return stations, stop_names, trips


def nearest_station(stations, lat, lon):
    return min(((haversine_mi(lat, lon, la, lo), n) for n, la, lo in stations))


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M:%S") if ts else "-"


def fetch(client, path, key, label, **params):
    """GET a 511 endpoint; save raw body; print status and rate-limit headers."""
    r = client.get(f"{BASE}/{path}", params={"api_key": key, "agency": "CT", **params})
    limit_hdrs = {k: v for k, v in r.headers.items() if "rate" in k.lower() or "limit" in k.lower()}
    print(f"[{datetime.now(TZ):%H:%M:%S}] {path}: HTTP {r.status_code}, {len(r.content):,} bytes, "
          f"content-type={r.headers.get('content-type')}, rate headers={limit_hdrs or 'none'}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / label).write_bytes(r.content)
    r.raise_for_status()
    return r.content


def parse_pb(raw):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(raw)
    return feed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--interval", type=int, default=60, help="seconds between VehiclePositions samples")
    args = ap.parse_args()

    key = os.environ.get("CALTRAIN_511_API_KEY")
    if not key:
        sys.exit("CALTRAIN_511_API_KEY is not set")
    if not STATIC_ZIP.exists():
        sys.exit(f"{STATIC_ZIP} missing; run tools/one-off-explore-static-gtfs.py first")

    stations, stop_names, static_trips = load_static()
    client = httpx.Client(timeout=30)

    # ---- TripUpdates (one shot) -------------------------------------------
    print("\n=== TripUpdates " + "=" * 55)
    tu = parse_pb(fetch(client, "TripUpdates", key, "tripupdates.pb"))
    print(f"header: version={tu.header.gtfs_realtime_version} ts={fmt_ts(tu.header.timestamp)} "
          f"entities={len(tu.entity)}")
    rel = Counter()
    for e in tu.entity:
        t = e.trip_update
        rel[gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(t.trip.schedule_relationship)] += 1
    print("trip schedule_relationship:", dict(rel))
    for e in tu.entity[:4]:
        t = e.trip_update
        print(f"  trip {t.trip.trip_id} route={t.trip.route_id!r} start={t.trip.start_date} "
              f"dir={t.trip.direction_id if t.trip.HasField('direction_id') else '-'} "
              f"vehicle={t.vehicle.id or '-'} ts={fmt_ts(t.timestamp)} delay={t.delay if t.HasField('delay') else '-'} "
              f"in_static={t.trip.trip_id in static_trips} stus={len(t.stop_time_update)}")
        for s in t.stop_time_update[:3]:
            arr = f"arr t={fmt_ts(s.arrival.time)} d={s.arrival.delay}" if s.HasField("arrival") else "arr -"
            dep = f"dep t={fmt_ts(s.departure.time)} d={s.departure.delay}" if s.HasField("departure") else "dep -"
            srel = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(s.schedule_relationship)
            print(f"      seq={s.stop_sequence} {s.stop_id} ({stop_names.get(s.stop_id, '?')}) {arr} {dep} {srel}")
    tu_ids = {e.trip_update.trip.trip_id for e in tu.entity}
    print(f"trip_ids not in static GTFS: {sorted(tu_ids - static_trips.keys()) or 'none'}")

    # ---- ServiceAlerts (one shot) -----------------------------------------
    print("\n=== ServiceAlerts " + "=" * 53)
    sa = parse_pb(fetch(client, "ServiceAlerts", key, "servicealerts.pb"))
    print(f"{len(sa.entity)} alerts")
    for e in sa.entity[:5]:
        hdr = e.alert.header_text.translation[0].text if e.alert.header_text.translation else ""
        print(f"  {e.id}: {hdr[:100]!r}")

    # ---- SIRI VehicleMonitoring (one shot) --------------------------------
    print("\n=== SIRI VehicleMonitoring " + "=" * 44)
    raw = fetch(client, "VehicleMonitoring", key, "vehiclemonitoring.json", format="json")
    try:
        vm = json.loads(raw.decode("utf-8-sig"))
        acts = (vm["Siri"]["ServiceDelivery"]["VehicleMonitoringDelivery"].get("VehicleActivity") or [])
        print(f"{len(acts)} VehicleActivity entries")
        for a in acts[:4]:
            j = a["MonitoredVehicleJourney"]
            loc = j.get("VehicleLocation") or {}
            print(f"  ref={j.get('FramedVehicleJourneyRef', {}).get('DatedVehicleJourneyRef')} "
                  f"line={j.get('LineRef')} loc={loc} bearing={j.get('Bearing')} "
                  f"recorded={a.get('RecordedAtTime')} "
                  f"monitored_call={(j.get('MonitoredCall') or {}).get('StopPointName')}")
    except (ValueError, KeyError) as exc:
        print(f"  could not parse: {exc!r}; raw saved to {OUT_DIR / 'vehiclemonitoring.json'}")

    # ---- VehiclePositions (sampled over time) -----------------------------
    print(f"\n=== VehiclePositions: {args.samples} samples, {args.interval}s apart " + "=" * 20)
    history = defaultdict(list)  # trip_id -> [(feed_ts, veh_ts, lat, lon, dist, station, status, stop)]
    fields_seen = Counter()
    for i in range(args.samples):
        if i:
            time.sleep(args.interval)
        vp = parse_pb(fetch(client, "VehiclePositions", key, f"vehiclepositions-{i:02}.pb"))
        for e in vp.entity:
            v = e.vehicle
            for fd, _ in v.ListFields():
                fields_seen[fd.name] += 1
            for fd, _ in v.position.ListFields():
                fields_seen["position." + fd.name] += 1
            d, st = nearest_station(stations, v.position.latitude, v.position.longitude)
            status = gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(v.current_status) \
                if v.HasField("current_status") else "-"
            history[v.trip.trip_id].append(
                (vp.header.timestamp, v.timestamp, v.position.latitude, v.position.longitude,
                 d, st, status, v.stop_id))
        print(f"   sample {i}: {len(vp.entity)} vehicles, feed ts {fmt_ts(vp.header.timestamp)}")

    print("\nfields populated across all observations:", dict(fields_seen))
    vp_ids = set(history)
    print(f"VP trip_ids not in static: {sorted(vp_ids - static_trips.keys()) or 'none'}; "
          f"VP trips missing from TripUpdates: {sorted(vp_ids - tu_ids) or 'none'}")

    print("\n=== Per-vehicle summary " + "=" * 47)
    total = snapped = 0
    for trip_id, obs in sorted(history.items()):
        t = static_trips.get(trip_id, {})
        distinct = len({(round(o[2], 5), round(o[3], 5)) for o in obs})
        snaps = sum(1 for o in obs if o[4] <= SNAP_MI)
        total += len(obs)
        snapped += snaps
        print(f"train {trip_id} ({t.get('trip_headsign', '?')}): {len(obs)} obs, {distinct} distinct positions, "
              f"{snaps} snapped")
        for o in obs:
            print(f"    feed {fmt_ts(o[0])} veh {fmt_ts(o[1])}  {o[2]:.5f},{o[3]:.5f}  "
                  f"{o[4]:.3f} mi from {o[5]}  status={o[6]} stop={o[7] or '-'}")
    print(f"\nOVERALL: {snapped}/{total} observations within {SNAP_MI} mi of a station")
    print("DONE")


if __name__ == "__main__":
    main()
