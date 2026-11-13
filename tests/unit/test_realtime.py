"""SIRI / alerts parsing and SiriRealtimeSource HTTP behavior (no real network)."""

import json
from datetime import date, timedelta

import httpx
import pytest

from caltrain_api import Direction, RateLimitedError, RealtimeUnavailableError, SiriRealtimeSource
from caltrain_api.realtime import parse_service_alerts, parse_vehicle_monitoring

from ..conftest import FIXTURES, pt

SIRI = (FIXTURES / "siri-vm-20261113-1145.json").read_bytes()
ALERTS = (FIXTURES / "servicealerts-20261113-1145.pb").read_bytes()
FAKE_KEY = "not-a-real-key-0000"


def test_parse_vehicle_monitoring_fixture():
    trains = parse_vehicle_monitoring(SIRI)
    assert sorted(trains) == [123, 124, 125, 126, 127, 128, 129, 130]
    t = trains[124]
    assert t.service_date == date(2026, 11, 13)
    assert t.direction is Direction.SOUTHBOUND
    assert (t.lat, t.lon) == (pytest.approx(37.4665985), pytest.approx(-122.200989))
    first = t.expected_calls[0]
    assert first.platform_stop_id == "70162"  # Menlo Park SB
    assert first.expected_arrival == pt(2026, 11, 13, 11, 42, 58)
    assert first.expected_departure == pt(2026, 11, 13, 11, 43, 58)
    assert len({c.platform_stop_id for c in t.expected_calls}) == len(t.expected_calls)


def _siri(activities):
    return json.dumps({"Siri": {"ServiceDelivery": {"VehicleMonitoringDelivery": {
        "VehicleActivity": activities}}}}).encode()


def test_parse_handles_siri_json_quirks():
    one = {"MonitoredVehicleJourney": {
        "FramedVehicleJourneyRef": {"DataFrameRef": "2026-11-27", "DatedVehicleJourneyRef": "M147"},
        "DirectionRef": "N",
        "VehicleLocation": {"Latitude": "0", "Longitude": ""},  # no fix
        "MonitoredCall": {"StopPointRef": "70171", "ExpectedArrivalTime": "2026-11-27T20:00:00Z"},
        "OnwardCalls": None,
    }}
    trains = parse_vehicle_monitoring(b"\xef\xbb\xbf" + _siri(one))  # BOM, bare object, null list
    t = trains[147]
    assert t.trip_id == "M147" and t.lat is None and t.lon is None
    assert t.expected_calls[0].expected_departure is None
    assert parse_vehicle_monitoring(_siri(None)) == {}


def test_parse_rejects_wrong_shape():
    with pytest.raises(ValueError):
        parse_vehicle_monitoring(b'{"nope": 1}')


def test_parse_service_alerts_fixture():
    alerts = {a.id: a for a in parse_service_alerts(ALERTS)}
    assert len(alerts) == 5
    delayed = next(a for a in alerts.values() if a.effect == "SIGNIFICANT_DELAYS")
    assert delayed.train_numbers == [123] and "38 minutes" in delayed.header
    elevator = next(a for a in alerts.values() if "Elevator" in a.header)
    assert set(elevator.station_ids) == {"SMT-01-NB", "70091"}  # raw; the facade normalizes


# --- SiriRealtimeSource over a mock transport -----------------------------------


class Server:
    """Programmable fake 511: maps path -> list of responses (last one repeats)."""

    def __init__(self, **routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        queue = self.routes[name]
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, Exception):
            raise resp
        return resp


def source(server, clock, **kw):
    client = httpx.Client(transport=httpx.MockTransport(server))
    return SiriRealtimeSource(FAKE_KEY, http_client=client, clock=clock, **kw)


def test_fetch_sends_key_and_parses():
    srv = Server(VehicleMonitoring=[httpx.Response(200, content=SIRI)],
                 ServiceAlerts=[httpx.Response(200, content=ALERTS)])
    now = pt(2026, 11, 13, 11, 45, 20)
    snap = source(srv, lambda: now).fetch()
    assert snap.fetched_at == now and len(snap.trains) == 8 and len(snap.alerts) == 5
    vm = srv.calls[0].url.params
    assert vm["api_key"] == FAKE_KEY and vm["agency"] == "CT" and vm["format"] == "json"


def test_alerts_fetched_only_every_interval():
    srv = Server(VehicleMonitoring=[httpx.Response(200, content=SIRI)],
                 ServiceAlerts=[httpx.Response(200, content=ALERTS)])
    now = [pt(2026, 11, 13, 11, 45)]
    src = source(srv, lambda: now[0], alerts_interval=600)
    assert src.fetch().alerts is not None
    now[0] += timedelta(minutes=2)
    assert src.fetch().alerts is None
    now[0] += timedelta(minutes=9)
    assert src.fetch().alerts is not None
    assert sum(1 for c in srv.calls if c.url.path.endswith("ServiceAlerts")) == 2


def test_alerts_failure_does_not_fail_snapshot():
    srv = Server(VehicleMonitoring=[httpx.Response(200, content=SIRI)],
                 ServiceAlerts=[httpx.Response(500)])
    snap = source(srv, lambda: pt(2026, 11, 13, 11, 45)).fetch()
    assert len(snap.trains) == 8 and snap.alerts is None


def test_rate_limit_raises_with_retry_after():
    srv = Server(VehicleMonitoring=[httpx.Response(429, headers={"Retry-After": "90"})])
    with pytest.raises(RateLimitedError) as info:
        source(srv, lambda: pt(2026, 11, 13, 11, 45)).fetch()
    assert info.value.retry_after == 90


@pytest.mark.parametrize("failure", [
    httpx.Response(503),
    httpx.Response(200, content=b"<html>oops</html>"),
    httpx.ConnectError("boom"),
])
def test_failures_raise_unavailable_without_leaking_key(failure):
    srv = Server(VehicleMonitoring=[failure])
    with pytest.raises(RealtimeUnavailableError) as info:
        source(srv, lambda: pt(2026, 11, 13, 11, 45)).fetch()
    assert FAKE_KEY not in str(info.value)
    assert FAKE_KEY not in repr(info.value.__cause__)
