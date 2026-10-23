"""Garmin Connect data source, backed by Garmin's official "Personal API"
program (https://developer.garmin.com/gc-developer-program/personal/), NOT
the unofficial garminconnect library this integration used previously.

The Personal API is a genuinely self-serve, individual-use program: a user
generates their own long-lived API key at
connect.garmin.com/app/settings/personalApiToken (expiry of their choosing,
up to 1 year) and that key alone authenticates every request — no app
registration, no OAuth client_id/secret, no approval queue (unlike Garmin's
separate, paused, multi-user "Connect Developer Program"). This sidesteps
the previous approach's real downsides: storing the user's actual Garmin
account password, and depending on a reverse-engineered, unsupported login
flow that could break at any time.

Auth is a simple, non-OAuth bearer-token exchange (confirmed against
Garmin's own official sample client, github.com/garmin/connect-personal-api-python):
POST the API key (as `Authorization: ApiKey <key>`) to a token endpoint,
get back a plain-text (NOT JSON) bearer token good for 24h or until the
underlying key expires, whichever is sooner. There is no refresh token —
"refreshing" just means repeating the same exchange with the same API key.
When the key itself expires (up to 1 year after creation), every exchange
starts failing and the user must generate a new key in Garmin Connect and
re-enter it via this integration's Reconfigure flow; there is no way to
renew a key in place.

Confirmed via Garmin's own published OpenAPI spec (extracted from
developer.garmin.com/gc-developer-program/personal/swagger-docs/) plus live
calls against a real account:
- Daily summary, HRV (with status classification), sleep (with stage
  breakdown), body battery, fitness age, heart rate zones: all present,
  often richer than the old garminconnect library exposed.
- Per-km/mile splits: present, but on GET /activity-service/activity/{id}
  (ActivitySummaryDTO.splitSummaries), NOT the .../workouts endpoint (which
  only returns a structured *planned* workout's steps/targets, never actual
  recorded performance).
- VO2max and Training Readiness (score/level/feedback): BOTH genuinely
  absent, despite Garmin Connect's own app clearly showing current values
  for both via its "Training Status" widget. The spec's one VO2max-shaped
  field (SummarizedActivityDTO.vO2MaxValue, on the activities list) was
  confirmed, by direct testing against a real account, to never actually
  be populated — checked across 50 real activities and a full recursive
  scan of the single-activity detail response, zero non-null values found
  anywhere. Both metrics live in Garmin's internal metrics/training-status
  service, which only the OLD unofficial garminconnect library's
  session-cookie login could reach (get_max_metrics/get_training_readiness)
  — confirmed the new Personal API's bearer token is flatly rejected by
  those old endpoints (a plain HTML sign-in page comes back, not even a
  401). These are the two real feature losses versus the old unofficial
  library; DailySummary has no vo2_max or training_readiness_* fields at
  all for this source.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..models import Activity, ActivitySplit, ActivityType, DailySummary, WorkoutData
from .base import (
    WorkoutSource,
    WorkoutSourceAuthError,
    WorkoutSourceError,
    WorkoutSourceRateLimitedError,
)

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://personal.connectapi.garmin.com"
_TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

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


def _parse_hrv(raw: dict[str, Any] | None) -> tuple[float | None, float | None, str | None]:
    """Extract (last_night_avg, weekly_avg, status) from GET /hrv-service/hrv/{date}.

    Returns all-None for a day with no overnight HRV reading yet (e.g. the
    watch wasn't worn overnight, or hasn't synced) — the endpoint can return
    a 200 with no "hrvSummary" block in that case, which is normal, not an
    error.
    """
    summary = (raw or {}).get("hrvSummary") or {}
    return (
        summary.get("lastNightAvg"),
        summary.get("weeklyAvg"),
        summary.get("status"),
    )


def _parse_splits(raw: dict[str, Any] | None) -> list[ActivitySplit]:
    """Parse GET /activity-service/activity/{id}'s splitSummaries into
    per-km/mile ActivitySplit records.

    Only "real" auto-laps are kept — Garmin's splitType enum covers many
    non-distance split kinds (e.g. RWD_STAND/RWD_WALK rest-wristband
    detection, MARKER_KILOMETER/MARKER_MILE distance markers, interval
    phases); INTERVAL_ACTIVE is the plain auto-lap type matching what the
    old garminconnect-based parser treated as "real" laps (intensityType ==
    "INTERVAL" there).
    """
    if not raw:
        return []
    split_dtos = [
        split
        for split in (raw.get("splitSummaries") or [])
        if split.get("splitType") == "INTERVAL_ACTIVE"
    ]

    splits: list[ActivitySplit] = []
    cumulative_distance = 0.0
    cumulative_elapsed = 0.0
    for index, split in enumerate(split_dtos, start=1):
        distance = split.get("distance") or 0.0
        elapsed = split.get("elapsedDuration") or split.get("duration") or 0.0
        cumulative_distance += distance
        cumulative_elapsed += elapsed

        avg_speed = split.get("averageSpeed")  # metres/second
        pace_seconds_per_km = (1000 / avg_speed) if avg_speed else None

        splits.append(
            ActivitySplit(
                index=index,
                distance_meters=distance,
                duration_seconds=split.get("duration") or 0.0,
                elapsed_seconds=elapsed,
                cumulative_distance_meters=cumulative_distance,
                cumulative_elapsed_seconds=cumulative_elapsed,
                avg_pace_seconds_per_km=pace_seconds_per_km,
                avg_heart_rate=split.get("averageHR"),
                max_heart_rate=split.get("maxHR"),
                elevation_gain_meters=split.get("elevationGain"),
            )
        )
    return splits


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
    """Fetches activity and daily summary data from Garmin's official Personal API."""

    key = "garmin"
    # Officially documented program, but with no published rate limit — pace
    # conservatively, matching this integration's existing convention for
    # every other unofficial/undocumented-limit source.
    backfill_chunk_pause_seconds = 15.0

    def __init__(self, hass: HomeAssistant, api_key: str) -> None:
        self._hass = hass
        self._api_key = api_key
        self._access_token: str | None = None
        # Serializes concurrent token exchanges the same way CorosSource's
        # _mcp_lock/_auth_lock do — the coordinator's periodic poll and the
        # background history backfill share one GarminSource instance, so an
        # expired-token moment during a long backfill could otherwise trigger
        # two concurrent exchanges for no reason.
        self._auth_lock = asyncio.Lock()

    def _client(self) -> aiohttp.ClientSession:
        return async_get_clientsession(self._hass)

    async def async_authenticate(self) -> None:
        async with self._auth_lock:
            if self._access_token is not None:
                # Another caller already re-authenticated while we were waiting.
                return
            await self._async_exchange_token()

    async def _async_exchange_token(self) -> None:
        """Exchange the long-lived Personal API key for a bearer access
        token. Not a "login" in the traditional sense — there's no session,
        just this bearer token, valid up to 24h or until the underlying key
        itself expires (see module docstring)."""
        try:
            async with self._client().post(
                f"{_BASE_URL}{_TOKEN_PATH}",
                headers={"Accept": "*/*", "Authorization": f"ApiKey {self._api_key}"},
            ) as resp:
                if resp.status == 401:
                    raise WorkoutSourceAuthError(
                        "Garmin Personal API key was rejected — it may have expired or been "
                        "revoked. Generate a new key at Garmin Connect's Personal API Key "
                        "settings page and reconnect this integration."
                    )
                if resp.status == 429:
                    raise WorkoutSourceRateLimitedError("Garmin Personal API rate limited us")
                resp.raise_for_status()
                # Response is the raw bearer token as plain text, NOT JSON —
                # confirmed against Garmin's own official sample client.
                token = (await resp.text()).strip()
        except aiohttp.ClientError as err:
            raise WorkoutSourceError(f"Could not connect to Garmin Personal API: {err}") from err

        if not token:
            raise WorkoutSourceAuthError("Garmin Personal API token exchange returned no token")
        self._access_token = token

    async def _async_ensure_authenticated(self) -> None:
        if self._access_token is None:
            await self.async_authenticate()

    async def _request(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        """GET one endpoint, retrying once with a fresh token on a 401.

        A 401 here means the bearer token itself expired mid-session (normal
        — it's only valid up to 24h) rather than the underlying API key
        being bad; _async_exchange_token distinguishes the "key rejected"
        case at exchange time, above.
        """
        await self._async_ensure_authenticated()
        assert self._access_token is not None

        for attempt in range(2):
            try:
                async with self._client().get(
                    f"{_BASE_URL}{path}",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._access_token}",
                    },
                    params=params,
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        self._access_token = None
                        await self._async_exchange_token()
                        continue
                    if resp.status == 401:
                        raise WorkoutSourceAuthError(
                            "Garmin Personal API rejected the request even after refreshing "
                            "the token — the underlying API key has likely expired."
                        )
                    if resp.status == 404:
                        return {}
                    if resp.status == 429:
                        raise WorkoutSourceRateLimitedError("Garmin Personal API rate limited us")
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientError as err:
                raise WorkoutSourceError(
                    f"Error communicating with Garmin Personal API: {err}"
                ) from err
        raise WorkoutSourceError("Garmin Personal API request failed after token refresh")

    async def async_fetch(self, target_day: date) -> WorkoutData:
        day_str = target_day.isoformat()

        raw_activities = await self._request(
            "/activitylist-service/activities/search/activities",
            {"start": 0, "limit": 20, "startDate": day_str, "endDate": day_str},
        )
        raw_summary = await self._request(
            "/usersummary-service/v2/dailysummaries",
            {"startDate": day_str, "endDate": day_str},
        )
        raw_hrv = await self._request(f"/hrv-service/hrv/{day_str}")

        activities = [self._parse_activity(item) for item in raw_activities or []]
        summary = self._parse_daily_summary(raw_summary, target_day, raw_hrv)

        # Splits are one extra API call per activity — cheap here since a
        # single day's poll only ever returns a handful of new activities at
        # most (unlike async_fetch_activities_range's backfill, which can
        # return hundreds; see sources/base.py's WorkoutSource docstring and
        # statistics_import.py's splits backfill job for why that path
        # deliberately does NOT fetch splits inline).
        for activity in activities:
            activity.splits = await self.async_fetch_splits(activity.source_id)

        return WorkoutData(activities=activities, daily_summary=summary)

    async def async_fetch_splits(self, source_id: str) -> list[ActivitySplit]:
        """Fetch per-km/mile auto-lap splits for one activity.

        Used both for today's newly-seen activities (see async_fetch above)
        and by the opt-in historical splits backfill job (see
        statistics_import.async_backfill_activity_splits). Splits live on
        the single-activity detail endpoint's splitSummaries field, NOT the
        .../workouts endpoint (confirmed via Garmin's own OpenAPI spec — see
        module docstring).
        """
        raw_activity = await self._request(f"/activity-service/activity/{source_id}")
        return _parse_splits(raw_activity if isinstance(raw_activity, dict) else None)

    async def async_fetch_activities_range(
        self, start_day: date, end_day: date
    ) -> list[Activity]:
        """Fetch all activities between start_day and end_day (inclusive). Used for backfill."""
        activities: list[Activity] = []
        start = 0
        page_size = 100
        while True:
            raw_activities = await self._request(
                "/activitylist-service/activities/search/activities",
                {
                    "start": start,
                    "limit": page_size,
                    "startDate": start_day.isoformat(),
                    "endDate": end_day.isoformat(),
                },
            )
            items = raw_activities if isinstance(raw_activities, list) else []
            activities.extend(self._parse_activity(item) for item in items)
            if len(items) < page_size:
                break
            start += page_size
        return activities

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

    def _parse_daily_summary(
        self,
        raw_summary: dict[str, Any] | list[Any],
        target_day: date,
        raw_hrv: dict[str, Any] | list[Any],
    ) -> DailySummary:
        summary_days = raw_summary.get("data") if isinstance(raw_summary, dict) else None
        day_summary = (summary_days or [{}])[-1] if summary_days else {}
        movement = day_summary.get("movement") or {}
        calories = day_summary.get("calories") or {}
        heart_rate = day_summary.get("heartRate") or {}
        stress = day_summary.get("stress") or {}
        body_battery = day_summary.get("bodyBattery") or {}

        hrv_last_night_avg, hrv_weekly_avg, hrv_status = _parse_hrv(
            raw_hrv if isinstance(raw_hrv, dict) else None
        )

        return DailySummary(
            source=self.key,
            day=target_day,
            steps=(movement.get("steps") or {}).get("value"),
            resting_heart_rate=heart_rate.get("restingValue"),
            active_calories=calories.get("burnedActive"),
            floors_climbed=(movement.get("floorsAscended") or {}).get("value"),
            stress_avg=stress.get("avgLevel"),
            body_battery_max=body_battery.get("maxValue"),
            body_battery_min=body_battery.get("minValue"),
            hrv_last_night_avg=hrv_last_night_avg,
            hrv_weekly_avg=hrv_weekly_avg,
            hrv_status=hrv_status,
            # VO2max and Training Readiness have no equivalent in Garmin's
            # official Personal API — confirmed absent (see module
            # docstring). Left unset (None) rather than guessed at.
        )

    @classmethod
    def config_schema_fields(cls) -> dict[str, Any]:
        return {"api_key": str}
