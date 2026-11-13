# Data source findings

Survey of Caltrain's static GTFS and 511's real-time feeds, taken 2026-11-13
(a Friday, ~11:45 PT) with `tools/one-off-explore-static-gtfs.py` and
`tools/one-off-explore-realtime.py`. Re-run those scripts to re-check; feed
behavior can change without notice.

## Static GTFS

- Source: `https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip`
  (linked from caltrain.com/developer-resources). ~170 KB, no key. Feed is
  republished frequently; `feed_info.txt` carries `feed_start_date` /
  `feed_end_date` (this one: 2026-11-13 .. 2027-01-31).
- **Stations**: 31 parent stations (`location_type=1`), each with NB and SB
  platform children (`70011`/`70012` etc.; Stanford's are `2537740`/`2537744`),
  plus elevator entrances (`location_type=2`). `stop_times` references
  platforms only. Stanford exists but no trip stops there.
- **Trains**: `trip_id == trip_short_name ==` train number for all trips.
  `direction_id` 0 = North (odd numbers), 1 = South (even numbers), confirmed
  by `directions.txt`.
- **Service type**: `routes.txt` short names are "Local Weekday", "Local
  Weekend", "Limited", "Express", "South County". `realtime_routes.txt`
  collapses them to clean names (Local / Limited / Express / South County).
- **Skip-stop**: express/limited trips simply omit stations they don't serve
  (7-24 stops per trip); `pickup_type`/`drop_off_type` are always 0. Knowing
  what a train *passes* requires station mileposts independent of the trip.
- **Shapes**: 17 directional, per-pattern shapes. Corridor is covered in two
  pieces: SF <-> San Jose Diridon/Tamien (~46.9 / 48.7 mi) and Diridon <->
  Gilroy (~30.0 mi). `shape_dist_traveled` is present on every point and every
  stop_time, **in meters**. A single SF->Gilroy milepost axis must be stitched
  from these.
- **Calendar**: weekday and weekend services in `calendar.txt`; a third,
  holiday-only service (`c_71743...`, Local + South County, 79 trips) appears
  only in `calendar_dates.txt`, which also has `holiday_name`. Times run past
  midnight (max `25:29:00`, 161 rows >= 24:00).
- **Data-quality note**: `calendar_dates.txt` labels 2027-01-27 (a Wednesday)
  as Martin Luther King Jr. Day; MLK Day 2027 is Monday 2027-01-18. The library
  should follow the feed, but this is a reminder the feed can be wrong.
- `linked_datasets.txt` points at a **keyless** Trillium service-alerts
  GTFS-RT feed.

## 511 real-time (agency=CT)

- **Rate limit**: 60/hour per key, reported in `ratelimit-limit` /
  `ratelimit-remaining` headers. `remaining` was non-monotonic across calls
  (59,58,57,56,55,58,54,58), suggesting several backend counters -- don't rely
  on it for precise budgeting.
- **VehiclePositions** (GTFS-RT): real lat/lon (not station-snapped -- 44 of
  69 observations were mid-segment), but only `trip`, `vehicle`, `position.
  {lat,lon}`, `timestamp` are populated -- no bearing, speed, stop status.
  **Positions refresh only every ~3-4 minutes** (11:45 -> 11:48 -> 11:52) while
  the per-vehicle `timestamp` advances every minute, so the timestamp is not
  the fix time. At line speed a fix can be several miles stale.
- **Feed dropouts**: 2 of 10 VehiclePositions samples came back empty (15
  bytes, 0 entities) mid-service. Empty must mean "no data", not "no trains".
- Trains staged at their origin before departure (SF, Tamien, Diridon) appear
  in the feed.
- **TripUpdates**: absolute predicted `arrival.time`/`departure.time` per
  remaining stop; `delay` fields are all 0 and `stop_sequence` is 0 -- delay
  must be computed against the static schedule. Example: train 123 predicted
  at Mountain View 11:44:45 vs scheduled 10:46 (~59 min late) while showing
  `delay=0`. Cancellation (per alerts) was *not* reflected in
  `schedule_relationship` (all SCHEDULED).
- **ServiceAlerts**: useful structured `informed_entity` (trip ids, stop ids)
  and effects (SIGNIFICANT_DELAYS, MODIFIED_SERVICE), plus free text.
  Sometimes contradictory (train 123 both "Cancelled" and "38 minutes late").
- **SIRI VehicleMonitoring** (`format=json`): one call returns, per train,
  the same position as VehiclePositions *plus* Aimed (scheduled) and Expected
  times for the monitored call and all onward calls. One request ~= VP + TU.
  Times are UTC ISO-8601; JSON has a UTF-8 BOM.
- Real-time trip ids match static trip ids. Real-time `route_id` is the route
  *short name* ("Local Weekday"), not the static numeric `route_id`.
