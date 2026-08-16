"""Abstract interface all workout data sources must implement."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ..models import Activity, WorkoutData


class WorkoutSourceAuthError(Exception):
    """Raised when authentication with the source fails (bad credentials, expired session)."""


class WorkoutSourceError(Exception):
    """Raised for any other error communicating with the source."""


class WorkoutSourceRateLimitedError(WorkoutSourceError):
    """Raised when the source's API rejects a request for being rate limited.

    Distinguished from other WorkoutSourceErrors so callers doing a long-running
    import (e.g. history backfill) can back off and retry rather than aborting.
    """


class WorkoutSource(ABC):
    """Base class for a single workout/health data provider (Garmin, Apple Health, Fitbit, ...)."""

    #: Short unique key identifying this source, e.g. "garmin". Used in config entry data
    #: and as the `source` field on normalized model instances.
    key: str

    #: Seconds to pause between backfill chunk requests. Conservative default for
    #: sources with unofficial/undocumented rate limits; sources with a known,
    #: generous documented limit (e.g. Strava) may override with a lower value.
    backfill_chunk_pause_seconds: float = 15.0

    #: Extra pause after a fresh login before the first data request, in case the
    #: source's auth endpoint is on a stricter/separate limit from data endpoints.
    post_login_pause_seconds: float = 0.0

    @abstractmethod
    async def async_authenticate(self) -> None:
        """Establish a session with the source. Raise WorkoutSourceAuthError on failure."""

    @abstractmethod
    async def async_fetch(self, target_day: date) -> WorkoutData:
        """Fetch activities and daily summary data for the given day."""

    @abstractmethod
    async def async_fetch_activities_range(
        self, start_day: date, end_day: date
    ) -> list[Activity]:
        """Fetch all activities between start_day and end_day (inclusive). Used for backfill."""

    @classmethod
    @abstractmethod
    def config_schema_fields(cls) -> dict[str, Any]:
        """Describe the config flow fields this source needs (e.g. email/password)."""
