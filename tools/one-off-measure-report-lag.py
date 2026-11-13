#!/usr/bin/env python
"""Measure how far 511's reported Caltrain positions lag the predicted timeline.

Why: in the first live run every estimated position sat exactly at the cap
(MAX_SPEED_MPH x REPORT_LATENCY ~= 6.6 mi) ahead of the reported position,
meaning reports and predicted times disagree by more than the library allows
for. This measures by how much, to calibrate REPORT_LATENCY (or decide which
source to trust).

Method: for each en-route train with both a report and predicted times, find
the moment the predicted timeline (expected times; passed stops = scheduled +
delay, as the library does) puts the train at the reported milepost. Lag =
when we first saw the report minus that moment. Positive lag = report is older
than the predictions imply.

Usage (inside the project venv, ~5 511 requests, ~8 minutes):
    CALTRAIN_511_API_KEY=... .venv/bin/python tools/one-off-measure-report-lag.py [--polls 5] [--interval 120]

Output: one line per train per poll, then per-train and overall median lag.
"""

import argparse
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from caltrain_api import CaltrainAPI, Direction, TrainStatus


def crossing_time(train, milepost):
    """When the train's predicted timeline passes ``milepost`` (None if outside it)."""
    delay = train.delay
    pts = []  # (milepost, arrival, departure) in travel order
    for s in train.stops:
        arr = s.expected_arrival or (s.scheduled_arrival.astimezone(UTC) + delay)
        dep = s.expected_departure or (s.scheduled_departure.astimezone(UTC) + delay)
        pts.append((s.station.location.milepost, arr, dep))
    sign = 1 if train.direction is Direction.SOUTHBOUND else -1
    for (m1, a1, d1), (m2, a2, d2) in zip(pts, pts[1:]):
        if sign * m1 <= sign * milepost <= sign * m2:
            if m1 == m2:
                return d1
            frac = (milepost - m1) / (m2 - m1)
            return d1 + (a2 - d1) * frac
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--polls", type=int, default=5)
    ap.add_argument("--interval", type=int, default=120)
    args = ap.parse_args()
    key = os.environ.get("CALTRAIN_511_API_KEY") or sys.exit("CALTRAIN_511_API_KEY is not set")

    api = CaltrainAPI(api_key=key, poll_interval=timedelta(seconds=args.interval - 5))
    lags = defaultdict(list)
    for i in range(args.polls):
        if i:
            time.sleep(args.interval)
        now = datetime.now(UTC)
        print(f"--- poll {i} at {now.astimezone().strftime('%H:%M:%S')}")
        for t in api.get_active_trains():
            r = t.reported_position
            if r is None or t.delay is None or t.status is not TrainStatus.EN_ROUTE:
                continue
            crossed = crossing_time(t, r.milepost)
            if crossed is None:
                print(f"  {t.number}: report mp {r.milepost:.1f} outside predicted timeline")
                continue
            lag = (r.as_of - crossed).total_seconds() / 60
            lags[t.number].append(lag)
            print(f"  {t.number}: report mp {r.milepost:5.1f} ({r.description}), first seen "
                  f"{r.as_of.strftime('%H:%M:%S')}, timeline there at "
                  f"{crossed.astimezone().strftime('%H:%M:%S')} -> lag {lag:+.1f} min "
                  f"(delay {t.delay})")

    print("\n=== summary (minutes; + means report is older than predictions imply)")
    every = []
    for n, xs in sorted(lags.items()):
        every += xs
        print(f"  train {n}: n={len(xs)} median {statistics.median(xs):+.1f} "
              f"range {min(xs):+.1f}..{max(xs):+.1f}")
    if every:
        print(f"OVERALL: n={len(every)} median {statistics.median(every):+.1f} min, "
              f"range {min(every):+.1f}..{max(every):+.1f}")
    else:
        print("OVERALL: no usable observations")


if __name__ == "__main__":
    main()
