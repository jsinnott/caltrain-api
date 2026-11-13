"""Schedule behavior against the pinned 2026-11-13 feed (see conftest)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from caltrain_api import (
    Direction,
    PositionSource,
    Relation,
    ScheduleUnavailableError,
    ServiceType,
    TrainStatus,
    UnknownStationError,
    UnknownTrainError,
)
from caltrain_api.schedule import gtfs_time, parse_gtfs_time

from ..conftest import TZ, pt

FRI = date(2026, 11, 13)


# --- metadata & stations ----------------------------------------------------


def test_feed_metadata(schedule):
    assert schedule.valid_from == date(2026, 11, 13)
    assert schedule.valid_until == date(2027, 1, 31)
    assert schedule.downloaded_at is None


def test_stations_order_and_unserved(schedule):
    served = schedule.stations()
    assert served[0].name == "San Francisco" and served[-1].name == "Gilroy"
    assert len(served) == 30
    assert "stanford" not in {s.id for s in served}
    assert len(schedule.stations(include_unserved=True)) == 31
    mps = [s.location.milepost for s in served]
    assert mps == sorted(mps)


@pytest.mark.parametrize("key", ["palo_alto", "70171", "70172", "Palo Alto", "palo alto",
                                 "Palo Alto Station", "  PALO ALTO "])
def test_station_lookup(schedule, key):
    assert schedule.station(key).id == "palo_alto"


def test_station_lookup_names(schedule):
    assert schedule.station("Millbrae").id == "place_MLBR"
    assert schedule.station("San Francisco Caltrain Station").id == "san_francisco"
    with pytest.raises(UnknownStationError):
        schedule.station("Oakland")


def test_station_platforms(schedule):
    pa = schedule.station("palo_alto")
    assert pa.platform_stop_ids == {Direction.NORTHBOUND: "70171", Direction.SOUTHBOUND: "70172"}


def test_from_zip_missing_file(tmp_path):
    from caltrain_api import Schedule
    with pytest.raises(ScheduleUnavailableError):
        Schedule.from_zip(tmp_path / "nope.zip")


# --- calendar -----------------------------------------------------------------


@pytest.mark.parametrize("d,count,holiday", [
    (date(2026, 11, 13), 112, None),                       # Friday: weekday
    (date(2026, 11, 14), 66, None),                        # Saturday: weekend
    (date(2026, 11, 26), 66, "Thanksgiving Day"),          # weekend service
    (date(2026, 11, 27), 79, "Day after Thanksgiving"),    # modified (M-train) service
])
def test_calendar(schedule, d, count, holiday):
    assert len(schedule.trains(d)) == count
    assert schedule.holiday(d) == holiday


def test_modified_service_train_numbers(schedule):
    t = schedule.trains(date(2026, 11, 27))[0]
    assert t.trip_id.startswith("M")
    assert t.number == int(t.trip_id[1:])


def test_service_types(schedule):
    types = {t.service_type for t in schedule.trains(FRI)}
    assert types == {ServiceType.LOCAL, ServiceType.LIMITED, ServiceType.EXPRESS, ServiceType.SOUTH_COUNTY}


# --- GTFS time ----------------------------------------------------------------


def test_parse_gtfs_time():
    assert parse_gtfs_time("25:29:00") == 25 * 3600 + 29 * 60


def test_gtfs_time_past_midnight():
    assert gtfs_time(FRI, parse_gtfs_time("25:10:00")) == pt(2026, 11, 14, 1, 10)


def test_gtfs_time_dst_end_uses_noon_minus_12h():
    # 2026-11-01: clocks fall back at 2:00 PDT. Noon PST minus 12h is 01:00 PDT,
    # so per the GTFS spec "00:00:00" on that service day is 01:00 PDT.
    t = gtfs_time(date(2026, 11, 1), 0)
    assert t.astimezone(timezone.utc) == datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
    # Noon is still noon.
    assert gtfs_time(date(2026, 11, 1), 12 * 3600) == pt(2026, 11, 1, 12, 0)


# --- trains & positions -------------------------------------------------------


def test_train_163_positions(schedule):
    """163: Friday northbound local, San Jose 20:28 -> SF 21:46; skips College Park."""
    def pos(h, m, s=0):
        return schedule.train(163, at=pt(2026, 11, 13, h, m, s)).position

    assert pos(20, 27) is None
    p = pos(20, 28)
    assert (p.relation, p.station.name, p.source) == (Relation.AT, "San Jose Diridon", PositionSource.SCHEDULED)
    assert pos(20, 31).description == "passing College Park"
    assert pos(20, 33, 30).description == "approaching Santa Clara"
    assert pos(20, 56).description == "departed Palo Alto"
    assert pos(21, 46).description == "arrived at San Francisco"


def test_train_snapshot_fields(schedule):
    t = schedule.train(163, at=pt(2026, 11, 13, 20, 50))
    assert t.direction is Direction.NORTHBOUND and t.number % 2 == 1
    assert t.status is TrainStatus.EN_ROUTE
    assert t.origin.name == "San Jose Diridon" and t.destination.name == "San Francisco"
    assert t.previous_stop.station.name == "San Antonio"      # departed 20:49
    assert t.next_stop.station.name == "California Avenue"    # departs 20:52
    assert t.stop_at("Palo Alto").scheduled_departure == pt(2026, 11, 13, 20, 55)
    assert t.stop_at("College Park") is None
    assert t.position == t.scheduled_position
    assert t.reported_position is None and t.delay is None


def test_status_lifecycle(schedule):
    assert schedule.train(163, FRI, at=pt(2026, 11, 13, 12, 0)).status is TrainStatus.SCHEDULED
    assert schedule.train(163, FRI, at=pt(2026, 11, 13, 23, 0)).status is TrainStatus.COMPLETED


def test_train_picks_previous_service_day_after_midnight(schedule):
    t = schedule.train(174, at=pt(2026, 11, 14, 0, 30))
    assert t.service_date == FRI
    assert t.status is TrainStatus.EN_ROUTE


def test_unknown_train(schedule):
    with pytest.raises(UnknownTrainError):
        schedule.train(9999, at=pt(2026, 11, 13, 12, 0))
    with pytest.raises(UnknownTrainError):
        schedule.train(522, date(2026, 11, 14))  # weekday express, not on Saturday


def test_active_trains_after_midnight_include_previous_day(schedule):
    active = schedule.active_trains(pt(2026, 11, 14, 0, 30))
    assert active and all(t.service_date == FRI for t in active)


def test_active_trains_rush_hour(schedule):
    active = schedule.active_trains(pt(2026, 11, 13, 17, 30))
    assert len(active) == 11
    mps = [t.position.milepost for t in active]
    assert mps == sorted(mps)
    assert all(t.status is TrainStatus.EN_ROUTE for t in active)
    nb = schedule.active_trains(pt(2026, 11, 13, 17, 30), direction=Direction.NORTHBOUND)
    assert nb and all(t.direction is Direction.NORTHBOUND for t in nb)


def test_express_passes_not_departs(schedule):
    t = next(t for t in schedule.active_trains(pt(2026, 11, 13, 17, 30)) if t.number == 523)
    assert t.position.description == "passed Lawrence"


def test_naive_datetime_rejected(schedule):
    with pytest.raises(ValueError):
        schedule.active_trains(datetime(2026, 11, 13, 17, 30))


def test_scheduled_position_matches_snapshot(schedule):
    at = pt(2026, 11, 13, 20, 40)
    t = schedule.train(163, at=at)
    assert schedule.scheduled_position(t, at) == t.position
    later = schedule.scheduled_position(t, at + timedelta(minutes=10))
    assert later.milepost < t.position.milepost  # northbound: milepost decreasing


def test_all_times_are_pacific(schedule):
    t = schedule.train(163, at=pt(2026, 11, 13, 20, 40))
    assert t.as_of.tzinfo == TZ
    assert all(s.scheduled_departure.tzinfo == TZ for s in t.stops)
