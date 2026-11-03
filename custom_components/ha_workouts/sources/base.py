"""Abstract interface all workout data sources must implement."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from ..models import Activity, WorkoutData


class WorkoutSourceAuthError(Exception):
    """Raised when authentication with the source fails (bad credentials, expired session).

    Covers both a transient, self-recoverable case (e.g. Coros's session was
    silently invalidated because the user logged into the Coros app
    elsewhere — the next call just logs back in automatically) AND a genuine
    "the user needs to do something" case. Callers that need to tell these
    apart (coordinator.py, to decide between a plain retry-later UpdateFailed
    vs. triggering Home Assistant's reauth flow) should catch
    WorkoutSourceReauthRequiredError specifically — it's a subclass, so
    existing code that only catches this base class still sees it.
    """


class WorkoutSourceReauthRequiredError(WorkoutSourceAuthError):
    """Raised specifically when stored credentials are confirmed bad/expired/
    revoked and the only fix is the user supplying new ones — as opposed to
    WorkoutSourceAuthError's broader "something about auth failed right now"
    (which may just mean "log in again automatically and retry").

    Sources should raise this (not the plain WorkoutSourceAuthError) when:
    - A login attempt is rejected outright for bad credentials (not merely an
      expired SESSION that a fresh login would silently fix).
    - A static token/API key (Garmin's Personal API key, Oura's Personal
      Access Token) is rejected — these have no concept of "session expiry
      that a relogin fixes"; a rejection always means the credential itself
      needs regenerating.

    coordinator.py maps this to Home Assistant's ConfigEntryAuthFailed, which
    triggers a reauth flow (see config_flow.py's async_step_reauth) instead
    of silently retrying forever on the normal poll schedule — repeatedly
    resubmitting known-bad credentials to a source's login endpoint risks
    that account getting locked out, and leaves the user with no indication
    anything needs fixing beyond a permanently-unavailable entity.
    """


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
