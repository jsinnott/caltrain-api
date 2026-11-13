"""Shared fixtures.

``tests/fixtures/caltrain-gtfs-20261113.zip`` is a pinned copy of Caltrain's
static GTFS as published 2026-11-13 (valid 2026-11-13 .. 2027-01-31). Tests
that use it assert facts about *that* feed, so they never change when Caltrain
publishes a new one.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from caltrain_api import Schedule

FIXTURES = Path(__file__).parent / "fixtures"
GTFS_ZIP = FIXTURES / "caltrain-gtfs-20261113.zip"
TZ = ZoneInfo("America/Los_Angeles")


def pt(*args: int) -> datetime:
    """Pacific-time datetime: pt(2026, 11, 13, 17, 30)."""
    return datetime(*args, tzinfo=TZ)


@pytest.fixture(scope="session")
def schedule() -> Schedule:
    return Schedule.from_zip(GTFS_ZIP)
