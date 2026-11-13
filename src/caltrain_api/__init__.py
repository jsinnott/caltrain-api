"""Caltrain schedules and train positions.

Public API -- everything importable from ``caltrain_api`` directly is covered
by semantic versioning; submodule internals are not.
"""

__version__ = "0.1.0"

from .api import CaltrainAPI
from .corridor import Corridor
from .describe import DescriptionPolicy
from .errors import (
    CaltrainError,
    OffCorridorError,
    RateLimitedError,
    RealtimeUnavailableError,
    ScheduleUnavailableError,
    UnknownStationError,
    UnknownTrainError,
)
from .models import (
    Alert,
    Departure,
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
from .realtime import RealtimeSnapshot, RealtimeSource, SiriRealtimeSource, TrainObservation
from .schedule import Schedule

__all__ = [
    "Alert",
    "CaltrainAPI",
    "CaltrainError",
    "Corridor",
    "Departure",
    "DescriptionPolicy",
    "Direction",
    "OffCorridorError",
    "PositionSource",
    "RateLimitedError",
    "RealtimeSnapshot",
    "RealtimeSource",
    "RealtimeUnavailableError",
    "Relation",
    "Schedule",
    "ScheduleUnavailableError",
    "ServiceType",
    "SiriRealtimeSource",
    "Station",
    "Stop",
    "TrackLocation",
    "Train",
    "TrainObservation",
    "TrainPosition",
    "TrainStatus",
    "UnknownStationError",
    "UnknownTrainError",
]
