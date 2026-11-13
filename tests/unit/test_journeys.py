"""get_schedule (point-to-point timetable) and Schedule.typical_service_date."""

from datetime import date, timedelta

import pytest

from caltrain_api import CaltrainAPI, Direction, UnknownStationError

from ..conftest import pt

FRI = date(2026, 11, 13)


@pytest.fixture
def api(schedule):
    return CaltrainAPI(schedule=schedule, clock=lambda: pt(2026, 11, 13, 9, 0))


def test_northbound_journeys(api):
    js = api.get_schedule("Palo Alto", "San Francisco", FRI)
    assert js
    assert all(j.train.direction is Direction.NORTHBOUND for j in js)
    assert all(j.origin_stop.station.id == "palo_alto" for j in js)
    assert all(j.destination_stop.station.id == "san_francisco" for j in js)
    deps = [j.origin_stop.scheduled_departure for j in js]
    assert deps == sorted(deps)
    assert all(timedelta(minutes=25) < j.scheduled_duration < timedelta(minutes=80) for j in js)
    # Expresses are faster than locals.
    by_type = {}
    for j in js:
        by_type.setdefault(j.train.service_type.value, []).append(j.scheduled_duration)
    assert min(by_type["express"]) < min(by_type["local"])


def test_direction_follows_station_order(api):
    sb = api.get_schedule("San Francisco", "Palo Alto", FRI)
    assert sb and all(j.train.direction is Direction.SOUTHBOUND for j in sb)


def test_skipped_stations_excluded(api):
    # College Park is served by very few trains; every journey must stop there.
    js = api.get_schedule("College Park", "San Jose Diridon", FRI)
    assert all(j.train.stop_at("College Park") is not None for j in js)
    assert len(js) < len(api.get_schedule("Santa Clara", "San Jose Diridon", FRI))


def test_errors(api):
    with pytest.raises(ValueError):
        api.get_schedule("Palo Alto", "palo_alto", FRI)
    with pytest.raises(UnknownStationError):
        api.get_schedule("Palo Alto", "Oakland", FRI)


@pytest.mark.parametrize("start,day_type,expected", [
    (date(2026, 11, 24), "weekday", date(2026, 11, 24)),   # ordinary Tuesday
    (date(2026, 11, 26), "weekday", date(2026, 11, 30)),   # skips Thanksgiving + day after + weekend
    (date(2026, 11, 13), "weekend", date(2026, 11, 14)),
    (date(2026, 12, 24), "weekday", date(2026, 12, 28)),   # skips Christmas Eve/Day
    (date(2026, 1, 1), "weekday", date(2026, 11, 13)),     # clamps to feed start
])
def test_typical_service_date(schedule, start, day_type, expected):
    assert schedule.typical_service_date(day_type, start) == expected


def test_typical_service_date_beyond_feed(schedule):
    with pytest.raises(ValueError):
        schedule.typical_service_date("weekday", date(2027, 6, 1))
