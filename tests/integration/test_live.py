"""Live end-to-end checks against the real GTFS download and 511.

Skipped unless CALTRAIN_511_API_KEY is set. Costs ~2 511 requests per run
(one VehicleMonitoring, one ServiceAlerts). Assertions are loose because live
service varies; run with ``-s`` to see what the library reports.

    CALTRAIN_511_API_KEY=... .venv/bin/pytest -s tests/integration
"""

import os

import pytest

from caltrain_api import CaltrainAPI, PositionSource, Schedule

KEY = os.environ.get("CALTRAIN_511_API_KEY")
pytestmark = pytest.mark.skipif(not KEY, reason="CALTRAIN_511_API_KEY not set")


def test_live_download_and_positions(tmp_path):
    schedule = Schedule.load(cache_dir=tmp_path)
    assert len(schedule.stations()) >= 25
    assert schedule.downloaded_at is not None

    api = CaltrainAPI(api_key=KEY, schedule=schedule)
    snap = api.get_realtime_snapshot()
    trains = api.get_active_trains()

    print(f"\nfeed {schedule.feed_version}; real-time trains: {len(snap.trains) if snap else 'n/a'}")
    for t in trains:
        p, r = t.position, t.reported_position
        print(f"  {t.number:>4} {t.service_type.value:12} {t.status.value:9} "
              f"mp {p.milepost:5.1f} [{p.source.value:9}] {p.description:42} "
              f"delay={t.delay} reported={r.description if r else '-'}")
    for a in api.get_alerts():
        print(f"  alert: {a.header[:90]}")

    if snap is not None and snap.trains:
        assert any(t.position and t.position.source is not PositionSource.SCHEDULED for t in trains)
