"""Real-time data sources.

:class:`RealtimeSource` is the internal seam between the library and any
particular feed. The default implementation, :class:`SiriRealtimeSource`, uses
511's SIRI VehicleMonitoring endpoint, which returns positions plus scheduled
("aimed") and predicted ("expected") times for every remaining stop in a
single request -- half the rate-limit cost of 511's GTFS-RT VehiclePositions +
TripUpdates pair.

Sources return raw observations keyed by train number; turning those into
:class:`~caltrain_api.models.Train` snapshots (mileposts, estimates,
descriptions) is the facade's job.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import httpx
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ._feedcache import USER_AGENT
from .errors import RateLimitedError, RealtimeUnavailableError
from .models import Alert, Direction
from .schedule import TZ, parse_train_number

log = logging.getLogger(__name__)


class ExpectedCall(BaseModel):
    """A predicted call at a station, as reported by the feed."""

    model_config = ConfigDict(frozen=True)

    platform_stop_id: str
    expected_arrival: AwareDatetime | None = None
    expected_departure: AwareDatetime | None = None


class TrainObservation(BaseModel):
    """What the feed says about one train."""

    model_config = ConfigDict(frozen=True)

    train_number: int
    trip_id: str | None = Field(default=None, description="Feed's trip reference, e.g. '124' or 'M147'.")
    service_date: date | None = Field(default=None, description="The trip's service day, if reported.")
    direction: Direction | None = None
    lat: float | None = None
    lon: float | None = None
    expected_calls: list[ExpectedCall] = Field(default_factory=list)
    cancelled: bool = False


class RealtimeSnapshot(BaseModel):
    """One fetch's worth of real-time data."""

    model_config = ConfigDict(frozen=True)

    fetched_at: AwareDatetime
    trains: dict[int, TrainObservation] = Field(default_factory=dict)
    alerts: list[Alert] | None = Field(
        default=None, description="None if this fetch did not refresh alerts."
    )

    @property
    def is_empty(self) -> bool:
        """True when the feed returned no trains.

        511 intermittently returns empty feeds mid-service; callers should treat
        this as "no data", not "no trains running".
        """
        return not self.trains


@runtime_checkable
class RealtimeSource(Protocol):
    """Anything that can produce a :class:`RealtimeSnapshot`.

    Implementations do no caching or rate limiting of their own beyond what
    they document; the facade decides when to call :meth:`fetch`.
    """

    def fetch(self) -> RealtimeSnapshot:
        """Fetch current data.

        Raises:
            RateLimitedError: the provider refused for rate limiting.
            RealtimeUnavailableError: any other fetch/parse failure.
        """
        ...


# ---------------------------------------------------------------------------
# Parsing helpers (module-level so tests can feed them saved responses)
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> list[Any]:
    """SIRI-JSON renders one-element lists as bare objects and empty ones as null."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _siri_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(TZ) if dt.tzinfo else None


def _float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f != 0.0 else None  # 0.0 means "no fix", not the Gulf of Guinea


def parse_vehicle_monitoring(raw: bytes) -> dict[int, TrainObservation]:
    """Parse a 511 SIRI VehicleMonitoring JSON response into observations by train number.

    Raises:
        ValueError: the response is not the expected JSON shape.
    """
    doc = json.loads(raw.decode("utf-8-sig"))  # 511 prefixes a BOM
    try:
        delivery = doc["Siri"]["ServiceDelivery"]["VehicleMonitoringDelivery"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unexpected SIRI structure: missing {exc}") from exc

    trains: dict[int, TrainObservation] = {}
    for delivery_part in _as_list(delivery):
        for activity in _as_list(delivery_part.get("VehicleActivity")):
            journey = activity.get("MonitoredVehicleJourney") or {}
            frame = journey.get("FramedVehicleJourneyRef") or {}
            ref = str(frame.get("DatedVehicleJourneyRef") or journey.get("VehicleRef") or "")
            number = parse_train_number(ref)
            if number is None:
                log.debug("Skipping SIRI activity with unparseable train ref %r", ref)
                continue

            service_date = None
            if frame.get("DataFrameRef"):
                try:
                    service_date = date.fromisoformat(frame["DataFrameRef"])
                except ValueError:
                    pass

            direction = {"N": Direction.NORTHBOUND, "S": Direction.SOUTHBOUND}.get(
                str(journey.get("DirectionRef") or "").upper()[:1])

            loc = journey.get("VehicleLocation") or {}
            lat, lon = _float(loc.get("Latitude")), _float(loc.get("Longitude"))
            if lat is None or lon is None:
                lat = lon = None

            calls: list[ExpectedCall] = []
            seen: set[str] = set()
            onward = journey.get("OnwardCalls") or {}
            for call in _as_list(journey.get("MonitoredCall")) + _as_list(onward.get("OnwardCall")):
                stop_id = str(call.get("StopPointRef") or "")
                if not stop_id or stop_id in seen:
                    continue
                seen.add(stop_id)
                calls.append(ExpectedCall(
                    platform_stop_id=stop_id,
                    expected_arrival=_siri_time(call.get("ExpectedArrivalTime")),
                    expected_departure=_siri_time(call.get("ExpectedDepartureTime")),
                ))

            trains[number] = TrainObservation(
                train_number=number, trip_id=ref, service_date=service_date, direction=direction,
                lat=lat, lon=lon, expected_calls=calls,
            )
    return trains


def _translated(ts: Any) -> str:
    """Pick the English (or first) translation of a GTFS-RT TranslatedString."""
    translations = list(ts.translation)
    for t in translations:
        if t.language.lower().startswith("en"):
            return str(t.text)
    return str(translations[0].text) if translations else ""


def parse_service_alerts(raw: bytes) -> list[Alert]:
    """Parse a GTFS-RT ServiceAlerts protobuf.

    ``station_ids`` holds the feed's raw stop ids (platforms, elevators...);
    the facade normalizes them to station ids.

    Raises:
        ValueError: the bytes are not a GTFS-RT FeedMessage.
    """
    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(raw)
    except DecodeError as exc:
        raise ValueError(f"not a GTFS-RT feed: {exc}") from exc

    alerts = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        a = entity.alert
        numbers = sorted({n for ie in a.informed_entity if ie.HasField("trip")
                          for n in [parse_train_number(ie.trip.trip_id)] if n is not None})
        stop_ids = sorted({ie.stop_id for ie in a.informed_entity if ie.stop_id})
        period = a.active_period[0] if a.active_period else None
        alerts.append(Alert(
            id=entity.id,
            header=_translated(a.header_text),
            description=_translated(a.description_text) or None,
            effect=gtfs_realtime_pb2.Alert.Effect.Name(a.effect) if a.HasField("effect") else None,
            train_numbers=numbers,
            station_ids=stop_ids,
            active_from=datetime.fromtimestamp(period.start, TZ) if period and period.start else None,
            active_until=datetime.fromtimestamp(period.end, TZ) if period and period.end else None,
        ))
    return alerts


# ---------------------------------------------------------------------------
# 511 source
# ---------------------------------------------------------------------------


class SiriRealtimeSource:
    """511 SIRI VehicleMonitoring for Caltrain (operator ``CT``), plus service alerts.

    Alerts come from 511's GTFS-RT ServiceAlerts endpoint. They change slowly,
    so they are fetched at most every ``alerts_interval`` seconds and included
    only in the snapshots where they were refreshed; an alerts failure is
    logged and never fails the snapshot.

    Request budget at the facade's default 120 s poll: ~30 VehicleMonitoring +
    ~6 ServiceAlerts calls per hour, against 511's default 60/hour.

    The API key is sent as a query parameter (511's only option) and is never
    included in exception messages or logs.
    """

    VEHICLE_MONITORING_URL = "https://api.511.org/Transit/VehicleMonitoring"
    SERVICE_ALERTS_URL = "https://api.511.org/Transit/ServiceAlerts"
    OPERATOR = "CT"

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        alerts_interval: float = 600.0,
        clock: Callable[[], datetime] | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self._key = api_key
        self._client = http_client or httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT})
        self._alerts_interval = alerts_interval
        self._clock = clock or (lambda: datetime.now(TZ))
        self._alerts_fetched_at: datetime | None = None

    def _get(self, url: str, label: str, **params: str) -> bytes:
        try:
            resp = self._client.get(url, params={"api_key": self._key, "agency": self.OPERATOR, **params})
        except httpx.HTTPError as exc:
            # str(exc) can embed the request URL, which contains the key.
            raise RealtimeUnavailableError(f"{label}: {type(exc).__name__}") from None
        remaining = resp.headers.get("ratelimit-remaining")
        log.debug("%s: HTTP %d, %d bytes, ratelimit-remaining=%s",
                  label, resp.status_code, len(resp.content), remaining)
        if resp.status_code == 429:
            retry = resp.headers.get("retry-after")
            raise RateLimitedError(f"{label}: rate limited by 511",
                                   retry_after=float(retry) if retry and retry.isdigit() else None)
        if resp.status_code != 200:
            raise RealtimeUnavailableError(f"{label}: HTTP {resp.status_code}")
        return resp.content

    def fetch(self) -> RealtimeSnapshot:
        now = self._clock()
        raw = self._get(self.VEHICLE_MONITORING_URL, "VehicleMonitoring", format="json")
        try:
            trains = parse_vehicle_monitoring(raw)
        except ValueError as exc:
            raise RealtimeUnavailableError(f"VehicleMonitoring: unparseable response: {exc}") from exc
        log.info("Real-time: %d trains", len(trains))

        alerts = None
        due = (self._alerts_fetched_at is None
               or (now - self._alerts_fetched_at).total_seconds() >= self._alerts_interval)
        if due:
            try:
                alerts = parse_service_alerts(self._get(self.SERVICE_ALERTS_URL, "ServiceAlerts"))
                self._alerts_fetched_at = now
                log.info("Real-time: %d alerts", len(alerts))
            except (RealtimeUnavailableError, ValueError) as exc:
                # Includes RateLimitedError: keep the train data we already have,
                # and wait a full interval before trying alerts again.
                self._alerts_fetched_at = now
                log.warning("Service alerts unavailable: %s", exc)

        return RealtimeSnapshot(fetched_at=now, trains=trains, alerts=alerts)
