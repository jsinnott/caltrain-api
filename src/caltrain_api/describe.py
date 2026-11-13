"""Turning a milepost into a station relationship and a human-readable phrase."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Direction, Relation, Station


class DescriptionPolicy(BaseModel):
    """Thresholds and wording rules for :class:`~caltrain_api.models.Relation`.

    Given a train's milepost and direction, the nearest stations ahead and behind
    are classified as:

    - within ``at_radius`` of a station -> ``AT`` (if the train stops there) or
      ``PASSING`` (if not);
    - within ``near_radius`` of the station ahead -> ``APPROACHING``;
    - within ``near_radius`` of the station behind -> ``DEPARTED``;
    - otherwise ``BETWEEN`` the two.

    Where stations are close enough for both APPROACHING and DEPARTED to apply,
    APPROACHING wins. ``WAITING`` and ``ARRIVED`` apply at the origin before
    departure and the destination after arrival, regardless of distance.

    Neighbors are *physical* stations along the track, not the train's stops:
    an express between Palo Alto and Mountain View is "between California
    Avenue and San Antonio" when it is there.
    """

    model_config = ConfigDict(frozen=True)

    at_radius: float = Field(default=0.2, gt=0, description="Miles.")
    near_radius: float = Field(default=0.5, gt=0, description="Miles.")

    @model_validator(mode="after")
    def _ordered(self) -> DescriptionPolicy:
        if self.near_radius <= self.at_radius:
            raise ValueError("near_radius must exceed at_radius")
        return self

    def classify(
        self,
        milepost: float,
        direction: Direction,
        stations: Sequence[Station],
        served: Collection[str],
    ) -> tuple[Relation, Station | None, Station | None, Station | None]:
        """Classify a position.

        Args:
            milepost: The train's milepost.
            direction: Travel direction (determines which neighbor is "ahead").
            stations: All stations, in milepost order.
            served: ids of the stations this train stops at (AT vs PASSING).

        Returns:
            ``(relation, station, previous_station, next_station)`` where
            ``station`` is the one ``relation`` refers to (None for BETWEEN).
            For AT/PASSING, previous/next are the at-station's own neighbors.
            Does not produce WAITING/ARRIVED; the caller decides those from
            trip timing.
        """
        if not stations:
            raise ValueError("no stations")
        # Work in "travel order": southbound = increasing milepost.
        ordered = list(stations) if direction is Direction.SOUTHBOUND else list(reversed(stations))
        sign = 1.0 if direction is Direction.SOUTHBOUND else -1.0

        def progress(s: Station) -> float:
            """How far past station ``s`` the train is, in miles (negative = not there yet)."""
            return sign * (milepost - s.location.milepost)

        nearest_i = min(range(len(ordered)), key=lambda i: abs(progress(ordered[i])))
        nearest = ordered[nearest_i]
        if abs(progress(nearest)) <= self.at_radius:
            prev = ordered[nearest_i - 1] if nearest_i > 0 else None
            nxt = ordered[nearest_i + 1] if nearest_i + 1 < len(ordered) else None
            rel = Relation.AT if nearest.id in served else Relation.PASSING
            return rel, nearest, prev, nxt

        behind = next((s for s in reversed(ordered) if progress(s) > 0), None)
        ahead = next((s for s in ordered if progress(s) < 0), None)
        if ahead is not None and -progress(ahead) <= self.near_radius:
            return Relation.APPROACHING, ahead, behind, ahead
        if behind is not None and progress(behind) <= self.near_radius:
            return Relation.DEPARTED, behind, behind, ahead
        return Relation.BETWEEN, None, behind, ahead

    def phrase(
        self,
        relation: Relation,
        station: Station | None,
        previous_station: Station | None,
        next_station: Station | None,
        *,
        stops_here: bool = True,
    ) -> str:
        """English phrase, e.g. 'departed Palo Alto', 'between California Avenue and San Antonio',
        'waiting to depart San Francisco', 'arrived at San Jose Diridon'.

        ``stops_here`` is whether the train stops at ``station``; DEPARTED from a
        station it ran through reads 'passed X' rather than 'departed X'.
        """
        name = station.name if station else "?"
        match relation:
            case Relation.WAITING:
                return f"waiting to depart {name}"
            case Relation.AT:
                return f"at {name}"
            case Relation.PASSING:
                return f"passing {name}"
            case Relation.APPROACHING:
                return f"approaching {name}"
            case Relation.DEPARTED:
                return f"departed {name}" if stops_here else f"passed {name}"
            case Relation.ARRIVED:
                return f"arrived at {name}"
            case Relation.BETWEEN:
                if previous_station and next_station:
                    return f"between {previous_station.name} and {next_station.name}"
                only = previous_station or next_station
                return f"near {only.name}" if only else "on the line"
        raise ValueError(relation)
