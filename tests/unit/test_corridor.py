import pytest

from caltrain_api import Corridor, OffCorridorError
from caltrain_api.corridor import haversine_mi

# A synthetic straight north-south line along lon -122.0, from lat 37.0 to 37.5.
NORTH, SOUTH = (37.5, -122.0), (37.0, -122.0)
MI_PER_DEG_LAT = haversine_mi(37.0, -122.0, 38.0, -122.0)


def line(origin=NORTH):
    return Corridor([NORTH, (37.25, -122.0), SOUTH], origin=origin)


def test_length_and_origin():
    c = line()
    assert c.start == pytest.approx(0.0)
    assert c.length == pytest.approx(0.5 * MI_PER_DEG_LAT, rel=1e-6)


def test_origin_offsets_mileposts():
    c = line(origin=(37.4, -122.0))
    assert c.start == pytest.approx(-0.1 * MI_PER_DEG_LAT, rel=1e-6)
    assert c.locate(37.4, -122.0).milepost == pytest.approx(0.0, abs=1e-9)


def test_locate_projects_onto_track():
    c = line()
    loc = c.locate(37.3, -122.001)  # ~0.055 mi west of the line
    assert loc.lon == pytest.approx(-122.0)
    assert loc.lat == pytest.approx(37.3)
    assert loc.milepost == pytest.approx(0.2 * MI_PER_DEG_LAT, rel=1e-4)
    assert c.offset(37.3, -122.001) == pytest.approx(0.055, abs=0.002)


def test_locate_rejects_far_points():
    with pytest.raises(OffCorridorError):
        line().locate(37.3, -122.1)


def test_at_milepost_roundtrip_and_bounds():
    c = line()
    for mp in (0.0, 5.0, 17.27, c.length):
        loc = c.at_milepost(mp)
        assert c.locate(loc.lat, loc.lon).milepost == pytest.approx(mp, abs=1e-6)
    with pytest.raises(ValueError):
        c.at_milepost(-1.0)
    with pytest.raises(ValueError):
        c.at_milepost(c.length + 1)


def test_polyline_slice():
    c = line()
    pts = c.polyline(start=5.0, end=25.0)
    assert pts[0].milepost == 5.0 and pts[-1].milepost == 25.0
    assert [p.milepost for p in pts] == sorted(p.milepost for p in pts)
    assert len(pts) == 3  # endpoints plus the middle vertex at ~17.27


def test_from_shapes_splices_extension():
    main = [NORTH, (37.25, -122.0), (37.1, -122.0)]
    # Starts mid-main (37.2) and continues past main's south end.
    ext = [(37.2, -122.0), (37.0, -122.0), (36.9, -122.0)]
    c = Corridor.from_shapes([ext, main], origin=NORTH)
    assert c.length == pytest.approx(0.6 * MI_PER_DEG_LAT, rel=1e-6)
    # Monotonic: a point between the splice and main's end maps once, correctly.
    assert c.locate(37.15, -122.0).milepost == pytest.approx(0.35 * MI_PER_DEG_LAT, rel=1e-6)


# --- against the real feed ------------------------------------------------


def test_real_corridor_station_mileposts(schedule):
    c = schedule.corridor
    assert c.start == pytest.approx(0.0, abs=0.05)
    assert 76 < c.length < 78  # Caltrain's official Gilroy milepost is 77.4
    mp = {s.id: s.location.milepost for s in schedule.stations(include_unserved=True)}
    assert mp["san_francisco"] == pytest.approx(0.0, abs=0.01)
    assert mp["sj_diridon"] == pytest.approx(46.9, abs=0.3)  # official 46.9
    assert mp["gilroy"] == pytest.approx(77.0, abs=0.5)


def test_real_corridor_roundtrip_every_station(schedule):
    c = schedule.corridor
    for s in schedule.stations(include_unserved=True):
        loc = c.at_milepost(s.location.milepost)
        # Stations sit within ~100 m of the track centerline.
        assert haversine_mi(loc.lat, loc.lon, s.location.lat, s.location.lon) < 0.1, s.name
