import pytest
from pydantic import ValidationError

from caltrain_api import DescriptionPolicy, Direction, Relation, Station, TrackLocation

N, S = Direction.NORTHBOUND, Direction.SOUTHBOUND


def st(id_, name, mp):
    return Station(id=id_, name=name, location=TrackLocation(lat=0, lon=0, milepost=mp),
                   platform_stop_ids={}, has_scheduled_service=True)


A, B, C, D = st("a", "Alpha", 10.0), st("b", "Bravo", 12.0), st("c", "Charlie", 12.8), st("d", "Delta", 20.0)
STATIONS = [A, B, C, D]
ALL = {"a", "b", "c", "d"}
policy = DescriptionPolicy()


def classify(mp, direction=S, served=ALL):
    return policy.classify(mp, direction, STATIONS, served)


@pytest.mark.parametrize("mp,direction,expected", [
    (10.1, S, (Relation.AT, A, None, B)),
    (9.85, N, (Relation.AT, A, B, None)),
    (10.4, S, (Relation.DEPARTED, A, A, B)),
    (11.6, S, (Relation.APPROACHING, B, A, B)),
    (16.0, S, (Relation.BETWEEN, None, C, D)),
    (16.0, N, (Relation.BETWEEN, None, D, C)),
    (19.6, N, (Relation.DEPARTED, D, D, C)),
    (13.2, N, (Relation.APPROACHING, C, D, C)),
    (13.4, N, (Relation.BETWEEN, None, D, C)),  # 0.6 from Charlie: beyond near_radius
])
def test_classify(mp, direction, expected):
    assert classify(mp, direction) == expected


def test_threshold_edges_are_inclusive():
    assert classify(10.2)[0] is Relation.AT
    assert classify(10.5)[0] is Relation.DEPARTED
    assert classify(10.51)[0] is Relation.BETWEEN


def test_approaching_beats_departed_when_stations_are_close():
    # 12.4 is 0.4 past Bravo and 0.4 short of Charlie (0.8 mi apart).
    rel, station, _, _ = classify(12.4, S)
    assert (rel, station) == (Relation.APPROACHING, C)


def test_passing_when_train_does_not_stop():
    assert classify(12.0, S, served={"a", "d"})[:2] == (Relation.PASSING, B)


def test_phrases():
    p = policy.phrase
    assert p(Relation.AT, A, None, B) == "at Alpha"
    assert p(Relation.PASSING, B, A, C) == "passing Bravo"
    assert p(Relation.APPROACHING, B, A, B) == "approaching Bravo"
    assert p(Relation.DEPARTED, A, A, B) == "departed Alpha"
    assert p(Relation.DEPARTED, A, A, B, stops_here=False) == "passed Alpha"
    assert p(Relation.BETWEEN, None, C, D) == "between Charlie and Delta"
    assert p(Relation.WAITING, A, None, B) == "waiting to depart Alpha"
    assert p(Relation.ARRIVED, D, C, None) == "arrived at Delta"


def test_custom_thresholds_and_validation():
    wide = DescriptionPolicy(at_radius=0.5, near_radius=1.0)
    assert wide.classify(10.4, S, STATIONS, ALL)[0] is Relation.AT
    with pytest.raises(ValidationError):
        DescriptionPolicy(at_radius=0.5, near_radius=0.5)
