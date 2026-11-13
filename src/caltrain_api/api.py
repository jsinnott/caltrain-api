"""CaltrainAPI: the main entry point, combining the timetable with real-time data."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .describe import DescriptionPolicy
from .errors import (
    CaltrainError,
    OffCorridorError,
    RateLimitedError,
    RealtimeUnavailableError,
    UnknownStationError,
    UnknownTrainError,
)
from .models import (
    Alert,
    Departure,
    Journey,
    Direction,
    PositionSource,
    Station,
    Stop,
    Train,
    TrainPosition,
    TrainStatus,
)
from .realtime import RealtimeSnapshot, RealtimeSource, SiriRealtimeSource, TrainObservation
from .schedule import TZ, Schedule, _interpolate

log = logging.getLogger(__name__)

#: Minimum time between real-time fetches. 511 allows 60 requests/hour per key
#: and refreshes Caltrain positions only every ~3-4 minutes, so polling faster
#: than this buys nothing.
DEFAULT_POLL_INTERVAL = timedelta(seconds=120)

#: How long a real-time snapshot stays usable when fetches fail or come back
#: empty. Older than this, trains fall back to schedule-only data.
DEFAULT_MAX_STALENESS = timedelta(minutes=10)

#: Real-time data is applied only when ``at`` is within this window of now;
#: questions about other times are answered from the timetable alone.
REALTIME_WINDOW = timedelta(minutes=5)

#: How often the facade checks whether the static GTFS needs refreshing.
SCHEDULE_CHECK_INTERVAL = timedelta(hours=1)

#: get_departures looks this far back for scheduled departures, to catch late trains.
LATE_TRAIN_LOOKBACK = timedelta(hours=2)


def _shift(dt: datetime, delta: timedelta) -> datetime:
    """Absolute-time shift (zoneinfo datetimes otherwise do wall-clock arithmetic)."""
    return (dt.astimezone(UTC) + delta).astimezone(TZ)


class CaltrainAPI:
    """Caltrain schedules and train positions.

    Without an API key, everything works from the published timetable
    (positions are SCHEDULED). With a 511 key, trains also carry reported
    positions, predicted times, delays, and best-estimate positions.

    Thread-safe: one instance can serve a long-running, concurrent application.
    Real-time data is fetched lazily -- when a method needs it and the last
    fetch is older than ``poll_interval`` -- so call as often as you like. The
    static timetable is re-checked at most hourly and re-downloaded when stale
    (by default, daily); that download happens inside whichever call notices.

    Example::

        api = CaltrainAPI(api_key=os.environ["CALTRAIN_511_API_KEY"])
        for train in api.get_active_trains():
            p = train.position
            print(f"{train.number} {train.service_type.value:8} mp {p.milepost:5.1f} "
                  f"{p.description}  ({train.delay or 'on time'})")

    Args:
        api_key: 511.org API key. None means schedule-only.
        schedule: A preloaded Schedule (default: ``Schedule.load(cache_dir=...)``).
        realtime: A custom real-time source (default: SiriRealtimeSource when
            ``api_key`` is given). Overrides ``api_key``.
        cache_dir: Static GTFS cache location (see :meth:`Schedule.load`).
        poll_interval: Minimum time between real-time fetches.
        max_staleness: How long to keep using the last good snapshot when fetches fail.
        description_policy: Thresholds for position descriptions (used when
            this facade loads the schedule itself).
        clock: Returns "now" as an aware datetime; injected for tests.
        http_client: Shared HTTP client for all network access.

    Raises:
        ScheduleUnavailableError: no schedule given, none cached, and the download failed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        schedule: Schedule | None = None,
        realtime: RealtimeSource | None = None,
        cache_dir: Path | None = None,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        max_staleness: timedelta = DEFAULT_MAX_STALENESS,
        description_policy: DescriptionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        http_client: httpx.Client | None = None,
    ):
        self._clock = clock or (lambda: datetime.now(TZ))
        self._schedule = schedule or Schedule.load(
            cache_dir=cache_dir, http_client=http_client,
            description_policy=description_policy, clock=self._clock)
        if realtime is None and api_key:
            realtime = SiriRealtimeSource(api_key, http_client=http_client, clock=self._clock)
        self._realtime = realtime
        self._poll_interval = poll_interval
        self._max_staleness = max_staleness

        self._lock = threading.RLock()
        self._schedule_checked_at = self._now()
        self._last_attempt: datetime | None = None
        self._backoff_until: datetime | None = None
        self._last_good: RealtimeSnapshot | None = None
        self._alerts: list[Alert] = []
        # train number -> ((lat, lon), when this library first saw it there)
        self._first_seen: dict[int, tuple[tuple[float, float], datetime]] = {}
        log.info("CaltrainAPI ready (real-time: %s)", type(realtime).__name__ if realtime else "none")

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    def _resolve_at(self, at: datetime | None, now: datetime) -> datetime:
        if at is None:
            return now
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("datetimes passed to caltrain_api must be timezone-aware")
        return at

    # -- schedule pass-throughs ---------------------------------------------

    @property
    def schedule(self) -> Schedule:
        """The current timetable (replaced, not mutated, by :meth:`refresh_schedule`)."""
        return self._schedule

    def refresh_schedule(self, *, force: bool = False) -> bool:
        """Re-download the static GTFS if stale (or ``force``). Returns True if it changed.

        Also happens automatically: the facade checks staleness at most hourly
        when answering queries.

        Raises:
            ScheduleUnavailableError: only with ``force``, if the download fails.
        """
        with self._lock:
            old = self._schedule
            self._schedule = old.refresh(force=force)
            self._schedule_checked_at = self._now()
            return self._schedule is not old

    def _maybe_refresh_schedule(self, now: datetime) -> Schedule:
        if now - self._schedule_checked_at >= SCHEDULE_CHECK_INTERVAL:
            with self._lock:
                if now - self._schedule_checked_at >= SCHEDULE_CHECK_INTERVAL:
                    self._schedule_checked_at = now
                    try:
                        self._schedule = self._schedule.refresh()
                    except CaltrainError as exc:  # refresh() already degrades; belt and braces
                        log.warning("Schedule refresh failed: %s", exc)
        return self._schedule

    def get_stations(self) -> list[Station]:
        """Served stations in milepost order (SF first)."""
        return self._maybe_refresh_schedule(self._now()).stations()

    def get_station(self, key: str) -> Station:
        """Station by id, platform stop_id, or name. See :meth:`Schedule.station`."""
        return self._maybe_refresh_schedule(self._now()).station(key)

    # -- real-time state ----------------------------------------------------

    def _snapshot_for(self, at: datetime, now: datetime) -> RealtimeSnapshot | None:
        """The real-time snapshot to apply at ``at``, fetching if due; None if not applicable."""
        if self._realtime is None or abs(at - now) > REALTIME_WINDOW:
            return None
        with self._lock:
            due = self._last_attempt is None or now - self._last_attempt >= self._poll_interval
            backing_off = self._backoff_until is not None and now < self._backoff_until
            if due and not backing_off:
                self._fetch(now)
            snap = self._last_good
            if snap is not None and now - snap.fetched_at > self._max_staleness:
                log.info("Real-time data is %s old; using schedule only", now - snap.fetched_at)
                return None
            return snap

    def _fetch(self, now: datetime) -> None:
        assert self._realtime is not None
        self._last_attempt = now
        try:
            snap = self._realtime.fetch()
        except RateLimitedError as exc:
            wait = timedelta(seconds=exc.retry_after) if exc.retry_after else self._poll_interval * 5
            self._backoff_until = now + wait
            log.warning("511 rate limit hit; pausing real-time fetches for %s", wait)
            return
        except RealtimeUnavailableError as exc:
            log.warning("Real-time fetch failed: %s", exc)
            return
        self._backoff_until = None

        if snap.alerts is not None:
            self._alerts = [self._normalize_alert(a) for a in snap.alerts]
        if snap.is_empty:
            # 511 drops to empty feeds mid-service; keep the last good trains.
            log.info("Real-time feed returned no trains; keeping previous snapshot")
            return

        seen: dict[int, tuple[tuple[float, float], datetime]] = {}
        for number, obs in snap.trains.items():
            if obs.lat is None or obs.lon is None:
                continue
            key = (round(obs.lat, 5), round(obs.lon, 5))
            prev = self._first_seen.get(number)
            seen[number] = prev if prev is not None and prev[0] == key else (key, snap.fetched_at)
        self._first_seen = seen
        self._last_good = snap

    def _normalize_alert(self, alert: Alert) -> Alert:
        ids: list[str] = []
        for raw in alert.station_ids:
            try:
                sid = self._schedule.station(raw).id
            except UnknownStationError:
                log.debug("Alert %s cites unknown stop %s", alert.id, raw)
                continue
            if sid not in ids:
                ids.append(sid)
        return alert.model_copy(update={"station_ids": ids})

    # -- combining schedule + real-time --------------------------------------

    def _enrich(self, train: Train, snap: RealtimeSnapshot | None, at: datetime) -> Train:
        """Apply real-time data and alerts to a schedule-only snapshot."""
        alerts = [a for a in self._alerts if train.number in a.train_numbers] if snap else []
        obs = snap.trains.get(train.number) if snap else None
        if obs is not None and obs.service_date is not None and obs.service_date != train.service_date:
            obs = None  # the feed is describing a different day's run of this number
        if obs is None:
            return train.model_copy(update={"alerts": alerts}) if alerts else train
        return self._apply_observation(train, obs, at, alerts)

    def _apply_observation(self, train: Train, obs: TrainObservation, at: datetime,
                           alerts: list[Alert]) -> Train:
        schedule = self._schedule
        by_stop = {c.platform_stop_id: c for c in obs.expected_calls}

        # 1. Expected times onto the stops the feed still lists.
        stops: list[Stop] = []
        for s in train.stops:
            pid = s.station.platform_stop_ids.get(train.direction)
            call = by_stop.get(pid) if pid else None
            if call is None:
                stops.append(s)
                continue
            arr = call.expected_arrival or call.expected_departure
            dep = call.expected_departure or call.expected_arrival
            if arr is not None and dep is not None and dep < arr:
                dep = arr
            stops.append(s.model_copy(update={"expected_arrival": arr, "expected_departure": dep}))

        # 2. Delay = lateness at the first stop with a prediction.
        first = next((s for s in stops if s.expected_arrival is not None), None)
        delay = (first.expected_arrival - first.scheduled_arrival
                 if first is not None and first.expected_arrival is not None else None)

        # 3. For estimation only: stops without predictions (mostly ones already
        #    passed) get scheduled + delay, so interpolation between the last
        #    stop and the next one uses a consistent timeline.
        if delay is not None:
            est_stops = [s if s.expected_arrival is not None else s.model_copy(update={
                "expected_arrival": _shift(s.scheduled_arrival, delay),
                "expected_departure": _shift(s.scheduled_departure, delay)}) for s in stops]
        else:
            est_stops = stops

        if obs.cancelled:
            status = TrainStatus.CANCELLED
        elif at < est_stops[0].departure:
            status = TrainStatus.SCHEDULED
        elif at >= est_stops[-1].arrival:
            status = TrainStatus.COMPLETED
        else:
            status = TrainStatus.EN_ROUTE

        # 4. Reported position: raw lat/lon, milepost by projection.
        reported: TrainPosition | None = None
        if obs.lat is not None and obs.lon is not None:
            seen = self._first_seen.get(train.number)
            seen_at = seen[1] if seen else at
            try:
                loc = schedule.corridor.locate(obs.lat, obs.lon)
                reported = schedule.position_at_milepost(
                    train.direction, est_stops, loc.milepost, seen_at,
                    source=PositionSource.REPORTED, lat_lon=(obs.lat, obs.lon))
            except OffCorridorError as exc:
                log.debug("Train %d report ignored: %s", train.number, exc)

        # 5. Best estimate.
        position: TrainPosition | None
        if delay is None:
            # No predicted times: the report is the best we have.
            position = reported or train.scheduled_position
        else:
            mps = [s.station.location.milepost for s in est_stops]
            times = [(s.arrival, s.departure) for s in est_stops]
            est_mp = _interpolate(mps, times, at)
            if est_mp is None and status is TrainStatus.SCHEDULED:
                est_mp = mps[0]  # waiting at origin
            # Before departure, 511 reports where the train's *equipment* is --
            # often still finishing its previous run miles away -- so the
            # report says nothing about this trip yet.
            if est_mp is not None and reported is not None and status is not TrainStatus.SCHEDULED:
                est_mp = self._reconcile(train.direction, est_mp, reported)
            position = (schedule.position_at_milepost(train.direction, est_stops, est_mp, at,
                                                      source=PositionSource.ESTIMATED)
                        if est_mp is not None else None)

        return train.model_copy(update={
            "stops": stops, "status": status, "delay": delay, "alerts": alerts,
            "position": position, "reported_position": reported,
        })

    @staticmethod
    def _reconcile(direction: Direction, est_mp: float, reported: TrainPosition) -> float:
        """Keep a time-based estimate consistent with the last reported position.

        Trains don't reverse, so the estimate may not be behind the report
        (this catches trains held at a station longer than predicted).

        There is deliberately no limit on how far *ahead* of the report the
        estimate may be: measured against live data (tools/one-off-measure-
        report-lag.py), reports normally trail the predicted timeline by ~1
        minute, but 511 occasionally serves a whole batch of positions ~15
        minutes stale -- and then the predictions are right and the reports
        are wrong.
        """
        sign = 1.0 if direction is Direction.SOUTHBOUND else -1.0
        if sign * (est_mp - reported.milepost) < 0:
            return reported.milepost
        return est_mp

    # -- trains -------------------------------------------------------------

    def get_active_trains(
        self,
        at: datetime | None = None,
        *,
        direction: Direction | None = None,
    ) -> list[Train]:
        """Trains running at ``at`` (default: now), ordered by milepost.

        "Running" means either scheduled to be between origin and destination,
        or present in the real-time feed (which covers late trains still
        running after their scheduled arrival, and trains waiting at their
        origin, whose position relation is WAITING). Trains the real-time data
        shows as completed are omitted.
        """
        now = self._now()
        at = self._resolve_at(at, now)
        schedule = self._maybe_refresh_schedule(now)
        snap = self._snapshot_for(at, now)

        trains = {t.number: t for t in schedule.active_trains(at)}
        if snap is not None:
            for number, obs in snap.trains.items():
                if number in trains:
                    continue
                try:
                    trains[number] = schedule.train(number, obs.service_date, at=at)
                except UnknownTrainError:
                    log.debug("Feed reports train %d, which the schedule doesn't know", number)

        out = [self._enrich(t, snap, at) for t in trains.values()]
        out = [t for t in out if t.status is not TrainStatus.COMPLETED
               and (direction is None or t.direction is direction)]
        out.sort(key=lambda t: t.position.milepost if t.position else float("inf"))
        return out

    def get_train(self, number: int, at: datetime | None = None) -> Train:
        """One train as of ``at`` (default: now).

        Uses the service date the real-time feed reports for ``number`` when
        available; otherwise the run closest to ``at`` (so train 198 at
        12:30 AM finds the previous evening's run).

        Raises:
            UnknownTrainError
        """
        now = self._now()
        at = self._resolve_at(at, now)
        schedule = self._maybe_refresh_schedule(now)
        snap = self._snapshot_for(at, now)
        obs = snap.trains.get(number) if snap else None
        try:
            train = schedule.train(number, obs.service_date if obs else None, at=at)
        except UnknownTrainError:
            if obs is None or obs.service_date is None:
                raise
            train = schedule.train(number, at=at)
        return self._enrich(train, snap, at)

    def get_trains(self, service_date: date | None = None, *, direction: Direction | None = None) -> list[Train]:
        """Every train on ``service_date`` (default: today), in departure order,
        as of now, with real-time data applied to those currently running."""
        now = self._now()
        schedule = self._maybe_refresh_schedule(now)
        sd = service_date or schedule.service_date_for(now)
        snap = self._snapshot_for(now, now)
        return [self._enrich(schedule.train(t.number, sd, at=now), snap, now)
                for t in schedule.trains(sd, direction=direction)]

    def get_departures(
        self,
        station: Station | str,
        at: datetime | None = None,
        *,
        direction: Direction | None = None,
        limit: int | None = 10,
        window: timedelta = timedelta(hours=3),
    ) -> list[Departure]:
        """Upcoming departures from ``station`` after ``at``, by best-available
        departure time (so a late train sorts by when it will actually leave).

        Trains terminating at ``station`` are not departures and are excluded.
        """
        now = self._now()
        at = self._resolve_at(at, now)
        schedule = self._maybe_refresh_schedule(now)
        st = station if isinstance(station, Station) else schedule.station(station)
        snap = self._snapshot_for(at, now)
        end = at + window

        today = schedule.service_date_for(at)
        dates = sorted({today - timedelta(days=1), today, schedule.service_date_for(end)})
        out: list[Departure] = []
        for sd in dates:
            for t0 in schedule.trains(sd, direction=direction):
                s0 = t0.stop_at(st)
                if s0 is None or s0 is t0.stops[-1]:
                    continue
                if not (at - LATE_TRAIN_LOOKBACK <= s0.scheduled_departure <= end):
                    continue
                train = self._enrich(schedule.train(t0.number, sd, at=at), snap, at)
                stop = train.stop_at(st)
                assert stop is not None
                if train.delay is not None and stop.expected_departure is None:
                    continue  # feed has predictions for later stops only: already left here
                if at <= stop.departure <= end:
                    out.append(Departure(train=train, stop=stop))
        out.sort(key=lambda d: d.stop.departure)
        return out[:limit] if limit is not None else out

    def get_schedule(
        self,
        origin: Station | str,
        destination: Station | str,
        service_date: date | None = None,
    ) -> list[Journey]:
        """Point-to-point timetable: every train on ``service_date`` (default:
        today) that stops at ``origin`` and later at ``destination``, by
        scheduled departure from ``origin``.

        Direction follows from the station order. For today's trains, expected
        times are applied where real-time data exists. Use
        :meth:`Schedule.typical_service_date` to pick "a typical weekday".

        Raises:
            UnknownStationError: either station is unknown.
            ValueError: origin and destination are the same station.
        """
        now = self._now()
        schedule = self._maybe_refresh_schedule(now)
        o = origin if isinstance(origin, Station) else schedule.station(origin)
        d = destination if isinstance(destination, Station) else schedule.station(destination)
        if o.id == d.id:
            raise ValueError("origin and destination are the same station")

        out = []
        for train in self.get_trains(service_date):
            ids = [s.station.id for s in train.stops]
            if o.id in ids and d.id in ids and ids.index(o.id) < ids.index(d.id):
                out.append(Journey(train=train, origin_stop=train.stops[ids.index(o.id)],
                                   destination_stop=train.stops[ids.index(d.id)]))
        out.sort(key=lambda j: j.origin_stop.scheduled_departure)
        return out

    # -- alerts & raw data --------------------------------------------------

    def get_alerts(self) -> list[Alert]:
        """Current service alerts (empty without real-time data)."""
        now = self._now()
        self._snapshot_for(now, now)
        return list(self._alerts)

    def get_realtime_snapshot(self) -> RealtimeSnapshot | None:
        """The most recent raw real-time snapshot (fetching if due), or None
        without a real-time source. Mostly for debugging."""
        now = self._now()
        return self._snapshot_for(now, now)
