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
JSON for every tool call, and its activity-listing tool has a documented
~40-50% spurious-404 failure rate on fresh sessions. Parsing prose reliably
and working around an undocumented flaky endpoint was judged more fragile
long-term than this REST API's plain, stable JSON shape for activities —
even accepting this route's own known tradeoff: logging in here
invalidates the user's own Coros app/web session, and vice versa (Coros's
Training Hub allows only one active session per account). Nothing to be
done about that from this side — it's how Coros's session model works.

A token from one region is REJECTED by the other regions' hosts even
though login itself succeeds against any of them — so the account's real
region has to be configured (see const.CONF_COROS_REGION), not
auto-detected.

Training Hub has no equivalent AT ALL for several real Coros features —
steps, sleep, HRV assessment, recovery status, fitness assessment (VO2max/
race predictions), training load — confirmed absent by direct probing.
Those come from the MCP program instead (see sources/coros_mcp.py), used
here ONLY for this handful of tools that don't touch the flaky/prose-heavy
parts avoided above. MCP is an entirely separate, OPTIONAL login on the
same Coros account — connecting it does not affect, and is not affected
by, the Training Hub session above.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..models import (
    Activity,
    ActivitySplit,
    ActivityType,
    DailySummary,
    FitnessAssessment,
    WorkoutData,
)
from . import coros_mcp_parsers
from .base import (
    WorkoutSource,
    WorkoutSourceAuthError,
    WorkoutSourceError,
    WorkoutSourceRateLimitedError,
)
from .coros_mcp import CorosMcpClient, McpTokenSet

_LOGGER = logging.getLogger(__name__)

#: How many recent MCP tool calls to keep raw text for (see
#: CorosSource.mcp_debug_log) — bounded so a long-running install doesn't
#: grow this without limit; only the most recent handful are ever useful for
#: troubleshooting a live wording/format change.
_MCP_DEBUG_LOG_MAXLEN = 20

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

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        region: str,
        *,
        mcp_tokens: McpTokenSet | None = None,
        mcp_token_update_callback: Callable[[McpTokenSet], None] | None = None,
    ) -> None:
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

        # MCP (see sources/coros_mcp.py) is an entirely separate, OPTIONAL
        # connection — mcp_tokens is None whenever the user hasn't connected
        # it, in which case _fetch_mcp_summary below is simply skipped. Uses
        # the same shared HA session as _client() (see that method's own
        # comment for why); CorosMcpClient itself owns no connection.
        self._mcp_client = CorosMcpClient(self._client())
        self._mcp_tokens = mcp_tokens
        self._mcp_token_update_callback = mcp_token_update_callback
        self._mcp_lock = asyncio.Lock()
        # Raw text of the most recent MCP tool calls (both successful and
        # failed), newest last — surfaced via the coros_mcp_debug_log
        # diagnostic sensor so a user with a real watch (which this
        # integration's own dev/test account doesn't have) can report back
        # exactly what Coros's MCP API returns for them, without needing
        # screen-share access to their account. See sensor.py's
        # CorosMcpDebugLogSensor.
        self.mcp_debug_log: deque[dict[str, Any]] = deque(maxlen=_MCP_DEBUG_LOG_MAXLEN)

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
        # Both summary sources below have no date parameter — they always
        # answer for "now", unlike the activity list above — so this whole
        # block only ever runs for today's live poll, never the historical
        # backfill range (see async_fetch_activities_range, which never
        # calls either).
        summary = await self._fetch_daily_summary(target_day)
        fitness_assessment = await self._fetch_fitness_assessment()
        return WorkoutData(
            activities=activities,
            daily_summary=summary,
            fitness_assessment=fitness_assessment,
        )

    async def _fetch_daily_summary(self, target_day: date) -> DailySummary | None:
        """Merge Training Hub's dashboard HRV with MCP's richer daily
        health/sleep/HRV/recovery/training-load data (see module docstring
        for why both exist) into a single DailySummary — WorkoutData only
        ever carries one.

        MCP's own official querySleepHrv, when connected and it has data,
        takes priority over Training Hub's rough dashboard-derived HRV
        (values below are overwritten, not merged field-by-field) — it's
        Coros's own assessed figure, not this integration's derived one.
        """
        body = await self._request("GET", "/dashboard/query")
        summary_info = (body.get("data") or {}).get("summaryInfo") or {}
        hrv_last_night_avg, hrv_weekly_avg, hrv_status = _parse_dashboard_hrv(summary_info)
        summary = None
        if hrv_last_night_avg is not None or hrv_weekly_avg is not None or hrv_status is not None:
            summary = DailySummary(
                source=self.key,
                day=target_day,
                hrv_last_night_avg=hrv_last_night_avg,
                hrv_weekly_avg=hrv_weekly_avg,
                hrv_status=hrv_status,
            )

        for mcp_summary in await self._fetch_mcp_daily_summaries(target_day):
            summary = _merge_daily_summaries(summary, mcp_summary, self.key, target_day)
        return summary

    async def _fetch_mcp_daily_summaries(self, target_day: date) -> list[DailySummary]:
        """Call every connected-MCP daily-summary tool and parse each
        response — empty list if MCP isn't connected (self._mcp_tokens is
        None) or every tool came back with nothing parseable, which is
        normal (e.g. no watch, nothing synced yet), not an error.
        """
        if self._mcp_tokens is None:
            return []
        summaries: list[DailySummary] = []
        for tool_name, args, parser in (
            ("queryDailyHealthData", {"days": 3}, coros_mcp_parsers.parse_daily_health_data),
            (
                "querySleepData",
                {
                    "startDate": target_day.strftime("%Y%m%d"),
                    "endDate": target_day.strftime("%Y%m%d"),
                    "days": 7,
                },
                coros_mcp_parsers.parse_sleep_data,
            ),
            (
                "querySleepHrv",
                {
                    "startDate": target_day.strftime("%Y%m%d"),
                    "endDate": target_day.strftime("%Y%m%d"),
                    "days": 7,
                },
                coros_mcp_parsers.parse_sleep_hrv,
            ),
            ("queryRecoveryStatus", {}, coros_mcp_parsers.parse_recovery_status),
            (
                "queryTrainingLoadAssessment",
                {"days": 3},
                coros_mcp_parsers.parse_training_load,
            ),
        ):
            text = await self._call_mcp_tool(tool_name, args)
            if text is None:
                continue
            parsed = parser(self.key, target_day, text)
            if parsed is not None:
                summaries.append(parsed)
        return summaries

    async def _fetch_fitness_assessment(self) -> FitnessAssessment | None:
        if self._mcp_tokens is None:
            return None
        text = await self._call_mcp_tool("queryFitnessAssessmentOverview", {})
        if text is None:
            return None
        return coros_mcp_parsers.parse_fitness_assessment(self.key, text)

    async def _call_mcp_tool(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        """Call one MCP tool, refreshing the token first if it's expired.

        Returns None (rather than raising) on any MCP-specific failure —
        MCP is optional supplementary data; a problem with it should never
        take down the whole Coros source's live poll, which still needs to
        report today's activities either way.
        """
        async with self._mcp_lock:
            if self._mcp_tokens is None:
                return None
            if self._mcp_tokens.is_expired():
                try:
                    self._mcp_tokens = await self._mcp_client.async_refresh(self._mcp_tokens)
                except WorkoutSourceError:
                    _LOGGER.warning(
                        "Coros MCP token refresh failed for %s; skipping MCP data this poll",
                        self.key,
                        exc_info=True,
                    )
                    return None
                if self._mcp_token_update_callback is not None:
                    self._mcp_token_update_callback(self._mcp_tokens)
            tokens = self._mcp_tokens

        try:
            text = await self._mcp_client.async_call_tool(tokens, tool_name, arguments)
        except WorkoutSourceError as err:
            _LOGGER.warning(
                "Coros MCP call to %s failed for %s; skipping this data this poll",
                tool_name,
                self.key,
                exc_info=True,
            )
            self.mcp_debug_log.append(
                {
                    "time": datetime.now(tz=timezone.utc).isoformat(),
                    "tool": tool_name,
                    "error": str(err),
                }
            )
            return None
        self.mcp_debug_log.append(
            {
                "time": datetime.now(tz=timezone.utc).isoformat(),
                "tool": tool_name,
                "text": text,
            }
        )
        return text

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


def _parse_dashboard_hrv(
    summary_info: dict[str, Any],
) -> tuple[float | None, float | None, str | None]:
    """Extract (last_night_avg, 7day_avg, status) from /dashboard/query's
    summaryInfo.sleepHrvData.

    Coros only records overnight HRV on some watch models — sleepHrvList
    comes back as an empty list (schema present, no readings) for an
    account whose watch doesn't capture it at all, which is the common case
    and not an error condition; this returns (None, None, None) then, same
    as Garmin does for a day with no reading yet.

    Unlike Garmin's response, Coros has no server-computed weekly average or
    qualitative status bucket (e.g. "BALANCED") — only a per-night value
    keyed by day. The 7-day average is computed here from whatever recent
    nights are present (fewer than 7 if that's all there is); status is left
    None rather than inventing a bucket Coros doesn't actually provide.
    """
    hrv_data = summary_info.get("sleepHrvData") or {}
    nights = hrv_data.get("sleepHrvList") or []
    readings = [n["avgSleepHrv"] for n in nights if n.get("avgSleepHrv") is not None]
    if not readings:
        return None, None, None
    last_night_avg = readings[-1]
    weekly_avg = sum(readings) / len(readings)
    return last_night_avg, weekly_avg, None


def _merge_daily_summaries(
    base: DailySummary | None, new: DailySummary, source: str, day: date
) -> DailySummary:
    """Fold new's non-None fields onto base (or create a fresh DailySummary
    if base is None) — used to combine Training Hub's dashboard HRV with
    each of MCP's several daily-summary tool calls into one DailySummary,
    since WorkoutData only ever carries a single daily_summary.

    new's hrv_* fields always win over base's when new has any (see
    _fetch_daily_summary's docstring: MCP's official querySleepHrv is
    intentionally treated as authoritative over Training Hub's rough
    dashboard-derived figure) — every other field is a plain "new wins if
    present" merge, since no other field is ever populated by more than one
    of the calls this is used to fold together.
    """
    if base is None:
        return new
    for field_name in (
        "steps",
        "resting_heart_rate",
        "sleep_seconds",
        "stress_avg",
        "body_battery_max",
        "body_battery_min",
        "active_calories",
        "floors_climbed",
        "vo2_max",
        "hrv_last_night_avg",
        "hrv_weekly_avg",
        "hrv_status",
        "training_readiness_score",
        "training_readiness_level",
        "training_readiness_feedback",
        "sleep_score",
        "recovery_level",
        "recovery_percent",
        "recovery_estimated_full_hours",
        "training_load_short_term",
        "training_load_long_term",
    ):
        new_value = getattr(new, field_name)
        if new_value is not None:
            setattr(base, field_name, new_value)
    return base


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
