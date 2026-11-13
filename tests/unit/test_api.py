"""CaltrainAPI facade: polling, degradation, estimation, departures.

Uses the 2026-11-13 11:45 PT SIRI capture (tests/fixtures) against the
matching GTFS fixture. In that capture train 123 is ~59 min late, 124-127
are running, and 128/130 (SF) and 129 (Tamien) are staged at their origins.
"""

from datetime import timedelta

import pytest

from caltrain_api import (
    CaltrainAPI,
    Direction,
    PositionSource,
    RateLimitedError,
    RealtimeSnapshot,
    RealtimeUnavailableError,
    Relation,
    TrainStatus,
)
from caltrain_api.realtime import parse_service_alerts, parse_vehicle_monitoring

from ..conftest import FIXTURES, pt

NOW = pt(2026, 11, 13, 11, 45, 20)
TRAINS = parse_vehicle_monitoring((FIXTURES / "siri-vm-20261113-1145.json").read_bytes())
ALERTS = parse_service_alerts((FIXTURES / "servicealerts-20261113-1145.pb").read_bytes())


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class FakeSource:
    """Returns queued results (snapshots or exceptions); the last repeats."""

    def __init__(self, clock, *results):
        self.clock = clock
        self.results = list(results) or ["full"]
        self.calls = 0

    def fetch(self):
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        if r == "full":
            return RealtimeSnapshot(fetched_at=self.clock(), trains=TRAINS, alerts=ALERTS)
        if r == "empty":
            return RealtimeSnapshot(fetched_at=self.clock(), trains={})
        raise AssertionError(r)


@pytest.fixture
def clock():
    return Clock()


def make_api(schedule, clock, *results, **kw):
    src = FakeSource(clock, *results)
    return CaltrainAPI(schedule=schedule, realtime=src, clock=clock, **kw), src


# --- schedule-only -------------------------------------------------------------


def test_no_key_is_schedule_only(schedule, clock):
    api = CaltrainAPI(schedule=schedule, clock=clock)
    trains = api.get_active_trains()
    assert trains and all(t.position.source is PositionSource.SCHEDULED for t in trains)
    assert all(t.reported_position is None and t.delay is None for t in trains)
    assert api.get_alerts() == [] and api.get_realtime_snapshot() is None


# --- enrichment --------------------------------------------------------------


def test_active_trains_include_late_and_waiting(schedule, clock):
    api, _ = make_api(schedule, clock)
    trains = {t.number: t for t in api.get_active_trains()}
    assert {123, 124, 125, 126, 127, 128, 129, 130} <= set(trains)

    late = trains[123]  # its scheduled run ended at ~11:46 in SF; really near Mountain View
    assert late.delay == pytest.approx(timedelta(minutes=58, seconds=45), abs=timedelta(seconds=1))
    assert late.status is TrainStatus.EN_ROUTE
    assert late.position.source is PositionSource.ESTIMATED
    assert late.position.description == "at Mountain View"
    assert late.scheduled_position.station.name == "San Francisco"

    waiting = trains[128]
    assert waiting.status is TrainStatus.SCHEDULED
    assert waiting.position.relation is Relation.WAITING
    assert waiting.position.description == "waiting to depart San Francisco"


def test_three_positions(schedule, clock):
    api, _ = make_api(schedule, clock)
    t = api.get_train(124)
    assert t.reported_position.source is PositionSource.REPORTED
    assert t.reported_position.lat == pytest.approx(37.4665985)  # raw, not snapped to track
    assert t.reported_position.description == "between Redwood City and Menlo Park"
    assert t.reported_position.as_of == NOW  # first seen
    assert t.position.description == "at Palo Alto"  # expected arrival 11:45:24
    assert t.scheduled_position.source is PositionSource.SCHEDULED
    assert t.delay == timedelta(minutes=1, seconds=58)


def test_expected_times_on_remaining_stops_only(schedule, clock):
    api, _ = make_api(schedule, clock)
    t = api.get_train(124)
    assert t.stop_at("Redwood City").expected_departure is None   # already passed
    assert t.stop_at("Palo Alto").expected_arrival == pt(2026, 11, 13, 11, 45, 24)
    assert t.next_stop.station.name == "Palo Alto"


def test_estimate_never_behind_report():
    from caltrain_api.api import CaltrainAPI as A
    from caltrain_api.models import TrainPosition
    rep = TrainPosition(lat=0, lon=0, milepost=20.0, source=PositionSource.REPORTED, as_of=NOW,
                        direction=Direction.SOUTHBOUND, relation=Relation.BETWEEN, description="x")
    assert A._reconcile(Direction.SOUTHBOUND, 18.0, rep) == 20.0
    assert A._reconcile(Direction.SOUTHBOUND, 21.0, rep) == 21.0
    # Far ahead is kept: stale report batches happen, predictions are more reliable.
    assert A._reconcile(Direction.SOUTHBOUND, 40.0, rep) == 40.0
    nb = rep.model_copy(update={"direction": Direction.NORTHBOUND})
    assert A._reconcile(Direction.NORTHBOUND, 22.0, nb) == 20.0
    assert A._reconcile(Direction.NORTHBOUND, 15.0, nb) == 15.0


def test_reported_as_of_is_first_seen(schedule, clock):
    api, _ = make_api(schedule, clock, poll_interval=timedelta(seconds=60))
    api.get_active_trains()
    clock.now += timedelta(minutes=2)  # same feed contents: positions unchanged
    t = api.get_train(124)
    assert t.reported_position.as_of == NOW


def test_alerts_attached_and_normalized(schedule, clock):
    api, _ = make_api(schedule, clock)
    assert any("38 minutes" in a.header for a in api.get_train(123).alerts)
    elevator = next(a for a in api.get_alerts() if "Elevator" in a.header)
    assert elevator.station_ids == ["san_mateo"]


def test_direction_filter(schedule, clock):
    api, _ = make_api(schedule, clock)
    nb = api.get_active_trains(direction=Direction.NORTHBOUND)
    assert nb and all(t.number % 2 == 1 for t in nb)


# --- polling & degradation ----------------------------------------------------


def test_poll_interval_limits_fetches(schedule, clock):
    api, src = make_api(schedule, clock)
    api.get_active_trains()
    api.get_train(124)
    api.get_departures("Palo Alto")
    assert src.calls == 1
    clock.now += timedelta(seconds=121)
    api.get_active_trains()
    assert src.calls == 2


def test_empty_feed_keeps_previous_snapshot(schedule, clock):
    api, _ = make_api(schedule, clock, "full", "empty")
    api.get_active_trains()
    clock.now += timedelta(minutes=3)
    t = api.get_train(124)
    assert t.reported_position is not None and t.position.source is PositionSource.ESTIMATED


def test_stale_snapshot_falls_back_to_schedule(schedule, clock):
    api, _ = make_api(schedule, clock, "full", RealtimeUnavailableError("down"))
    api.get_active_trains()
    clock.now += timedelta(minutes=11)
    trains = api.get_active_trains()
    assert trains and all(t.position.source is PositionSource.SCHEDULED for t in trains)


def test_rate_limit_backs_off(schedule, clock):
    api, src = make_api(schedule, clock, RateLimitedError("slow down", retry_after=600), "full")
    api.get_active_trains()
    clock.now += timedelta(minutes=5)
    api.get_active_trains()
    assert src.calls == 1  # still backing off
    clock.now += timedelta(minutes=6)
    api.get_active_trains()
    assert src.calls == 2


def test_realtime_not_used_far_from_now(schedule, clock):
    api, src = make_api(schedule, clock)
    trains = api.get_active_trains(at=NOW + timedelta(hours=3))
    assert src.calls == 0
    assert all(t.position.source is PositionSource.SCHEDULED for t in trains)


# --- departures ------------------------------------------------------------------


def test_departures_sort_by_expected_time(schedule, clock):
    api, _ = make_api(schedule, clock)
    deps = api.get_departures("Palo Alto", limit=5)
    nums = [d.train.number for d in deps]
    assert nums[:3] == [124, 123, 127]
    late = deps[1]
    assert late.stop.scheduled_departure == pt(2026, 11, 13, 10, 55)
    assert late.stop.departure > NOW
    times = [d.stop.departure for d in deps]
    assert times == sorted(times)


def test_departures_direction_and_terminus(schedule, clock):
    api = CaltrainAPI(schedule=schedule, clock=clock)
    sb = api.get_departures("Palo Alto", direction=Direction.SOUTHBOUND, limit=None)
    assert sb and all(d.train.direction is Direction.SOUTHBOUND for d in sb)
    # Northbound trains terminate at SF: never a departure from there.
    sf = api.get_departures("San Francisco", limit=None)
    assert sf and all(d.train.direction is Direction.SOUTHBOUND for d in sf)


def test_get_trains_whole_day(schedule, clock):
    api, _ = make_api(schedule, clock)
    trains = api.get_trains()
    assert len(trains) == 112
    assert next(t for t in trains if t.number == 124).reported_position is not None
