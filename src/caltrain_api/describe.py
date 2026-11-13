"""Turning a milepost into a station relationship and a human-readable phrase."""

from __future__ import annotations

from collections.abc import Sequence

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
        served: frozenset[str],
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
            Does not produce WAITING/ARRIVED; the caller decides those from
            trip timing.
        """
        raise NotImplementedError

    def phrase(
        self,
        relation: Relation,
        station: Station | None,
        previous_station: Station | None,
        next_station: Station | None,
    ) -> str:
        """English phrase, e.g. 'departed Palo Alto', 'between California Avenue and San Antonio',
        'waiting to depart San Francisco', 'arrived at San Jose Diridon'."""
        raise NotImplementedError
