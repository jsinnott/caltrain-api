"""The static timetable: stations, trips, service calendar, scheduled positions.

Needs no API key. After the first download it works offline from a disk cache.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from .corridor import Corridor
from .describe import DescriptionPolicy
from .models import Direction, Station, Train, TrainPosition

#: Caltrain's static GTFS, as linked from https://www.caltrain.com/developer-resources
DEFAULT_GTFS_URL = "https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip"

#: How old the cached feed may get before :meth:`Schedule.load` re-downloads it.
DEFAULT_MAX_AGE = timedelta(days=1)


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

        Raises:
            ScheduleUnavailableError: no cached copy and the download failed.
        """
        raise NotImplementedError

    @classmethod
    def from_zip(cls, path: Path, *, description_policy: DescriptionPolicy | None = None) -> Schedule:
        """Load from a local GTFS zip with no network access (tests, pinned feeds)."""
        raise NotImplementedError

    def refresh(self, *, force: bool = False) -> Schedule:
        """Return an up-to-date Schedule, downloading only if stale (or ``force``).

        Returns ``self`` when nothing changed.
        """
        raise NotImplementedError

    # -- feed metadata ------------------------------------------------------

    @property
    def feed_version(self) -> str:
        raise NotImplementedError

    @property
    def valid_from(self) -> date:
        raise NotImplementedError

    @property
    def valid_until(self) -> date:
        raise NotImplementedError

    @property
    def downloaded_at(self) -> datetime | None:
        """When the underlying zip was fetched (None for :meth:`from_zip`)."""
        raise NotImplementedError

    @property
    def corridor(self) -> Corridor:
        raise NotImplementedError

    # -- stations -----------------------------------------------------------

    def stations(self, *, include_unserved: bool = False) -> list[Station]:
        """All stations in milepost order (SF first).

        Args:
            include_unserved: include stations no trip stops at (e.g. Stanford).
        """
        raise NotImplementedError

    def station(self, key: str) -> Station:
        """Look up a station by GTFS id ('palo_alto'), platform stop_id ('70171'),
        or name ('Palo Alto', 'palo alto', 'San Jose Diridon'; case- and
        'Station'-suffix-insensitive).

        Raises:
            UnknownStationError
        """
        raise NotImplementedError

    # -- calendar -----------------------------------------------------------

    def service_ids(self, service_date: date) -> frozenset[str]:
        """GTFS service_ids running on ``service_date`` (calendar + calendar_dates exceptions)."""
        raise NotImplementedError

    def holiday(self, service_date: date) -> str | None:
        """Holiday name from the feed if ``service_date`` has modified service, e.g. 'Thanksgiving Day'."""
        raise NotImplementedError

    def service_date_for(self, at: datetime) -> date:
        """The service date a moment *primarily* belongs to (the calendar date in Pacific time).

        Note a moment can be covered by two service days: trips from the previous
        day run past midnight (GTFS times like 25:10:00). Methods that ask
        "what's running at T" consider both.
        """
        raise NotImplementedError

    # -- trains -------------------------------------------------------------

    def trains(
        self,
        service_date: date | None = None,
        *,
        direction: Direction | None = None,
    ) -> list[Train]:
        """All trains scheduled on ``service_date`` (default: today), in departure order.

        Snapshots are taken at the start of that service day, so ``position`` is None.
        """
        raise NotImplementedError

    def train(self, number: int, service_date: date | None = None, *, at: datetime | None = None) -> Train:
        """One scheduled train, as a snapshot at ``at`` (default: now).

        Raises:
            UnknownTrainError: ``number`` doesn't run on ``service_date``.
        """
        raise NotImplementedError

    def active_trains(self, at: datetime | None = None, *, direction: Direction | None = None) -> list[Train]:
        """Trains that, per the timetable, are between origin departure and
        destination arrival at ``at`` (default: now), across both service days
        that can cover that moment. Ordered by milepost.
        """
        raise NotImplementedError

    def scheduled_position(self, train: Train, at: datetime) -> TrainPosition | None:
        """Where ``train`` should be at ``at`` per the timetable (None outside its trip span).

        Interpolates linearly in milepost between consecutive scheduled stops and
        holds position at a station between its arrival and departure times.
        """
        raise NotImplementedError
