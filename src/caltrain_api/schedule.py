"""The static timetable: stations, trips, service calendar, scheduled positions.

Needs no API key. After the first download it works offline from a disk cache.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
import platformdirs

from ._feedcache import FeedCache
from .corridor import Corridor, LatLon
from .describe import DescriptionPolicy
from .errors import ScheduleUnavailableError, UnknownStationError, UnknownTrainError
from .models import (
    Direction,
    PositionSource,
    Relation,
    ServiceType,
    Station,
    Stop,
    TrackLocation,
    Train,
    TrainPosition,
    TrainStatus,
)

log = logging.getLogger(__name__)

#: Caltrain's static GTFS, as linked from https://www.caltrain.com/developer-resources
DEFAULT_GTFS_URL = "https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip"

#: How old the cached feed may get before :meth:`Schedule.load` re-downloads it.
DEFAULT_MAX_AGE = timedelta(days=1)

#: Caltrain's agency timezone; every datetime the library produces is in it.
TZ = ZoneInfo("America/Los_Angeles")

# realtime_routes.txt / route_short_name prefixes -> ServiceType
_SERVICE_TYPES = {
    "local": ServiceType.LOCAL,
    "limited": ServiceType.LIMITED,
    "express": ServiceType.EXPRESS,
    "south county": ServiceType.SOUTH_COUNTY,
}

#: Argument to :meth:`Schedule.typical_service_date`.
DayType = Literal["weekday", "weekend"]

_TRAIN_NUMBER = re.compile(r"^[A-Za-z]*(\d+)$")
_NAME_SUFFIX = re.compile(r"\s+(caltrain\s+)?station$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_gtfs_time(value: str) -> int:
    """'25:10:00' -> seconds after the service day's reference time (may exceed 24h)."""
    h, m, s = (int(x) for x in value.strip().split(":"))
    return h * 3600 + m * 60 + s


def gtfs_time(service_date: date, seconds: int) -> datetime:
    """Absolute time of a GTFS stop time on ``service_date``.

    Per the GTFS spec, times are measured from "noon minus 12 hours" local time,
    which differs from midnight on daylight-saving transition days. Arithmetic
    is done in UTC: adding a timedelta to a zoneinfo-aware datetime does
    wall-clock arithmetic, which is wrong across a DST change.
    """
    noon = datetime.combine(service_date, time(12), tzinfo=TZ)
    base = noon.astimezone(UTC) - timedelta(hours=12)
    return (base + timedelta(seconds=seconds)).astimezone(TZ)


def parse_train_number(value: str) -> int | None:
    """'123' -> 123; holiday-schedule 'M147' -> 147; anything else -> None."""
    m = _TRAIN_NUMBER.match(value.strip())
    return int(m.group(1)) if m else None


def _require_aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("datetimes passed to caltrain_api must be timezone-aware")
    return at


def _now() -> datetime:
    return datetime.now(TZ)


# ---------------------------------------------------------------------------
# Internal parsed-feed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    station: Station
    arrival: int      # GTFS seconds
    departure: int


@dataclass(frozen=True)
class _Trip:
    number: int
    trip_id: str
    direction: Direction
    service_type: ServiceType
    service_id: str
    calls: tuple[_Call, ...]


@dataclass(frozen=True)
class _CalendarRule:
    weekdays: tuple[bool, ...]   # Monday..Sunday
    start: date
    end: date


def _gtfs_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _clean_name(name: str) -> str:
    return _NAME_SUFFIX.sub("", name.strip())


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


class Schedule:
    """Caltrain's published timetable, parsed from static GTFS.

    Immutable once loaded; :meth:`refresh` returns a *new* Schedule rather than
    mutating this one, so a Schedule can be shared between threads safely.

    Example::

        schedule = Schedule.load()
        for train in schedule.active_trains():
            print(train.number, train.position.description)
    """

    # -- construction -------------------------------------------------------

    @classmethod
    def load(
        cls,
        *,
        cache_dir: Path | None = None,
        max_age: timedelta = DEFAULT_MAX_AGE,
        url: str = DEFAULT_GTFS_URL,
        http_client: httpx.Client | None = None,
        description_policy: DescriptionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> Schedule:
        """Load the timetable, downloading it if the cache is missing or stale.

        The cache is considered stale when older than ``max_age`` or when today
        is past the feed's ``feed_end_date``. If a download fails but a cached
        copy exists, the cached copy is used and a warning is logged.

        Args:
            cache_dir: Where to keep the GTFS zip. Default: the platform's user
                cache dir (e.g. ``~/Library/Caches/caltrain-api`` on macOS).
            max_age: Re-download when the cached file is older than this.
            url: GTFS zip URL.
            http_client: Injected for testing or connection reuse.
            description_policy: Thresholds for position descriptions.
            clock: Returns "now" (aware); for tests. Drives staleness checks.

        Raises:
            ScheduleUnavailableError: no cached copy and the download failed.
        """
        policy = description_policy or DescriptionPolicy()
        cache = FeedCache(
            cache_dir=cache_dir or Path(platformdirs.user_cache_dir("caltrain-api")),
            url=url, max_age=max_age, http_client=http_client, clock=clock or _now,
        )
        return cls._load_from_cache(cache, policy, force=False)

    @classmethod
    def _load_from_cache(cls, cache: FeedCache, policy: DescriptionPolicy, *, force: bool) -> Schedule:
        cached: Schedule | None = None
        if cache.zip_path.exists():
            try:
                cached = cls._from_bytes(cache.zip_path.read_bytes(), policy)
            except ScheduleUnavailableError as exc:
                log.warning("Cached GTFS unusable, will re-download: %s", exc)

        result = cached
        if force or cached is None or cache.is_stale(cached.valid_until):
            try:
                fresh = cache.download(lambda data: cls._from_bytes(data, policy),
                                       conditional=cached is not None)
                if fresh is not None:
                    result = fresh
            except ScheduleUnavailableError as exc:
                if cached is None:
                    raise
                log.warning("GTFS refresh failed; using cached copy: %s", exc)

        assert result is not None  # download() returns None only when a cache exists
        meta = cache.read_meta()
        result._cache = cache
        result._downloaded_at = meta.downloaded_at if meta else None
        return result

    @classmethod
    def from_zip(cls, path: Path, *, description_policy: DescriptionPolicy | None = None) -> Schedule:
        """Load from a local GTFS zip with no network access (tests, pinned feeds).

        Raises:
            ScheduleUnavailableError: the file is missing or not a usable GTFS feed.
        """
        log.info("Loading GTFS from %s", path)
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise ScheduleUnavailableError(f"cannot read GTFS from {path}: {exc}") from exc
        return cls._from_bytes(data, description_policy or DescriptionPolicy())

    @classmethod
    def _from_bytes(cls, data: bytes, policy: DescriptionPolicy) -> Schedule:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return cls(zf, description_policy=policy)
        except (zipfile.BadZipFile, KeyError, ValueError) as exc:
            raise ScheduleUnavailableError(f"not a usable GTFS feed: {exc}") from exc

    def __init__(self, zf: zipfile.ZipFile, *, description_policy: DescriptionPolicy):
        """Parse an open GTFS zip. Use :meth:`load` or :meth:`from_zip` instead."""
        self._policy = description_policy
        self._downloaded_at: datetime | None = None
        self._cache: FeedCache | None = None

        def table(name: str, required: bool = True) -> list[dict[str, str]]:
            if name not in zf.namelist():
                if required:
                    raise KeyError(f"{name} missing from GTFS feed")
                return []
            with zf.open(name) as f:
                return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")))

        stops = table("stops.txt")
        routes = table("routes.txt")
        rt_routes = {r["route_id"]: r for r in table("realtime_routes.txt", required=False)}
        trips = table("trips.txt")
        stop_times = table("stop_times.txt")
        shapes = table("shapes.txt")
        feed_info = table("feed_info.txt", required=False)

        self._parse_calendar(table("calendar.txt", required=False),
                             table("calendar_dates.txt", required=False))

        # --- corridor: stitched from the shapes southbound trips use --------
        sb_shape_ids = {t["shape_id"] for t in trips if t.get("direction_id") == "1" and t.get("shape_id")}
        pts_by_shape: dict[str, list[tuple[int, LatLon]]] = defaultdict(list)
        for row in shapes:
            if row["shape_id"] in sb_shape_ids:
                pts_by_shape[row["shape_id"]].append(
                    (int(row["shape_pt_sequence"]), (float(row["shape_pt_lat"]), float(row["shape_pt_lon"]))))
        parents = [s for s in stops if s.get("location_type") == "1"]
        if not parents or not pts_by_shape:
            raise ValueError("feed has no parent stations or no southbound shapes")
        # SF 4th & King is the northernmost station; it defines milepost 0.
        sf = max(parents, key=lambda s: float(s["stop_lat"]))
        self._corridor = Corridor.from_shapes(
            [[p for _, p in sorted(v)] for v in pts_by_shape.values()],
            origin=(float(sf["stop_lat"]), float(sf["stop_lon"])),
        )

        # --- stations -------------------------------------------------------
        trip_dir = {t["trip_id"]: t.get("direction_id") for t in trips}
        platform_dir: dict[str, str] = {}
        used_platforms: set[str] = set()
        for st in stop_times:
            used_platforms.add(st["stop_id"])
            d = trip_dir.get(st["trip_id"])
            if d in ("0", "1"):
                platform_dir[st["stop_id"]] = d

        platforms_by_parent: dict[str, dict[Direction, str]] = defaultdict(dict)
        served_parents: set[str] = set()
        parent_of: dict[str, str] = {}
        for s in stops:
            if not s.get("parent_station"):
                continue
            # Every child (platforms, and elevators/entrances that alerts cite)
            # resolves to its station in lookups; only platforms carry service.
            parent_of[s["stop_id"]] = s["parent_station"]
            if (s.get("location_type") or "0") != "0":
                continue
            d = platform_dir.get(s["stop_id"])
            if d is None:  # unserved platform: fall back to its name
                name = s["stop_name"].lower()
                d = "0" if "northbound" in name else "1" if "southbound" in name else None
            if d is not None:
                direction = Direction.NORTHBOUND if d == "0" else Direction.SOUTHBOUND
                platforms_by_parent[s["parent_station"]][direction] = s["stop_id"]
            if s["stop_id"] in used_platforms:
                served_parents.add(s["parent_station"])

        stations = []
        for p in parents:
            lat, lon = float(p["stop_lat"]), float(p["stop_lon"])
            mp = self._corridor.locate(lat, lon).milepost
            stations.append(Station(
                id=p["stop_id"],
                name=_clean_name(p["stop_name"]),
                location=TrackLocation(lat=lat, lon=lon, milepost=mp),
                platform_stop_ids=platforms_by_parent.get(p["stop_id"], {}),
                has_scheduled_service=p["stop_id"] in served_parents,
            ))
        stations.sort(key=lambda s: s.location.milepost)
        self._all_stations = stations
        self._served_stations = [s for s in stations if s.has_scheduled_service]
        self._station_by_id = {s.id: s for s in stations}
        self._station_by_platform = {
            pid: self._station_by_id[parent] for pid, parent in parent_of.items() if parent in self._station_by_id
        }
        self._station_by_name = {s.name.lower(): s for s in stations}
        log.info("Loaded %d stations (%d served)", len(stations), len(self._served_stations))

        # --- trips ----------------------------------------------------------
        route_type: dict[str, ServiceType] = {}
        for r in routes:
            label = (rt_routes.get(r["route_id"], {}).get("realtime_routename")
                     or r.get("route_short_name") or r.get("route_long_name") or "").lower()
            st_type = next((v for k, v in _SERVICE_TYPES.items() if label.startswith(k)), None)
            if st_type is None:
                log.warning("Unrecognized route %s (%r); treating as LOCAL", r["route_id"], label)
                st_type = ServiceType.LOCAL
            route_type[r["route_id"]] = st_type

        calls_by_trip: dict[str, list[tuple[int, _Call]]] = defaultdict(list)
        for st in stop_times:
            station = self._station_by_platform.get(st["stop_id"])
            if station is None:
                log.warning("stop_time references unknown stop %s", st["stop_id"])
                continue
            calls_by_trip[st["trip_id"]].append((int(st["stop_sequence"]), _Call(
                station=station,
                arrival=parse_gtfs_time(st["arrival_time"] or st["departure_time"]),
                departure=parse_gtfs_time(st["departure_time"] or st["arrival_time"]),
            )))

        self._trips_by_service: dict[str, list[_Trip]] = defaultdict(list)
        self._trips_by_number: dict[int, list[_Trip]] = defaultdict(list)
        for t in trips:
            # Holiday ("modified") service numbers trains like "M147"; the train
            # number is the digits. The raw id is kept as Train.trip_id.
            raw_number = (t.get("trip_short_name") or t["trip_id"]).strip()
            parsed = parse_train_number(raw_number)
            if parsed is None:
                log.warning("Skipping trip %s: unparseable train number %r", t["trip_id"], raw_number)
                continue
            number = parsed
            calls = tuple(c for _, c in sorted(calls_by_trip.get(t["trip_id"], [])))
            if len(calls) < 2:
                log.warning("Skipping trip %s: fewer than two stops", t["trip_id"])
                continue
            d = t.get("direction_id")
            direction = (Direction.NORTHBOUND if d == "0" else Direction.SOUTHBOUND if d == "1"
                         else Direction.NORTHBOUND if number % 2 else Direction.SOUTHBOUND)
            trip = _Trip(number=number, trip_id=t["trip_id"], direction=direction,
                         service_type=route_type.get(t["route_id"], ServiceType.LOCAL),
                         service_id=t["service_id"], calls=calls)
            self._trips_by_service[trip.service_id].append(trip)
            self._trips_by_number[number].append(trip)
        for lst in self._trips_by_service.values():
            lst.sort(key=lambda tr: tr.calls[0].departure)
        log.info("Loaded %d trips across %d services",
                 sum(len(v) for v in self._trips_by_service.values()), len(self._trips_by_service))

        # --- feed metadata --------------------------------------------------
        fi = feed_info[0] if feed_info else {}
        self._feed_version = fi.get("feed_version") or "unknown"
        starts = [r.start for r in self._calendar.values()] + list(self._exceptions)
        ends = [r.end for r in self._calendar.values()] + list(self._exceptions)
        self._valid_from = _gtfs_date(fi["feed_start_date"]) if fi.get("feed_start_date") else min(starts)
        self._valid_until = _gtfs_date(fi["feed_end_date"]) if fi.get("feed_end_date") else max(ends)

    def _parse_calendar(self, calendar: list[dict[str, str]], calendar_dates: list[dict[str, str]]) -> None:
        days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        self._calendar: dict[str, _CalendarRule] = {
            r["service_id"]: _CalendarRule(
                weekdays=tuple(r[d] == "1" for d in days),
                start=_gtfs_date(r["start_date"]),
                end=_gtfs_date(r["end_date"]),
            )
            for r in calendar
        }
        # date -> list of (service_id, added?) ; date -> holiday name
        self._exceptions: dict[date, list[tuple[str, bool]]] = defaultdict(list)
        self._holidays: dict[date, str] = {}
        for r in calendar_dates:
            d = _gtfs_date(r["date"])
            self._exceptions[d].append((r["service_id"], r["exception_type"].strip() == "1"))
            if r.get("holiday_name"):
                self._holidays[d] = r["holiday_name"].strip()

    def refresh(self, *, force: bool = False) -> Schedule:
        """Return an up-to-date Schedule, downloading only if stale (or ``force``).

        Returns ``self`` when nothing changed, including for schedules made
        with :meth:`from_zip`, which have no download source.

        Raises:
            ScheduleUnavailableError: only when ``force`` is set and the download fails.
        """
        if self._cache is None:
            return self
        if not force and not self._cache.is_stale(self.valid_until):
            return self
        try:
            fresh = self._cache.download(lambda data: Schedule._from_bytes(data, self._policy),
                                         conditional=not force)
        except ScheduleUnavailableError as exc:
            if force:
                raise
            log.warning("GTFS refresh failed; keeping current schedule: %s", exc)
            return self
        if fresh is None:
            return self
        meta = self._cache.read_meta()
        fresh._cache = self._cache
        fresh._downloaded_at = meta.downloaded_at if meta else None
        log.info("Schedule refreshed: %s -> %s", self.feed_version, fresh.feed_version)
        return fresh

    # -- feed metadata ------------------------------------------------------

    @property
    def feed_version(self) -> str:
        return self._feed_version

    @property
    def valid_from(self) -> date:
        return self._valid_from

    @property
    def valid_until(self) -> date:
        return self._valid_until

    @property
    def downloaded_at(self) -> datetime | None:
        """When the underlying zip was fetched (None for :meth:`from_zip`)."""
        return self._downloaded_at

    @property
    def corridor(self) -> Corridor:
        return self._corridor

    @property
    def description_policy(self) -> DescriptionPolicy:
        return self._policy

    # -- stations -----------------------------------------------------------

    def stations(self, *, include_unserved: bool = False) -> list[Station]:
        """All stations in milepost order (SF first).

        Args:
            include_unserved: include stations no trip stops at (e.g. Stanford).
        """
        return list(self._all_stations if include_unserved else self._served_stations)

    def station(self, key: str) -> Station:
        """Look up a station by GTFS id ('palo_alto'), platform stop_id ('70171'),
        or name ('Palo Alto', 'palo alto', 'San Jose Diridon Station'; case- and
        'Station'-suffix-insensitive).

        Raises:
            UnknownStationError
        """
        k = key.strip()
        found = (self._station_by_id.get(k)
                 or self._station_by_platform.get(k)
                 or self._station_by_name.get(_clean_name(k).lower())
                 or self._station_by_id.get(k.lower().replace(" ", "_")))
        if found is None:
            raise UnknownStationError(key)
        return found

    # -- calendar -----------------------------------------------------------

    def service_ids(self, service_date: date) -> frozenset[str]:
        """GTFS service_ids running on ``service_date`` (calendar + calendar_dates exceptions)."""
        active = {
            sid for sid, rule in self._calendar.items()
            if rule.start <= service_date <= rule.end and rule.weekdays[service_date.weekday()]
        }
        for sid, added in self._exceptions.get(service_date, []):
            if added:
                active.add(sid)
            else:
                active.discard(sid)
        return frozenset(active)

    def holiday(self, service_date: date) -> str | None:
        """Holiday name from the feed if ``service_date`` has modified service, e.g. 'Thanksgiving Day'."""
        return self._holidays.get(service_date)

    def typical_service_date(self, day_type: DayType, on_or_after: date) -> date:
        """The first date on or after ``on_or_after`` that is an ordinary
        weekday (Mon-Fri) or weekend day (Sat/Sun) with no holiday service
        changes -- e.g. for showing "the weekday timetable".

        Raises:
            ValueError: no such date within the feed's validity period.
        """
        d = max(on_or_after, self.valid_from)
        while d <= self.valid_until:
            is_weekend = d.weekday() >= 5
            if is_weekend == (day_type == "weekend") and self.holiday(d) is None \
                    and d not in self._exceptions:
                return d
            d += timedelta(days=1)
        raise ValueError(f"no ordinary {day_type} on or after {on_or_after} "
                         f"within the feed (valid until {self.valid_until})")

    def service_date_for(self, at: datetime) -> date:
        """The service date a moment *primarily* belongs to (the calendar date in Pacific time).

        Note a moment can be covered by two service days: trips from the previous
        day run past midnight (GTFS times like 25:10:00). Methods that ask
        "what's running at T" consider both.
        """
        return _require_aware(at).astimezone(TZ).date()

    # -- trains -------------------------------------------------------------

    def _trips_on(self, service_date: date) -> list[_Trip]:
        out: list[_Trip] = []
        for sid in sorted(self.service_ids(service_date)):
            out.extend(self._trips_by_service.get(sid, []))
        out.sort(key=lambda tr: tr.calls[0].departure)
        return out

    def _snapshot(self, trip: _Trip, service_date: date, at: datetime) -> Train:
        stops = [
            Stop(station=c.station,
                 scheduled_arrival=gtfs_time(service_date, c.arrival),
                 scheduled_departure=gtfs_time(service_date, c.departure))
            for c in trip.calls
        ]
        if at < stops[0].scheduled_departure:
            status = TrainStatus.SCHEDULED
        elif at >= stops[-1].scheduled_arrival:
            status = TrainStatus.COMPLETED
        else:
            status = TrainStatus.EN_ROUTE
        pos = self.position_from_stops(trip.direction, stops, at, source=PositionSource.SCHEDULED)
        return Train(
            number=trip.number, trip_id=trip.trip_id, direction=trip.direction, service_type=trip.service_type,
            service_date=service_date, as_of=at.astimezone(TZ), status=status, stops=stops,
            position=pos, scheduled_position=pos,
        )

    def trains(
        self,
        service_date: date | None = None,
        *,
        direction: Direction | None = None,
    ) -> list[Train]:
        """All trains scheduled on ``service_date`` (default: today), in departure order.

        Snapshots are taken at the start of that service day, so ``position`` is None.
        """
        sd = service_date or self.service_date_for(_now())
        as_of = gtfs_time(sd, 0)
        return [self._snapshot(t, sd, as_of) for t in self._trips_on(sd)
                if direction is None or t.direction is direction]

    def train(self, number: int, service_date: date | None = None, *, at: datetime | None = None) -> Train:
        """One scheduled train, as a snapshot at ``at`` (default: now).

        Without ``service_date``, picks the run of ``number`` (from the service
        days of ``at`` and the day before) whose span contains ``at``, else the
        one closest to it -- so train 198 asked about at 12:30 AM finds the
        previous evening's run.

        Raises:
            UnknownTrainError: ``number`` doesn't run on the candidate service date(s).
        """
        at = _require_aware(at) if at else _now()
        if service_date is not None:
            dates = [service_date]
        else:
            today = self.service_date_for(at)
            dates = [today - timedelta(days=1), today]

        best: tuple[timedelta, _Trip, date] | None = None
        for sd in dates:
            sids = self.service_ids(sd)
            for trip in self._trips_by_number.get(number, []):
                if trip.service_id not in sids:
                    continue
                start = gtfs_time(sd, trip.calls[0].departure)
                end = gtfs_time(sd, trip.calls[-1].arrival)
                gap = start - at if at < start else at - end if at > end else timedelta(0)
                if best is None or gap < best[0]:
                    best = (gap, trip, sd)
        if best is None:
            raise UnknownTrainError(f"train {number} does not run on {', '.join(map(str, dates))}")
        return self._snapshot(best[1], best[2], at)

    def active_trains(self, at: datetime | None = None, *, direction: Direction | None = None) -> list[Train]:
        """Trains that, per the timetable, are between origin departure and
        destination arrival at ``at`` (default: now), across both service days
        that can cover that moment. Ordered by milepost.
        """
        at = _require_aware(at) if at else _now()
        today = self.service_date_for(at)
        out = []
        for sd in (today - timedelta(days=1), today):
            for trip in self._trips_on(sd):
                if direction is not None and trip.direction is not direction:
                    continue
                if gtfs_time(sd, trip.calls[0].departure) <= at <= gtfs_time(sd, trip.calls[-1].arrival):
                    out.append(self._snapshot(trip, sd, at))
        out.sort(key=lambda tr: tr.position.milepost if tr.position else 0.0)
        return out

    def scheduled_position(self, train: Train, at: datetime) -> TrainPosition | None:
        """Where ``train`` should be at ``at`` per the timetable (None outside its trip span).

        Interpolates linearly in milepost between consecutive scheduled stops and
        holds position at a station between its arrival and departure times.
        """
        return self.position_from_stops(train.direction, train.stops, _require_aware(at),
                                        source=PositionSource.SCHEDULED)

    def position_from_stops(
        self,
        direction: Direction,
        stops: Sequence[Stop],
        at: datetime,
        *,
        source: PositionSource,
    ) -> TrainPosition | None:
        """Interpolate a train's position at ``at`` from its stop times.

        With ``source=SCHEDULED`` uses timetable times; otherwise uses each
        stop's best-available (expected, else scheduled) times. Returns None
        before the origin departure or after the destination arrival.
        """
        if source is PositionSource.SCHEDULED:
            times = [(s.scheduled_arrival, s.scheduled_departure) for s in stops]
        else:
            times = [(s.arrival, s.departure) for s in stops]
        mps = [s.station.location.milepost for s in stops]

        milepost = _interpolate(mps, times, at)
        if milepost is None:
            return None
        return self.position_at_milepost(direction, stops, milepost, at, source=source)

    def position_at_milepost(
        self,
        direction: Direction,
        stops: Sequence[Stop],
        milepost: float,
        at: datetime,
        *,
        source: PositionSource,
        lat_lon: tuple[float, float] | None = None,
    ) -> TrainPosition:
        """Build a :class:`TrainPosition` for a train known to be at ``milepost``.

        The relation is WAITING before the origin departure (by best-available
        times), ARRIVED from the destination arrival on, and otherwise
        classified by :attr:`description_policy`.

        Args:
            stops: The train's stops (determine AT vs PASSING and WAITING/ARRIVED).
            lat_lon: Report this lat/lon instead of the track point at
                ``milepost`` (used for raw REPORTED positions).
        """
        times = [(s.arrival, s.departure) for s in stops]
        if source is PositionSource.SCHEDULED:
            times = [(s.scheduled_arrival, s.scheduled_departure) for s in stops]
        served = {s.station.id for s in stops}
        relation, station, prev, nxt = self._policy.classify(
            milepost, direction, self._served_stations, served)
        if at < times[0][1]:
            relation, station = Relation.WAITING, stops[0].station
        elif at >= times[-1][0]:
            relation, station = Relation.ARRIVED, stops[-1].station
        if lat_lon is None:
            loc = self._corridor.at_milepost(
                min(max(milepost, self._corridor.start), self._corridor.length))
            lat_lon = (loc.lat, loc.lon)
        return TrainPosition(
            lat=lat_lon[0], lon=lat_lon[1], milepost=milepost,
            source=source, as_of=at.astimezone(TZ), direction=direction,
            relation=relation, station=station, previous_station=prev, next_station=nxt,
            description=self._policy.phrase(
                relation, station, prev, nxt,
                stops_here=station is None or station.id in served),
        )


def _interpolate(
    mileposts: Sequence[float],
    times: Sequence[tuple[datetime, datetime]],
    at: datetime,
) -> float | None:
    """Milepost at ``at`` given per-stop mileposts and (arrival, departure) times.

    Holds at a station during its dwell; moves linearly in time between
    stations. None outside [first departure, last arrival].
    """
    if not mileposts or at < times[0][1] or at > times[-1][0]:
        return None
    for i, (arr, dep) in enumerate(times):
        if arr <= at <= dep:
            return mileposts[i]
        if i + 1 < len(times) and dep < at < times[i + 1][0]:
            next_arr = times[i + 1][0]
            frac = (at - dep) / (next_arr - dep)
            return mileposts[i] + frac * (mileposts[i + 1] - mileposts[i])
    return mileposts[-1]  # at == final arrival exactly, or out-of-order times
