"""Coros Training Hub data source.

There is no maintained Python library for Coros (unlike garminconnect for
Garmin) — the closest thing, PyPI's "corosexport", is a 4-star unlicensed
alpha package with an auth scheme superseded by more recent reverse
engineering. This hand-rolls a small client against the same
unofficial Training Hub web API (teamapi.coros.com and friends) that
Coros's own training.coros.com dashboard uses, following the same shape
as GarminSource.

Login: POST /account/login with {"account": email, "accountType": 2,
"pwd": md5(password)}. Coros also runs an official OAuth2 + MCP program
("COROS MCP", support.coros.com) requiring no app registration on our part
— but its MCP server returns free-form prose text instead of structured
JSON for every tool call, and its main activity-listing tool has a
documented ~40-50% spurious-404 failure rate on fresh sessions. Parsing
prose reliably and working around an undocumented flaky endpoint was
judged more fragile long-term than this REST API's plain, stable JSON
shape, even accepting this route's own known tradeoff: logging in here
invalidates the user's own Coros app/web session, and vice versa (Coros's
Training Hub allows only one active session per account). Nothing to be
done about that from this side — it's how Coros's session model works.

A token from one region is REJECTED by the other regions' hosts even
though login itself succeeds against any of them — so the account's real
region has to be configured (see const.CONF_COROS_REGION), not
auto-detected.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..models import Activity, ActivitySplit, ActivityType, WorkoutData
from .base import (
    WorkoutSource,
    WorkoutSourceAuthError,
    WorkoutSourceError,
    WorkoutSourceRateLimitedError,
)

_LOGGER = logging.getLogger(__name__)

#: Login succeeds against any region's host, but the returned token is only
#: accepted by that SAME region's host for every later call — see module
#: docstring.
_REGION_BASE_URLS: dict[str, str] = {
    "eu": "https://teameuapi.coros.com",
    "us": "https://teamapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
}

#: Coros's servers reject requests with no (or a non-browser) User-Agent —
#: observed returning the same generic "1019 Access token is invalid" error
#: as an actually-expired token, rather than a distinguishable 403, which
#: made this a confusing failure to track down. Sent on every request,
#: including login.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Coros's own sport type codes, mapped to our normalized ActivityType. Not
# exhaustive — anything unmapped falls back to ActivityType.OTHER.
_ACTIVITY_TYPE_MAP: dict[int, ActivityType] = {
    100: ActivityType.RUNNING,
    102: ActivityType.RUNNING,  # Trail running
    103: ActivityType.RUNNING,  # Track running
    104: ActivityType.HIKING,
    200: ActivityType.CYCLING,  # Road bike
    201: ActivityType.CYCLING,  # Indoor cycling
    203: ActivityType.CYCLING,  # Gravel bike
    204: ActivityType.CYCLING,  # MTB
    9807: ActivityType.CYCLING,  # Bike commute
    300: ActivityType.SWIMMING,  # Pool swim
    301: ActivityType.SWIMMING,  # Open water swim
    402: ActivityType.STRENGTH_TRAINING,
    403: ActivityType.YOGA,
    900: ActivityType.WALKING,
}


def _map_activity_type(sport_type: int | None) -> ActivityType:
    if sport_type is None:
        return ActivityType.OTHER
    return _ACTIVITY_TYPE_MAP.get(sport_type, ActivityType.OTHER)


def _md5(value: str) -> str:
    # Coros's own login scheme (matches its web/mobile app) — not used for
    # any cryptographic purpose of ours.
    return hashlib.md5(value.encode()).hexdigest()


class CorosAPIError(Exception):
    """Coros API responded with a non-success result code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CorosSource(WorkoutSource):
    """Fetches activity data from Coros's unofficial Training Hub web API."""

    key = "coros"
    # Unofficial and undocumented, like Garmin — no published rate limit to
    # target, so pace conservatively.
    backfill_chunk_pause_seconds = 20.0

    def __init__(self, hass: HomeAssistant, email: str, password: str, region: str) -> None:
        self._hass = hass
        self._email = email
        self._password = password
        self._region = region
        self._base_url = _REGION_BASE_URLS.get(region, _REGION_BASE_URLS["eu"])
        self._access_token: str | None = None
        self._user_id: str | None = None
        # The coordinator's periodic poll and the background history backfill
        # both call async_fetch*/async_authenticate on this same shared
        # instance — see GarminSource's identical lock for why concurrent
        # logins must be serialized rather than each independently racing
        # Coros's login endpoint.
        self._auth_lock = asyncio.Lock()

    def _client(self) -> aiohttp.ClientSession:
        # HA's shared session (one per hass instance, closed by HA itself on
        # shutdown) — not a session this class owns, so there's nothing for
        # it to leak or need to close.
        return async_get_clientsession(self._hass)

    async def async_authenticate(self) -> None:
        async with self._auth_lock:
            if self._access_token is not None:
                # Another caller already re-authenticated while we were waiting.
                return
            payload = {
                "account": self._email,
                "accountType": 2,
                "pwd": _md5(self._password),
            }
            try:
                async with self._client().post(
                    f"{self._base_url}/account/login",
                    json=payload,
                    headers={"User-Agent": _USER_AGENT},
                ) as resp:
                    if resp.status == 429:
                        raise WorkoutSourceRateLimitedError("Coros rate limited the login")
                    resp.raise_for_status()
                    body = await resp.json()
            except aiohttp.ClientError as err:
                raise WorkoutSourceError(f"Could not connect to Coros: {err}") from err

            if body.get("result") != "0000":
                raise WorkoutSourceAuthError(
                    f"Invalid Coros email or password (result={body.get('result')}, "
                    f"message={body.get('message')})"
                )

            data = body.get("data") or {}
            access_token = data.get("accessToken")
            user_id = data.get("userId")
            if not access_token or not user_id:
                raise WorkoutSourceAuthError("Coros login response missing accessToken/userId")
            self._access_token = access_token
            self._user_id = user_id

    async def _async_ensure_authenticated(self) -> None:
        if self._access_token is None:
            await self.async_authenticate()

    def _auth_headers(self) -> dict[str, str]:
        assert self._access_token is not None
        assert self._user_id is not None
        return {
            "User-Agent": _USER_AGENT,
            "accessToken": self._access_token,
            # Coros's own web app sends the user id as a JSON blob in this
            # header, not as a query param or bearer claim — matches what
            # training.coros.com itself sends.
            "yfheader": f'{{"userId":"{self._user_id}"}}',
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self._async_ensure_authenticated()

        try:
            async with self._client().request(
                method,
                f"{self._base_url}{path}",
                params=params,
                data=data,
                headers=self._auth_headers(),
            ) as resp:
                if resp.status == 429:
                    raise WorkoutSourceRateLimitedError("Coros rate limited us")
                resp.raise_for_status()
                body = await resp.json()
        except aiohttp.ClientError as err:
            raise WorkoutSourceError(f"Error communicating with Coros: {err}") from err

        # "1019" (and other non-"0000" auth-shaped codes) means the token has
        # expired or been invalidated — most commonly because the user (or
        # this integration, on a prior run) logged into the Coros app/web
        # dashboard, which silently kicks out any other active session (see
        # module docstring). Force a fresh login on the next call rather than
        # surfacing a raw API error.
        if body.get("result") != "0000":
            self._access_token = None
            self._user_id = None
            raise WorkoutSourceAuthError(
                f"Coros session expired or was invalidated (result={body.get('result')}, "
                f"message={body.get('message')})"
            )
        return body

    async def async_fetch(self, target_day: date) -> WorkoutData:
        activities = await self.async_fetch_activities_range(target_day, target_day)
        for activity in activities:
            activity.splits = await self.async_fetch_splits(
                activity.source_id, _activity_sport_type(activity)
            )
        # Coros's Training Hub API has no equivalent of Garmin's daily
        # steps/resting-HR/body-battery summary endpoint — only activities.
        return WorkoutData(activities=activities, daily_summary=None)

    async def async_fetch_activities_range(
        self, start_day: date, end_day: date
    ) -> list[Activity]:
        """Fetch all activities between start_day and end_day (inclusive). Used for backfill."""
        activities: list[Activity] = []
        page = 1
        page_size = 100
        while True:
            body = await self._request(
                "GET",
                "/activity/query",
                params={
                    "startDay": start_day.strftime("%Y%m%d"),
                    "endDay": end_day.strftime("%Y%m%d"),
                    "pageNumber": page,
                    "size": page_size,
                },
            )
            page_data = body.get("data") or {}
            items = page_data.get("dataList") or []
            activities.extend(self._parse_activity(item) for item in items)
            if len(items) < page_size:
                break
            page += 1
        return activities

    async def async_fetch_splits(self, source_id: str, sport_type: int) -> list[ActivitySplit]:
        """Fetch per-km/mile auto-lap splits for one activity.

        Coros's activity-detail endpoint requires sportType alongside the
        activity id (unlike Garmin, where the activity id alone is enough) —
        see _activity_sport_type for why that's threaded through from the
        already-parsed Activity rather than looked up again here.
        """
        body = await self._request(
            "POST",
            "/activity/detail/query",
            data={
                "labelId": source_id,
                "userId": self._user_id,
                "sportType": str(sport_type),
            },
        )
        return _parse_splits((body.get("data") or {}).get("lapDTOs") or [])

    def _parse_activity(self, item: dict[str, Any]) -> Activity:
        sport_type = item.get("sportType")
        # Coros's "calorie" field is physical calories, not kilocalories — a
        # typical 60-minute run reports ~600,000, i.e. 600 kcal. Divide by
        # 1000 to match every other source's kcal convention.
        calories_raw = item.get("calorie")
        return Activity(
            source=self.key,
            source_id=str(item.get("labelId", "")),
            activity_type=_map_activity_type(sport_type),
            start=_parse_coros_timestamp(item.get("startTime")),
            duration_seconds=float(item.get("totalTime") or item.get("workoutTime") or 0),
            distance_meters=item.get("distance"),
            calories=(calories_raw / 1000) if calories_raw is not None else None,
            avg_heart_rate=item.get("avgHr"),
            max_heart_rate=item.get("maxHr"),
            elevation_gain_meters=item.get("ascent"),
            name=item.get("name"),
        )

    @classmethod
    def config_schema_fields(cls) -> dict[str, Any]:
        return {"email": str, "password": str}


def _activity_sport_type(activity: Activity) -> int:
    # source_id alone doesn't carry the raw sportType through Activity's
    # normalized fields, but splits fetches need it (see
    # CorosSource.async_fetch_splits) — reverse the normalized ActivityType
    # back to a representative Coros sport code good enough to satisfy the
    # detail endpoint's required parameter. This is deliberately approximate
    # (e.g. any CYCLING activity uses the road-bike code, 200): Coros's detail
    # endpoint has been observed to accept any sportType code within the
    # correct broad category for a given labelId, since the id itself, not
    # sportType, is what actually selects the activity server-side.
    reverse_map = {
        ActivityType.RUNNING: 100,
        ActivityType.CYCLING: 200,
        ActivityType.SWIMMING: 300,
        ActivityType.WALKING: 900,
        ActivityType.HIKING: 104,
        ActivityType.STRENGTH_TRAINING: 402,
        ActivityType.YOGA: 403,
    }
    return reverse_map.get(activity.activity_type, 100)


def _parse_coros_timestamp(value: int | None) -> datetime:
    """Coros's startTime is Unix seconds, UTC."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _parse_splits(lap_dtos: list[dict[str, Any]]) -> list[ActivitySplit]:
    """Parse /activity/detail/query's lapDTOs into per-km/mile ActivitySplit records.

    Same auto-lap-only filtering and cumulative-total approach as Garmin's
    _parse_splits — see that function's docstring for why REST-time (not
    moving time) is what the cumulative elapsed total is built from.
    """
    real_laps = [lap for lap in lap_dtos if lap.get("intensityType") in (None, "INTERVAL")]
    real_laps.sort(key=lambda lap: lap.get("lapIndex", 0))

    splits: list[ActivitySplit] = []
    cumulative_distance = 0.0
    cumulative_elapsed = 0.0
    for lap in real_laps:
        distance = lap.get("distance") or 0.0
        elapsed = lap.get("elapsedDuration") or lap.get("duration") or 0.0
        cumulative_distance += distance
        cumulative_elapsed += elapsed

        avg_speed = lap.get("averageSpeed")  # metres/second
        pace_seconds_per_km = (1000 / avg_speed) if avg_speed else None

        splits.append(
            ActivitySplit(
                index=lap.get("lapIndex", len(splits) + 1),
                distance_meters=distance,
                duration_seconds=lap.get("duration") or 0.0,
                elapsed_seconds=elapsed,
                cumulative_distance_meters=cumulative_distance,
                cumulative_elapsed_seconds=cumulative_elapsed,
                avg_pace_seconds_per_km=pace_seconds_per_km,
                avg_heart_rate=lap.get("averageHR") or lap.get("avgHr"),
                max_heart_rate=lap.get("maxHR") or lap.get("maxHr"),
                elevation_gain_meters=lap.get("elevationGain") or lap.get("ascent"),
            )
        )
    return splits
