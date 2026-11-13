API reference
=============

Everything here is importable directly from ``caltrain_api`` and is covered by
semantic versioning.

.. currentmodule:: caltrain_api

Facade
------

Most applications need only :class:`CaltrainAPI`: it combines the timetable
with real-time data and returns immutable snapshots.

.. autoclass:: CaltrainAPI

Trains and positions
--------------------

.. autopydantic_model:: Train
.. autopydantic_model:: TrainPosition
.. autopydantic_model:: Stop
.. autopydantic_model:: Departure
.. autopydantic_model:: Journey
.. autopydantic_model:: Alert

Stations and track
------------------

.. autopydantic_model:: Station
.. autopydantic_model:: TrackLocation
.. autoclass:: Corridor

Enumerations
------------

.. autoclass:: Direction
   :members:
   :undoc-members:
.. autoclass:: ServiceType
   :members:
   :undoc-members:
.. autoclass:: PositionSource
   :members:
   :undoc-members:
.. autoclass:: Relation
   :members:
   :undoc-members:
.. autoclass:: TrainStatus
   :members:
   :undoc-members:

Timetable
---------

:class:`Schedule` works without an API key or (after the first download)
network access. :class:`CaltrainAPI` builds on it; use it directly for
timetable-only applications.

.. autoclass:: Schedule

Descriptions
------------

.. autopydantic_model:: DescriptionPolicy

Real-time sources
-----------------

The default source is :class:`SiriRealtimeSource`. Implement
:class:`RealtimeSource` to plug in another feed.

.. autoclass:: RealtimeSource
.. autoclass:: SiriRealtimeSource
.. autopydantic_model:: RealtimeSnapshot
.. autopydantic_model:: TrainObservation
.. autopydantic_model:: ExpectedCall

Errors
------

.. autoexception:: CaltrainError
.. autoexception:: ScheduleUnavailableError
.. autoexception:: RealtimeUnavailableError
.. autoexception:: RateLimitedError
.. autoexception:: UnknownStationError
.. autoexception:: UnknownTrainError
.. autoexception:: OffCorridorError
