"""Garmin Connect data source, backed by the unofficial python-garminconnect library."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from homeassistant.core import HomeAssistant

from ..models import Activity, ActivityType, DailySummary, WorkoutData
from .base import (
    WorkoutSource,
    WorkoutSourceAuthError,
    WorkoutSourceError,
    WorkoutSourceRateLimitedError,
)

_LOGGER = logging.getLogger(__name__)

# Garmin's own activity type keys, mapped to our normalized ActivityType.
_ACTIVITY_TYPE_MAP: dict[str, ActivityType] = {
    "running": ActivityType.RUNNING,
    "treadmill_running": ActivityType.RUNNING,
    "trail_running": ActivityType.RUNNING,
    "cycling": ActivityType.CYCLING,
    "road_biking": ActivityType.CYCLING,
    "mountain_biking": ActivityType.CYCLING,
    "lap_swimming": ActivityType.SWIMMING,
    "open_water_swimming": ActivityType.SWIMMING,
    "walking": ActivityType.WALKING,
    "hiking": ActivityType.HIKING,
    "strength_training": ActivityType.STRENGTH_TRAINING,
    "yoga": ActivityType.YOGA,
}


def _map_activity_type(garmin_type: str | None) -> ActivityType:
    if not garmin_type:
        return ActivityType.OTHER
    return _ACTIVITY_TYPE_MAP.get(garmin_type.lower(), ActivityType.OTHER)


def _parse_garmin_datetime(value: str | None) -> datetime:
    """Parse Garmin's 'YYYY-MM-DD HH:MM:SS' local-time strings into a naive datetime.

    Deliberately naive: Garmin's API returns this in the account's local time
    with no offset, and we have no way to know what timezone that is from the
    string alone (guessing UTC would be actively wrong, not just imprecise).
    Only .date() is ever read from this downstream (see
    sensor.py/statistics_import.py), so the missing tzinfo doesn't affect
    correctness there.
    """
    if not value:
        return datetime.min  # noqa: DTZ901
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007


class GarminSource(WorkoutSource):
    """Fetches activity and daily summary data from Garmin Connect."""

    key = "garmin"
    # Garmin's API is unofficial and undocumented — no published rate limit to
    # target, so pace conservatively. sso/signin (login) appears to be on a
    # stricter/separate limit from the data endpoints (observed 429s there
    # specifically), hence the extra post-login cooldown.
    backfill_chunk_pause_seconds = 20.0
    post_login_pause_seconds = 3.0

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        self._hass = hass
        self._email = email
        self._password = password
        self._client: Garmin | None = None
        # The coordinator's periodic poll and the background history backfill both
        # call async_fetch*/async_authenticate on this same shared instance. Without
        # serializing logins, an expired-session moment during a long backfill can
        # trigger two concurrent login attempts against Garmin's sso/signin endpoint
        # — exactly the kind of burst that trips its rate limit.
        self._auth_lock = asyncio.Lock()

    async def async_authenticate(self) -> None:
        async with self._auth_lock:
            if self._client is not None:
                # Another caller already re-authenticated while we were waiting.
                return
            client = Garmin(email=self._email, password=self._password)
            try:
                await self._hass.async_add_executor_job(client.login)
            except GarminConnectAuthenticationError as err:
                raise WorkoutSourceAuthError(
                    "Invalid Garmin Connect email or password"
                ) from err
            except GarminConnectTooManyRequestsError as err:
                raise WorkoutSourceRateLimitedError(
                    f"Garmin Connect rate limited the login: {err}"
                ) from err
            except GarminConnectConnectionError as err:
                raise WorkoutSourceError(f"Could not connect to Garmin Connect: {err}") from err
            self._client = client
            if self.post_login_pause_seconds:
                await asyncio.sleep(self.post_login_pause_seconds)

    async def _async_ensure_authenticated(self) -> None:
        if self._client is None:
            await self.async_authenticate()

    async def async_fetch(self, target_day: date) -> WorkoutData:
        await self._async_ensure_authenticated()
        assert self._client is not None

        day_str = target_day.isoformat()
        try:
            raw_activities = await self._hass.async_add_executor_job(
                self._client.get_activities_by_date, day_str, day_str
            )
            raw_stats = await self._hass.async_add_executor_job(
                self._client.get_stats, day_str
            )
        except GarminConnectAuthenticationError as err:
            # Session expired; force a fresh login on the next refresh.
            self._client = None
            raise WorkoutSourceAuthError("Garmin Connect session expired") from err
        except GarminConnectTooManyRequestsError as err:
            raise WorkoutSourceRateLimitedError(f"Garmin Connect rate limited us: {err}") from err
        except GarminConnectConnectionError as err:
            raise WorkoutSourceError(f"Error fetching Garmin Connect data: {err}") from err

        activities = [self._parse_activity(item) for item in raw_activities or []]
        summary = self._parse_daily_summary(raw_stats, target_day)

        return WorkoutData(activities=activities, daily_summary=summary)

    async def async_fetch_activities_range(
        self, start_day: date, end_day: date
    ) -> list[Activity]:
        """Fetch all activities between start_day and end_day (inclusive). Used for backfill."""
        await self._async_ensure_authenticated()
        assert self._client is not None

        try:
            raw_activities = await self._hass.async_add_executor_job(
                self._client.get_activities_by_date,
                start_day.isoformat(),
                end_day.isoformat(),
            )
        except GarminConnectAuthenticationError as err:
            self._client = None
            raise WorkoutSourceAuthError("Garmin Connect session expired") from err
        except GarminConnectTooManyRequestsError as err:
            raise WorkoutSourceRateLimitedError(f"Garmin Connect rate limited us: {err}") from err
        except GarminConnectConnectionError as err:
            raise WorkoutSourceError(f"Error fetching Garmin Connect data: {err}") from err

        return [self._parse_activity(item) for item in raw_activities or []]

    def _parse_activity(self, item: dict[str, Any]) -> Activity:
        activity_type_key = (item.get("activityType") or {}).get("typeKey")
        return Activity(
            source=self.key,
            source_id=str(item.get("activityId")),
            activity_type=_map_activity_type(activity_type_key),
            start=_parse_garmin_datetime(item.get("startTimeLocal")),
            duration_seconds=float(item.get("duration") or 0),
            distance_meters=item.get("distance"),
            calories=item.get("calories"),
            avg_heart_rate=item.get("averageHR"),
            max_heart_rate=item.get("maxHR"),
            elevation_gain_meters=item.get("elevationGain"),
            name=item.get("activityName"),
        )

    def _parse_daily_summary(self, stats: dict[str, Any], target_day: date) -> DailySummary:
        return DailySummary(
            source=self.key,
            day=target_day,
            steps=stats.get("totalSteps"),
            resting_heart_rate=stats.get("restingHeartRate"),
            active_calories=stats.get("activeKilocalories"),
            floors_climbed=stats.get("floorsAscended"),
            stress_avg=stats.get("averageStressLevel"),
            body_battery_max=stats.get("bodyBatteryHighestValue"),
            body_battery_min=stats.get("bodyBatteryLowestValue"),
        )

    @classmethod
    def config_schema_fields(cls) -> dict[str, Any]:
        return {"email": str, "password": str}
