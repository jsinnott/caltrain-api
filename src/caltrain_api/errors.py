"""Exceptions raised by caltrain_api. All derive from :class:`CaltrainError`."""


class CaltrainError(Exception):
    """Base class for all library errors."""


class ScheduleUnavailableError(CaltrainError):
    """No usable static GTFS: no cached copy and the download failed, or the feed is unparseable."""


class RealtimeUnavailableError(CaltrainError):
    """The real-time source failed and no recent-enough cached snapshot exists.

    The facade normally degrades to schedule-only data instead of raising; this
    surfaces only from direct :class:`RealtimeSource` use.
    """


class RateLimitedError(RealtimeUnavailableError):
    """511 returned HTTP 429. ``retry_after`` is seconds to wait, if the server said."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UnknownStationError(CaltrainError, KeyError):
    """A station id or name matched no station."""


class UnknownTrainError(CaltrainError, KeyError):
    """No train with that number runs on the requested service date."""


class OffCorridorError(CaltrainError, ValueError):
    """A coordinate is too far from the track to be assigned a milepost."""
