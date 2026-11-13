"""CaltrainAPI: the main entry point, combining the timetable with real-time data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from .describe import DescriptionPolicy
from .models import Alert, Departure, Direction, Station, Train
from .realtime import RealtimeSnapshot, RealtimeSource
from .schedule import Schedule

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


class CaltrainAPI:
    """Caltrain schedules and train positions.

    Without an API key, everything works from the published timetable
    (positions are SCHEDULED). With a 511 key, trains also carry reported
    positions, predicted times, delays, and best-estimate positions.

    Thread-safe: one instance can serve a long-running, concurrent application.
    Real-time data is fetched lazily -- when a method needs it and the last
    fetch is older than ``poll_interval`` -- so call as often as you like.

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
        description_policy: Thresholds for position descriptions.
        clock: Returns "now" as an aware datetime; injected for tests.
        http_client: Shared HTTP client for all network access.
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
        raise NotImplementedError

    # -- schedule pass-throughs ---------------------------------------------

    @property
    def schedule(self) -> Schedule:
        """The current timetable (replaced, not mutated, by :meth:`refresh_schedule`)."""
        raise NotImplementedError

    def refresh_schedule(self, *, force: bool = False) -> bool:
        """Re-download the static GTFS if stale (or ``force``). Returns True if it changed.

        Also happens automatically: the facade checks staleness at most hourly
        when answering queries.
        """
        raise NotImplementedError

    def get_stations(self) -> list[Station]:
        """Served stations in milepost order (SF first)."""
        raise NotImplementedError

    def get_station(self, key: str) -> Station:
        """Station by id, platform stop_id, or name. See :meth:`Schedule.station`."""
        raise NotImplementedError

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
        origin, whose position relation is WAITING).
        """
        raise NotImplementedError

    def get_train(self, number: int, at: datetime | None = None) -> Train:
        """One train as of ``at`` (default: now).

        Chooses the service date whose run of ``number`` is closest to ``at``
        (so train 198 at 12:30 AM finds the previous evening's run).

        Raises:
            UnknownTrainError
        """
        raise NotImplementedError

    def get_trains(self, service_date: date | None = None, *, direction: Direction | None = None) -> list[Train]:
        """Every train on ``service_date`` (default: today), in departure order,
        with real-time data applied to those currently running."""
        raise NotImplementedError

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
        departure time (so a late train sorts by when it will actually leave)."""
        raise NotImplementedError

    # -- alerts & raw data --------------------------------------------------

    def get_alerts(self) -> list[Alert]:
        """Current service alerts (empty without real-time data)."""
        raise NotImplementedError

    def get_realtime_snapshot(self) -> RealtimeSnapshot | None:
        """The most recent raw real-time snapshot (fetching if due), or None
        without a real-time source. Mostly for debugging."""
        raise NotImplementedError
