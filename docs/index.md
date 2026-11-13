# caltrain-api

Caltrain schedules and train positions for Python: where every train is (latitude/longitude, milepost from San Francisco 4th & King, and plain English like "approaching Redwood City"), where it should be, departures, point-to-point timetables, delays and service alerts -- from Caltrain's static GTFS feed and 511.org real-time data.

```bash
pip install caltrain-api
```

```python
from caltrain_api import CaltrainAPI

api = CaltrainAPI(api_key="...")   # a free 511.org key; omit for timetable-only
for train in api.get_active_trains():
    print(train.number, train.position.milepost, train.position.description)
```

Start with the {doc}`overview` (usage, positions, data handling), then the {doc}`api` for every class and method. {doc}`data-findings` records what the data sources actually provide and how that shaped the design.

```{toctree}
:maxdepth: 2
:hidden:

overview
api
data-findings
```
