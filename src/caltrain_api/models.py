"""Immutable data types returned by the caltrain_api library.

Everything here is a frozen pydantic model: a *snapshot* of what the library
knew at a moment in time. Nothing here performs I/O or changes after
construction; to see newer data, ask the API again.

All datetimes are timezone-aware. The library produces them in
America/Los_Angeles (Caltrain's agency timezone); convert with
``dt.astimezone(...)`` as needed.

Distances are in statute miles. A *milepost* is distance along the track from
San Francisco 4th & King (milepost 0.0), increasing southward toward Gilroy
(roughly 77). Mileposts are computed from the GTFS track geometry, so they
will not exactly match Caltrain's official chaining.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    """Direction of travel. Northbound trains have odd numbers, southbound even."""

    NORTHBOUND = "northbound"
    SOUTHBOUND = "southbound"


class ServiceType(str, Enum):
    """Caltrain's service patterns, from GTFS ``realtime_routes.txt``.

    Weekday and weekend locals are both ``LOCAL``; whether a train runs on a
    weekday or weekend schedule is a property of its service date, not its type.
    """

    LOCAL = "local"
    LIMITED = "limited"
    EXPRESS = "express"
    SOUTH_COUNTY = "south_county"


class PositionSource(str, Enum):
    """How a :class:`TrainPosition` was obtained.

    - ``REPORTED``: the raw location from the real-time feed. Real but stale --
      511 refreshes Caltrain positions only every few minutes.
    - ``ESTIMATED``: interpolated along the track using real-time *predicted*
      arrival/departure times, cross-checked against the latest report. This is
      the library's best guess and the default :attr:`Train.position`.
    - ``SCHEDULED``: interpolated along the track from the published timetable
      alone. This is where the train *should* be.
    """

    REPORTED = "reported"
    ESTIMATED = "estimated"
    SCHEDULED = "scheduled"


class Relation(str, Enum):
    """A train position's relationship to the nearest relevant station.

    Used to build :attr:`TrainPosition.description`; exposed separately so UIs
    can render their own wording or icons. Thresholds are configurable via
    :class:`~caltrain_api.describe.DescriptionPolicy`.
    """

    WAITING = "waiting"            # at origin, before scheduled/expected departure
    AT = "at"                      # at a station this train stops at
    PASSING = "passing"            # at a station this train does not stop at
    APPROACHING = "approaching"    # near the next station ahead
    DEPARTED = "departed"          # near the station just behind
    BETWEEN = "between"            # none of the above
    ARRIVED = "arrived"            # at destination, trip complete


class TrainStatus(str, Enum):
    """Where a train is in its trip's lifecycle, as of the snapshot."""

    SCHEDULED = "scheduled"        # not yet departed origin
    EN_ROUTE = "en_route"
    COMPLETED = "completed"
    CANCELLED = "cancelled"        # only when real-time data says so explicitly


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


class TrackLocation(_Frozen):
    """A point on (or projected onto) the Caltrain corridor.

    The common currency for anything with a place on the line: stations,
    train positions, points along the track polyline.
    """

    lat: float
    lon: float
    milepost: float = Field(
        description="Miles along the track from SF 4th & King (0.0). May be slightly "
        "negative for equipment north of the SF platform."
    )


class Station(_Frozen):
    """A physical Caltrain station.

    GTFS models each station as a parent stop with separate northbound and
    southbound platform stops; this class represents the station as a whole.
    """

    id: str = Field(description="GTFS parent stop_id, e.g. 'palo_alto'. Stable across feeds.")
    name: str = Field(description="Display name, e.g. 'Palo Alto' (trailing 'Station' removed).")
    location: TrackLocation
    platform_stop_ids: dict[Direction, str] = Field(
        description="GTFS platform stop_ids by direction, for correlating with raw GTFS data."
    )
    has_scheduled_service: bool = Field(
        description="False for stations present in GTFS but served by no trip in the "
        "current feed (e.g. Stanford, which opens only for events)."
    )


# ---------------------------------------------------------------------------
# Trains
# ---------------------------------------------------------------------------


class Stop(_Frozen):
    """One scheduled call of a train at a station."""

    station: Station
    scheduled_arrival: AwareDatetime
    scheduled_departure: AwareDatetime
    expected_arrival: AwareDatetime | None = Field(
        default=None, description="Real-time prediction; None if unavailable or already passed."
    )
    expected_departure: AwareDatetime | None = None

    @property
    def arrival(self) -> datetime:
        """Best available arrival time: expected if known, else scheduled."""
        return self.expected_arrival or self.scheduled_arrival

    @property
    def departure(self) -> datetime:
        """Best available departure time: expected if known, else scheduled."""
        return self.expected_departure or self.scheduled_departure

    @property
    def delay(self) -> timedelta | None:
        """Predicted departure lateness (negative = early); None without real-time data."""
        if self.expected_departure is None:
            return None
        return self.expected_departure - self.scheduled_departure


class TrainPosition(TrackLocation):
    """Where a train is (or should be), with context.

    A ``TrackLocation`` plus when, how we know, and how it relates to stations.
    ``station``/``previous_station``/``next_station`` refer to *physical*
    neighbors along the track, including stations the train does not stop at;
    use :attr:`Train.next_stop` for the next station it will serve.
    """

    source: PositionSource
    as_of: AwareDatetime = Field(
        description="When this position applies. For REPORTED positions this is when the "
        "library first saw the feed report this location -- 511's own timestamps do not "
        "reflect fix time -- so the train may have been there somewhat earlier."
    )
    direction: Direction
    relation: Relation
    station: Station | None = Field(
        default=None,
        description="The station that ``relation`` refers to (None for BETWEEN).",
    )
    previous_station: Station | None = Field(
        default=None, description="Nearest station behind the train (None before origin)."
    )
    next_station: Station | None = Field(
        default=None, description="Nearest station ahead of the train (None past the end)."
    )
    description: str = Field(
        description="Human-readable summary, e.g. 'approaching Redwood City', "
        "'between California Avenue and San Antonio'."
    )


class Alert(_Frozen):
    """A Caltrain service alert (delay, cancellation, platform change, elevator outage...).

    Alerts come from free-text operational messages and can be contradictory
    (e.g. a train both 'cancelled' and 'running late'); treat them as
    informational, not authoritative state.
    """

    id: str
    header: str
    description: str | None = None
    effect: str | None = Field(
        default=None, description="GTFS-RT effect name, e.g. 'SIGNIFICANT_DELAYS'."
    )
    train_numbers: list[int] = Field(default_factory=list)
    station_ids: list[str] = Field(default_factory=list)
    active_from: AwareDatetime | None = None
    active_until: AwareDatetime | None = None


class Train(_Frozen):
    """A snapshot of one Caltrain trip on one service day.

    Created by :class:`~caltrain_api.CaltrainAPI` (schedule + real-time) or
    :class:`~caltrain_api.Schedule` (schedule only, in which case real-time
    fields are None and ``position`` equals ``scheduled_position``).
    """

    number: int = Field(description="Train number, e.g. 123. Odd = northbound.")
    trip_id: str = Field(
        description="Raw GTFS trip_id, e.g. '123', or 'M147' on holiday (modified) schedules."
    )
    direction: Direction
    service_type: ServiceType
    service_date: date = Field(
        description="The GTFS service day this trip belongs to. A train running at 12:30 AM "
        "Saturday on Friday's schedule has Friday's service_date."
    )
    as_of: AwareDatetime = Field(description="The moment this snapshot describes.")
    status: TrainStatus
    stops: list[Stop] = Field(description="Calls in travel order, origin first.")

    position: TrainPosition | None = Field(
        default=None,
        description="Best estimate: ESTIMATED when real-time data exists, else SCHEDULED. "
        "None when the train is not running at ``as_of``.",
    )
    reported_position: TrainPosition | None = Field(
        default=None, description="Raw real-time location, if the feed reports this train."
    )
    scheduled_position: TrainPosition | None = Field(
        default=None, description="Timetable position at ``as_of``; None outside the trip's span."
    )
    delay: timedelta | None = Field(
        default=None,
        description="Current lateness (expected vs scheduled at the next stop); None without "
        "real-time data.",
    )
    alerts: list[Alert] = Field(default_factory=list)

    @property
    def origin(self) -> Station:
        return self.stops[0].station

    @property
    def destination(self) -> Station:
        return self.stops[-1].station

    @property
    def stations(self) -> list[Station]:
        """Stations this train stops at, in travel order."""
        return [s.station for s in self.stops]

    @property
    def next_stop(self) -> Stop | None:
        """First stop whose best-available departure is after ``as_of``.

        While the train dwells at a station, that station is still the next stop.
        """
        return next((s for s in self.stops if s.departure > self.as_of), None)

    @property
    def previous_stop(self) -> Stop | None:
        """Last stop whose best-available departure is at or before ``as_of``."""
        return next((s for s in reversed(self.stops) if s.departure <= self.as_of), None)

    def stop_at(self, station: Station | str) -> Stop | None:
        """This train's stop at ``station`` (a Station, id, or name), or None if it doesn't stop there.

        String matching is case-insensitive against the station id and name.
        """
        key = station.id if isinstance(station, Station) else station.strip().lower()
        for s in self.stops:
            if key in (s.station.id, s.station.id.lower(), s.station.name.lower()):
                return s
        return None


class Departure(_Frozen):
    """A train's upcoming call at a particular station (one row of a departure board)."""

    train: Train
    stop: Stop
