# caltrain-api

Caltrain schedules and train positions -- latitude/longitude, milepost from SF 4th & King, and plain-English descriptions ("approaching Redwood City") -- from Caltrain static GTFS and 511.org real-time data.

Status: pre-release. The timetable side (`Schedule`, `Corridor`, `DescriptionPolicy`) works offline from a local GTFS zip; downloading/caching the feed and real-time data (`CaltrainAPI`) are not implemented yet. See `docs/data-findings.md` for what the data sources actually provide.

## Using the timetable (works today)

```python
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from caltrain_api import Schedule

schedule = Schedule.from_zip(Path("caltrain-gtfs.zip"))
at = datetime(2026, 11, 13, 17, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
for train in schedule.active_trains(at):
    p = train.position
    print(train.number, train.service_type.value, f"mp {p.milepost:.1f}", p.description)
```

## License

MIT

## Development

```bash
uv sync --extra dev
.venv/bin/pytest tests/unit
.venv/bin/mypy --strict src
```

Unit tests run against a pinned copy of the GTFS feed in `tests/fixtures/`, so they don't change when Caltrain publishes a new one. `tools/one-off-explore-*.py` survey the live data sources (the real-time one needs `CALTRAIN_511_API_KEY`).

## Changelog

- 0.0.1 -- Offline timetable: `Corridor` (stitched SF->Gilroy track, mileposts), `DescriptionPolicy` (at/passing/approaching/departed/passed/between), `Schedule.from_zip` (stations, service calendar incl. holidays and past-midnight trips, scheduled positions). `Train.trip_id` added; holiday "M"-prefixed trains parsed to plain numbers. (unreleased)
- 0.0.0 -- API stubs for review (unreleased).
