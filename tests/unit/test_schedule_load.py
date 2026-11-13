"""Schedule.load / refresh: download, disk cache, staleness, failure handling."""

from datetime import timedelta

import httpx
import pytest

from caltrain_api import Schedule, ScheduleUnavailableError

from ..conftest import GTFS_ZIP, pt

GTFS = GTFS_ZIP.read_bytes()
URL = "https://example.test/gtfs.zip"


class Server:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(resp, Exception):
            raise resp
        return resp


class Clock:
    def __init__(self):
        self.now = pt(2026, 11, 13, 9, 0)

    def __call__(self):
        return self.now


def load(tmp_path, server, clock, **kw):
    client = httpx.Client(transport=httpx.MockTransport(server))
    return Schedule.load(cache_dir=tmp_path, url=URL, http_client=client, clock=clock, **kw)


def ok(etag='"v1"'):
    return httpx.Response(200, content=GTFS, headers={"ETag": etag})


def test_first_load_downloads_and_caches(tmp_path):
    srv, clock = Server(ok()), Clock()
    s = load(tmp_path, srv, clock)
    assert len(srv.requests) == 1
    assert (tmp_path / "caltrain-gtfs.zip").read_bytes() == GTFS
    assert s.downloaded_at == clock.now
    assert len(s.stations()) == 30


def test_fresh_cache_needs_no_network(tmp_path):
    clock = Clock()
    load(tmp_path, Server(ok()), clock)
    clock.now += timedelta(hours=2)
    offline = Server(httpx.ConnectError("offline"))
    s = load(tmp_path, offline, clock)
    assert offline.requests == [] and len(s.stations()) == 30


def test_stale_cache_revalidates_conditionally(tmp_path):
    clock = Clock()
    load(tmp_path, Server(ok()), clock)
    clock.now += timedelta(days=2)
    srv = Server(httpx.Response(304))
    s = load(tmp_path, srv, clock)
    assert srv.requests[0].headers["If-None-Match"] == '"v1"'
    assert s.downloaded_at == pt(2026, 11, 13, 9, 0)  # content unchanged


def test_download_failure_falls_back_to_cache(tmp_path):
    clock = Clock()
    load(tmp_path, Server(ok()), clock)
    clock.now += timedelta(days=2)
    s = load(tmp_path, Server(httpx.Response(500)), clock)
    assert len(s.stations()) == 30


def test_no_cache_and_no_network_raises(tmp_path):
    with pytest.raises(ScheduleUnavailableError):
        load(tmp_path, Server(httpx.ConnectError("offline")), Clock())


def test_corrupt_download_never_replaces_good_cache(tmp_path):
    clock = Clock()
    load(tmp_path, Server(ok()), clock)
    clock.now += timedelta(days=2)
    s = load(tmp_path, Server(httpx.Response(200, content=b"not a zip")), clock)
    assert len(s.stations()) == 30
    assert (tmp_path / "caltrain-gtfs.zip").read_bytes() == GTFS


def test_expired_feed_is_stale_even_if_recent(tmp_path):
    clock = Clock()
    load(tmp_path, Server(ok()), clock, max_age=timedelta(days=365))
    clock.now = pt(2027, 2, 1, 9, 0)  # past feed_end_date 2027-01-31
    srv = Server(httpx.Response(304))
    load(tmp_path, srv, clock, max_age=timedelta(days=365))
    assert len(srv.requests) == 1


def test_refresh(tmp_path):
    clock = Clock()
    srv = Server(ok('"v1"'), httpx.Response(304), ok('"v2"'))
    s = load(tmp_path, srv, clock)
    assert s.refresh() is s                      # not stale: no request
    assert len(srv.requests) == 1
    clock.now += timedelta(days=2)
    assert s.refresh() is s                      # 304: unchanged
    clock.now += timedelta(days=2)
    s2 = s.refresh()                             # new content
    assert s2 is not s and s2.downloaded_at == clock.now
    assert len(srv.requests) == 3


def test_refresh_force_failure_raises(tmp_path):
    clock = Clock()
    srv = Server(ok(), httpx.Response(500))
    s = load(tmp_path, srv, clock)
    with pytest.raises(ScheduleUnavailableError):
        s.refresh(force=True)
    assert s.refresh() is s  # non-forced, not stale


def test_from_zip_schedule_refresh_is_noop():
    s = Schedule.from_zip(GTFS_ZIP)
    assert s.refresh(force=True) is s
