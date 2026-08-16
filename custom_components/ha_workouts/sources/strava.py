"""Strava data source. OAuth2 via Home Assistant's Application Credentials system —
each user registers their own Strava API app (see README) rather than sharing one
baked into this integration.

Strava's list endpoint (GET /athlete/activities) does not include calories; only
the per-activity detail endpoint does. To stay well within Strava's rate limits
(100 reads/15min, 1000/day) during a potentially multi-year history backfill,
calories are only fetched for "today" (async_fetch) via the cheap single detail
call, never for bulk range fetches (async_fetch_activities_range) — backfilled
days show distance/duration but no calories.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import aiohttp
from homeassistant.helpers import config_entry_oauth2_flow

from ..models import Activity, ActivityType, WorkoutData
from .base import (
    WorkoutSource,
    WorkoutSourceAuthError,
    WorkoutSourceError,
    WorkoutSourceRateLimitedError,
)

_LOGGER = logging.getLogger(__name__)

_API_BASE = "https://www.strava.com/api/v3"

# Strava's own sport_type values, mapped to our normalized ActivityType.
_ACTIVITY_TYPE_MAP: dict[str, ActivityType] = {
    "run": ActivityType.RUNNING,
    "trailrun": ActivityType.RUNNING,
    "treadmill": ActivityType.RUNNING,
    "ride": ActivityType.CYCLING,
    "mountainbikeride": ActivityType.CYCLING,
    "gravelride": ActivityType.CYCLING,
    "virtualride": ActivityType.CYCLING,
    "swim": ActivityType.SWIMMING,
    "walk": ActivityType.WALKING,
    "hike": ActivityType.HIKING,
    "weighttraining": ActivityType.STRENGTH_TRAINING,
    "workout": ActivityType.STRENGTH_TRAINING,
    "yoga": ActivityType.YOGA,
}


def _map_activity_type(strava_type: str | None) -> ActivityType:
    if not strava_type:
        return ActivityType.OTHER
    return _ACTIVITY_TYPE_MAP.get(strava_type.lower(), ActivityType.OTHER)


class StravaSource(WorkoutSource):
    """Fetches activity data from Strava via its OAuth2 REST API."""

    key = "strava"
    # Strava's documented limit (100 reads/15min) is generous relative to a
    # 90-day-chunk backfill's request volume, so less pacing headroom is needed
    # than for Garmin's undocumented limit. OAuth2Session refreshes tokens
    # transparently without a separate login call, so no post-login pause needed.
    backfill_chunk_pause_seconds = 5.0

    def __init__(self, session: config_entry_oauth2_flow.OAuth2Session) -> None:
        self._session = session

    async def async_authenticate(self) -> None:
        # OAuth2Session refreshes tokens transparently; this just validates the
        # current token works by touching the cheapest authenticated endpoint.
        await self._request("GET", "/athlete")

    async def async_fetch(self, target_day: date) -> WorkoutData:
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc)
        summaries = await self._list_activities(start, end)

        activities = []
        for summary in summaries:
            detail = await self._fetch_detail(summary["id"])
            activities.append(self._parse_activity(summary, calories=detail.get("calories")))

        return WorkoutData(activities=activities, daily_summary=None)

    async def async_fetch_activities_range(
        self, start_day: date, end_day: date
    ) -> list[Activity]:
        """Fetch all activities in the range. No calorie detail calls (see module docstring)."""
        start = datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.combine(end_day, datetime.max.time(), tzinfo=timezone.utc)
        summaries = await self._list_activities(start, end)
        return [self._parse_activity(summary, calories=None) for summary in summaries]

    async def _list_activities(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = await self._request(
                "GET",
                "/athlete/activities",
                params={
                    "after": int(start.timestamp()),
                    "before": int(end.timestamp()),
                    "page": page,
                    "per_page": 200,
                },
            )
            if not batch:
                break
            activities.extend(batch)
            if len(batch) < 200:
                break
            page += 1
        return activities

    async def _fetch_detail(self, activity_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/activities/{activity_id}")

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        # OAuth2Session.async_request ensures the token is valid (refreshing via
        # HA's Application Credentials-backed implementation if needed) and reuses
        # HA's shared aiohttp session rather than opening a new one per call.
        try:
            resp = await self._session.async_request(
                method, f"{_API_BASE}{path}", params=params
            )
            async with resp:
                if resp.status == 401:
                    raise WorkoutSourceAuthError("Strava token rejected or expired")
                if resp.status == 429:
                    raise WorkoutSourceRateLimitedError("Strava API rate limited us")
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise WorkoutSourceError(f"Error communicating with Strava: {err}") from err

    def _parse_activity(self, item: dict[str, Any], calories: float | None) -> Activity:
        return Activity(
            source=self.key,
            source_id=str(item["id"]),
            activity_type=_map_activity_type(item.get("sport_type") or item.get("type")),
            start=datetime.fromisoformat(item["start_date_local"].replace("Z", "+00:00")),
            duration_seconds=float(item.get("moving_time") or 0),
            distance_meters=item.get("distance"),
            calories=calories,
            avg_heart_rate=(
                round(item["average_heartrate"]) if item.get("average_heartrate") else None
            ),
            max_heart_rate=(
                round(item["max_heartrate"]) if item.get("max_heartrate") else None
            ),
            elevation_gain_meters=item.get("total_elevation_gain"),
            name=item.get("name"),
        )

    @classmethod
    def config_schema_fields(cls) -> dict[str, Any]:
        # Strava auth is handled entirely via OAuth2 (config_flow's
        # AbstractOAuth2FlowHandler), not a plain form — no fields here.
        return {}
