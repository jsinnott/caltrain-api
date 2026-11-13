"""Track geometry: converting between lat/lon and mileposts.

GTFS publishes 17 directional, per-service-pattern shapes covering the line in
two pieces (SF <-> San Jose/Tamien, San Jose <-> Gilroy). The Corridor stitches
them into one canonical SF -> Gilroy polyline and measures distance along it,
with San Francisco 4th & King defined as milepost 0.0.

Geometry notes: segment lengths use the haversine formula; projecting a point
onto a segment uses a local equirectangular approximation centered on the
point, which is accurate to well under a meter at segment scale.
"""

from __future__ import annotations

import bisect
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

from .errors import OffCorridorError
from .models import TrackLocation

log = logging.getLogger(__name__)

EARTH_RADIUS_MI = 3958.7613
_MI_PER_DEG = EARTH_RADIUS_MI * math.pi / 180.0

LatLon = tuple[float, float]


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class _Projection:
    along: float      # raw distance along the polyline (not origin-adjusted)
    offset: float     # perpendicular distance from the point to the track, miles
    segment: int      # index of the segment the point projects onto
    lat: float        # projected point
    lon: float


class _Polyline:
    """Measured polyline; the shared machinery behind Corridor and stitching."""

    def __init__(self, points: Sequence[LatLon]):
        pts: list[LatLon] = []
        for p in points:
            if not pts or p != pts[-1]:  # drop zero-length segments
                pts.append(p)
        if len(pts) < 2:
            raise ValueError("a polyline needs at least two distinct points")
        self.points = pts
        self.cum = [0.0]
        for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
            self.cum.append(self.cum[-1] + haversine_mi(la1, lo1, la2, lo2))

    def project(self, lat: float, lon: float) -> _Projection:
        kx = math.cos(math.radians(lat)) * _MI_PER_DEG
        ky = _MI_PER_DEG
        best: _Projection | None = None
        best_d2 = math.inf
        pts = self.points
        for i in range(len(pts) - 1):
            # Segment endpoints in a local plane with the query point at the origin.
            ax, ay = (pts[i][1] - lon) * kx, (pts[i][0] - lat) * ky
            bx, by = (pts[i + 1][1] - lon) * kx, (pts[i + 1][0] - lat) * ky
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            d2 = px * px + py * py
            if d2 < best_d2:
                best_d2 = d2
                best = _Projection(
                    along=self.cum[i] + t * (self.cum[i + 1] - self.cum[i]),
                    offset=math.sqrt(d2),
                    segment=i,
                    lat=pts[i][0] + t * (pts[i + 1][0] - pts[i][0]),
                    lon=pts[i][1] + t * (pts[i + 1][1] - pts[i][1]),
                )
        assert best is not None
        return best


class Corridor:
    """The Caltrain line as a single measured polyline.

    Obtain one from :attr:`~caltrain_api.Schedule.corridor`; constructing directly is for tests.
    Immutable and safe to share between threads.
    """

    def __init__(self, points: Sequence[LatLon], origin: LatLon, *, max_offset: float = 0.25):
        """
        Args:
            points: (lat, lon) vertices ordered north to south.
            origin: (lat, lon) of the point defined as milepost 0.0 (SF 4th & King).
                It is projected onto the polyline; vertices north of it get
                negative mileposts.
            max_offset: :meth:`locate` rejects points farther than this many
                miles from the track.
        """
        self._line = _Polyline(points)
        self.max_offset = max_offset
        zero = self._line.project(*origin).along
        self._mp = [c - zero for c in self._line.cum]
        log.debug("Corridor: %d vertices, mileposts %.3f..%.3f",
                  len(self._mp), self._mp[0], self._mp[-1])

    @classmethod
    def from_shapes(
        cls,
        shapes: Sequence[Sequence[LatLon]],
        origin: LatLon,
        *,
        join_tolerance: float = 0.1,
        max_offset: float = 0.25,
    ) -> Corridor:
        """Stitch several overlapping north-to-south shapes into one corridor.

        Starts from the longest shape beginning nearest ``origin``, then
        repeatedly splices on any shape that starts on the current line (within
        ``join_tolerance`` miles) and continues beyond its southern end -- e.g.
        the San Jose -> Gilroy shape onto an SF -> Tamien shape. The splice
        point is where the extension starts; the main line's overlapping tail
        is dropped.
        """
        if not shapes:
            raise ValueError("no shapes")
        lines = [_Polyline(s) for s in shapes]

        def start_dist(line: _Polyline) -> float:
            return haversine_mi(*line.points[0], *origin)

        nearest = min(start_dist(line) for line in lines)
        main = max((line for line in lines if start_dist(line) <= nearest + join_tolerance),
                   key=lambda line: line.cum[-1])
        points = list(main.points)

        while True:
            current = _Polyline(points)
            best: tuple[float, _Polyline, _Projection] | None = None
            for line in lines:
                start = current.project(*line.points[0])
                end = current.project(*line.points[-1])
                if start.offset > join_tolerance or end.offset <= join_tolerance:
                    continue  # doesn't join the line, or doesn't extend past it
                if best is None or end.offset > best[0]:
                    best = (end.offset, line, start)
            if best is None:
                break
            _, ext, at = best
            log.debug("Corridor: splicing %d-point shape at segment %d", len(ext.points), at.segment)
            # Index into current.points (deduplicated), which at.segment refers to.
            points = current.points[: at.segment + 1] + list(ext.points)

        return cls(points, origin, max_offset=max_offset)

    @property
    def length(self) -> float:
        """Milepost of the southern end of the line (Gilroy, ~77)."""
        return self._mp[-1]

    @property
    def start(self) -> float:
        """Milepost of the northern end of the line (0.0 or slightly negative)."""
        return self._mp[0]

    def _locate(self, lat: float, lon: float) -> tuple[TrackLocation, float]:
        p = self._line.project(lat, lon)
        mp = self._mp[p.segment] + (p.along - self._line.cum[p.segment])
        return TrackLocation(lat=p.lat, lon=p.lon, milepost=mp), p.offset

    def locate(self, lat: float, lon: float) -> TrackLocation:
        """Project a lat/lon onto the track and return the projected point with its milepost.

        Raises:
            OffCorridorError: if the point is farther than ``max_offset`` miles
                from the track -- e.g. a bad GPS fix.
        """
        loc, offset = self._locate(lat, lon)
        if offset > self.max_offset:
            raise OffCorridorError(f"({lat}, {lon}) is {offset:.2f} mi from the track")
        return loc

    def offset(self, lat: float, lon: float) -> float:
        """Perpendicular distance in miles from a point to the track (no exception)."""
        return self._locate(lat, lon)[1]

    def at_milepost(self, milepost: float) -> TrackLocation:
        """The point on the track at ``milepost``.

        Raises:
            ValueError: if ``milepost`` is outside ``[start, length]``.
        """
        mp = self._mp
        eps = 1e-9
        if not (mp[0] - eps <= milepost <= mp[-1] + eps):
            raise ValueError(f"milepost {milepost} outside {mp[0]:.3f}..{mp[-1]:.3f}")
        i = max(0, min(len(mp) - 2, bisect.bisect_right(mp, milepost) - 1))
        span = mp[i + 1] - mp[i]
        t = 0.0 if span == 0 else max(0.0, min(1.0, (milepost - mp[i]) / span))
        (la1, lo1), (la2, lo2) = self._line.points[i], self._line.points[i + 1]
        return TrackLocation(lat=la1 + t * (la2 - la1), lon=lo1 + t * (lo2 - lo1), milepost=milepost)

    def polyline(self, *, start: float | None = None, end: float | None = None) -> list[TrackLocation]:
        """Track vertices between two mileposts (default: the whole line), for drawing maps."""
        lo = self._mp[0] if start is None else start
        hi = self._mp[-1] if end is None else end
        if lo > hi:
            raise ValueError("start must not exceed end")
        out = [self.at_milepost(lo)]
        for (lat, lon), mp in zip(self._line.points, self._mp):
            if lo < mp < hi:
                out.append(TrackLocation(lat=lat, lon=lon, milepost=mp))
        if hi > lo:
            out.append(self.at_milepost(hi))
        return out
