# caltrain-api

Caltrain schedules and train positions -- latitude/longitude, milepost from SF 4th & King, and plain-English descriptions ("approaching Redwood City") -- from Caltrain's static GTFS and 511.org real-time data.

**Documentation:** https://caltrain-api.readthedocs.io/ (usage guide and full API reference).

## Install

```bash
pip install caltrain-api
```

Python 3.11+. Real-time data needs a free [511.org API key](https://511.org/open-data/token); without one, everything works from the published timetable.

## Usage

```python
import os
from datetime import date
from caltrain_api import CaltrainAPI, Direction

api = CaltrainAPI(api_key=os.environ.get("CALTRAIN_511_API_KEY"))  # None -> schedule only

for train in api.get_active_trains():
    p = train.position                          # best estimate
    print(train.number, train.service_type.value, f"mp {p.milepost:.1f}",
          p.description, train.delay)           # e.g. 124 local mp 30.0 at Palo Alto 0:01:58

train = api.get_train(124)
train.position             # ESTIMATED: from real-time predicted times
train.reported_position    # REPORTED: raw feed lat/lon (can be minutes stale)
train.scheduled_position   # SCHEDULED: where the timetable says it should be
train.next_stop, train.stops, train.alerts

for d in api.get_departures("Palo Alto", direction=Direction.NORTHBOUND, limit=5):
    print(d.train.number, d.stop.departure, d.stop.delay)

for j in api.get_schedule("Palo Alto", "San Francisco",
                          api.schedule.typical_service_date("weekday", date.today())):
    print(j.train.number, j.origin_stop.scheduled_departure, j.scheduled_duration)

api.get_stations()         # in milepost order, SF first
api.get_alerts()
```

Every method that takes `at=` answers for any moment (timezone-aware datetimes); real-time data applies only within 5 minutes of now.

Positions offer three expressions: `lat`/`lon`, `milepost` (miles along the track from SF 4th & King = 0.0, ~76.8 at Gilroy; computed from GTFS geometry, so close to but not identical with Caltrain's official mileposts), and `description` plus a structured `relation` (`AT`, `PASSING`, `APPROACHING`, `DEPARTED`, `BETWEEN`, `WAITING`, `ARRIVED`). Thresholds (0.2 mi "at", 0.5 mi "approaching/departed") are configurable via `DescriptionPolicy`.

### Data handling

- The static GTFS is downloaded on first use, cached in the user cache dir (`~/Library/Caches/caltrain-api` on macOS), and re-checked daily or when the feed expires. `api.refresh_schedule(force=True)` forces it. Offline, the cached copy is used.
- Real-time data is fetched lazily, at most every 120 s (511 allows 60 requests/hour; positions only change every few minutes). Empty feeds and failures keep the last good data for up to 10 minutes, then fall back to the schedule. Rate limiting backs off automatically.
- `CaltrainAPI` is thread-safe; share one instance.

See [docs/data-findings.md](https://github.com/jsinnott/caltrain-api/blob/master/docs/data-findings.md) for what the data sources actually provide and how that shaped these choices.

## License

MIT

## Development

From a clone of https://github.com/jsinnott/caltrain-api:

```bash
uv sync --extra dev
.venv/bin/pytest tests/unit                                     # offline, pinned fixtures
CALTRAIN_511_API_KEY=... .venv/bin/pytest -s tests/integration   # live; ~2 511 requests
.venv/bin/mypy --strict src
```

API docs (Sphinx, published by Read the Docs; warnings fail the build):

```bash
uv sync --extra docs
.venv/bin/sphinx-build -W --keep-going -b html docs docs/_build/html && open docs/_build/html/index.html
```

Unit tests use a pinned GTFS feed and a captured 511 response (2026-11-13 11:45 PT) in `tests/fixtures/`. `tools/one-off-*.py` are survey/measurement scripts for the live data sources.

## Changelog

- 1.1.0 -- API documentation: Sphinx docs published at https://caltrain-api.readthedocs.io/ (`docs` extra; strict build). `ExpectedCall` is now exported from `caltrain_api` (it was already part of `TrainObservation`). Docstrings added for previously undocumented members. First release published by the GitHub Actions Trusted Publishing workflow.
- 1.0.1 -- Packaging for PyPI: LICENSE file, project URLs, classifiers, `py.typed` marker (type checkers now use the library's annotations), README links that work on PyPI. No code changes.
- 1.0.0 -- Stable release: the public API (everything exported from `caltrain_api`) is now covered by semantic versioning. No code changes from 0.2.1.
- 0.2.1 -- Fix: a train waiting at its origin could be placed miles down the line, because 511 reports its equipment (often finishing a previous run) before departure. Reports no longer constrain estimates before departure, and WAITING/ARRIVED apply only near the origin/destination.
- 0.2.0 -- `CaltrainAPI.get_schedule(origin, destination, service_date)` returning `Journey` objects (point-to-point timetable); `Schedule.typical_service_date("weekday"|"weekend", on_or_after)` picks an ordinary non-holiday date.
- 0.1.0 -- First working release. `Schedule.load`/`refresh` (download, disk cache, conditional revalidation, offline fallback); `SiriRealtimeSource` (511 SIRI VehicleMonitoring + ServiceAlerts); `CaltrainAPI` facade with lazy polling, empty-feed/failure tolerance, rate-limit backoff, estimated/reported/scheduled positions, delays, alerts, `get_departures`.
- 0.0.1 -- Offline timetable: `Corridor`, `DescriptionPolicy`, `Schedule.from_zip`. `Train.trip_id` added; holiday "M"-prefixed trains parsed to plain numbers.
- 0.0.0 -- API stubs for review.
