"""Track geometry: converting between lat/lon and mileposts.

GTFS publishes 17 directional, per-service-pattern shapes covering the line in
two pieces (SF <-> San Jose/Tamien, San Jose <-> Gilroy). The Corridor stitches
them into one canonical SF -> Gilroy polyline and measures distance along it,
with San Francisco 4th & King defined as milepost 0.0.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import TrackLocation


class Corridor:
    """The Caltrain line as a single measured polyline.

    Obtain one from :attr:`Schedule.corridor`; constructing directly is for tests.
    Immutable and safe to share between threads.
    """

    def __init__(self, points: Sequence[tuple[float, float]], origin: tuple[float, float]):
        """
        Args:
            points: (lat, lon) vertices ordered north to south.
            origin: (lat, lon) of the point defined as milepost 0.0 (SF 4th & King).
                It is projected onto the polyline; vertices north of it get
                negative mileposts.
        """
        raise NotImplementedError

    @property
    def length(self) -> float:
        """Milepost of the southern end of the line (Gilroy, ~77)."""
        raise NotImplementedError

    def locate(self, lat: float, lon: float) -> TrackLocation:
        """Project a lat/lon onto the track and return the projected point with its milepost.

        Raises:
            OffCorridorError: if the point is farther than ``max_offset_mi``
                (default 0.25 mi) from the track -- e.g. a bad GPS fix or a
                train in a yard off the main line.
        """
        raise NotImplementedError

    def offset(self, lat: float, lon: float) -> float:
        """Perpendicular distance in miles from a point to the track (no exception)."""
        raise NotImplementedError

    def at_milepost(self, milepost: float) -> TrackLocation:
        """The point on the track at ``milepost``.

        Raises:
            ValueError: if ``milepost`` is outside ``[min milepost, length]``.
        """
        raise NotImplementedError

    def polyline(self, *, start: float | None = None, end: float | None = None) -> list[TrackLocation]:
        """Track vertices between two mileposts (default: the whole line), for drawing maps."""
        raise NotImplementedError
