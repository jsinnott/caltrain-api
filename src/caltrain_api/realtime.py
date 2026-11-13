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

from typing import Protocol, runtime_checkable

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .models import Alert


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
        default=None, description="None if this source/fetch did not include alerts."
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

    Implementations do no caching or rate limiting of their own; the facade
    decides when to call :meth:`fetch`.
    """

    def fetch(self) -> RealtimeSnapshot:
        """Fetch current data.

        Raises:
            RateLimitedError: the provider refused for rate limiting.
            RealtimeUnavailableError: any other fetch/parse failure.
        """
        ...


class SiriRealtimeSource:
    """511 SIRI VehicleMonitoring for Caltrain (operator ``CT``), plus service alerts.

    Alerts come from Trillium's keyless GTFS-RT alerts feed (advertised in the
    static GTFS ``linked_datasets.txt``), so they cost nothing against the 511
    quota. They are fetched at most every ``alerts_interval`` seconds and
    included only in the snapshots where they were refreshed.
    """

    VEHICLE_MONITORING_URL = "https://api.511.org/Transit/VehicleMonitoring"
    ALERTS_URL = "http://gtfs-realtime.trilliumtransit.com/gtfs-realtime/feed/caltrain-ca-us/service_alerts.proto"

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        alerts_interval: float = 300.0,
    ):
        raise NotImplementedError

    def fetch(self) -> RealtimeSnapshot:
        raise NotImplementedError
