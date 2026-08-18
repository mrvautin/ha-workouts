"""Apple Health data source.

Unlike Garmin/Strava, there is no cloud API for Apple Health/HealthKit — it's an
on-device-only datastore. Data instead arrives by the user's own iOS Shortcut (or
similar) POSTing one JSON object per workout to this integration's webhook
endpoint, whenever that automation happens to run.

This means AppleHealthSource is a push receiver, not a poller: async_fetch and
async_fetch_activities_range never make outbound requests. async_fetch returns
whatever's arrived in the in-memory buffer since it was last read; range-fetch
(used for history backfill) always returns empty, since Apple Health can only
ever tell us about activities from the moment someone wires up the Shortcut
onward — there is no historical archive to pull from.

Confirmed webhook payload shape (from real-device testing via a Toolbox Pro
"Get Workouts" Shortcut action, since stock Shortcuts cannot query HKWorkoutType
objects at all — only individual quantity samples like "Walking + Running
Distance", which conflate incidental walking with real workouts and carry no
reliable per-session date):

    {
      "distance": "11.4 km",
      "startDate": "16 Aug 2026 at 9:34 am",
      "endDate": "16 Aug 2026 at 10:33 am",
      "id": "283029CC-46AA-4890-A5ED-D61251D92218",
      "calories": "724 kcal",
      "duration": "58.2",
      "type": "Running"
    }

distance/calories carry a unit suffix (observed "km"/"kcal", but the user's
device unit settings could produce "mi" or others, so both number and unit are
parsed rather than assumed). duration is bare minutes, no suffix observed.
"""
from __future__ import annotations

import logging
import re
from collections import deque
from datetime import date, datetime
from typing import Any

from ..models import Activity, ActivityType, WorkoutData
from .base import WorkoutSource

_LOGGER = logging.getLogger(__name__)

# Apple/HealthKit workout type names (as returned by Toolbox Pro's Get Workouts),
# mapped to our normalized ActivityType.
_ACTIVITY_TYPE_MAP: dict[str, ActivityType] = {
    "running": ActivityType.RUNNING,
    "walking": ActivityType.WALKING,
    "cycling": ActivityType.CYCLING,
    "swimming": ActivityType.SWIMMING,
    "hiking": ActivityType.HIKING,
    "traditional strength training": ActivityType.STRENGTH_TRAINING,
    "functional strength training": ActivityType.STRENGTH_TRAINING,
    "yoga": ActivityType.YOGA,
}

# Recognized distance units and their conversion factor to meters.
_DISTANCE_UNIT_TO_METERS = {
    "km": 1000.0,
    "kilometers": 1000.0,
    "m": 1.0,
    "meters": 1.0,
    "mi": 1609.344,
    "mile": 1609.344,
    "miles": 1609.344,
    "yd": 0.9144,
    "yards": 0.9144,
}

# Recognized calorie/energy units and their conversion factor to kcal.
_ENERGY_UNIT_TO_KCAL = {
    "kcal": 1.0,
    "cal": 0.001,
    "calories": 0.001,
    "kj": 1 / 4.184,
    "j": 1 / 4184.0,
}

_NUMBER_AND_UNIT_RE = re.compile(r"([\d.,]+)\s*([a-zA-Z]+)?")

#: How many recently-seen webhook payload IDs to remember for deduplication.
#: Bounded so a very long-running HA instance doesn't grow this unboundedly;
#: far larger than any plausible burst of re-delivered/replayed workouts.
_DEDUP_HISTORY_SIZE = 500


def _map_activity_type(apple_type: str | None) -> ActivityType:
    if not apple_type:
        return ActivityType.OTHER
    return _ACTIVITY_TYPE_MAP.get(apple_type.strip().lower(), ActivityType.OTHER)


def _parse_number_and_unit(value: str | None) -> tuple[float, str] | None:
    """Parse a string like '11.4 km' or '724 kcal' into (11.4, 'km')."""
    if not value:
        return None
    match = _NUMBER_AND_UNIT_RE.match(value.strip())
    if not match:
        return None
    number_str, unit = match.groups()
    try:
        number = float(number_str.replace(",", ""))
    except ValueError:
        return None
    return number, (unit or "").strip().lower()


def _parse_distance_meters(value: str | None) -> float | None:
    parsed = _parse_number_and_unit(value)
    if parsed is None:
        return None
    number, unit = parsed
    factor = _DISTANCE_UNIT_TO_METERS.get(unit)
    if factor is None:
        _LOGGER.warning("Unrecognized distance unit %r in Apple Health payload", unit)
        return None
    return number * factor


def _parse_calories_kcal(value: str | None) -> float | None:
    parsed = _parse_number_and_unit(value)
    if parsed is None:
        return None
    number, unit = parsed
    factor = _ENERGY_UNIT_TO_KCAL.get(unit)
    if factor is None:
        _LOGGER.warning("Unrecognized energy unit %r in Apple Health payload", unit)
        return None
    return number * factor


def _parse_apple_datetime(value: str | None) -> datetime | None:
    """Parse Shortcuts' 'DD Mon YYYY at H:MM am/pm' date format.

    Shortcuts renders a narrow no-break space before am/pm (U+202F) depending on
    locale; normalized to a regular space before parsing.
    """
    if not value:
        return None
    normalized = value.replace(" ", " ").strip()
    for fmt in ("%d %b %Y at %I:%M %p", "%d %B %Y at %I:%M %p"):
        try:
            # Deliberately naive: Shortcuts sends the phone's local wall-clock
            # time with no offset, and we have no way to know what timezone
            # that is from the string alone (guessing UTC would be actively
            # wrong, not just imprecise). Only .date() is ever read from this
            # downstream (see sensor.py/statistics_import.py), so the missing
            # tzinfo doesn't affect correctness there.
            return datetime.strptime(normalized, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    _LOGGER.warning("Could not parse Apple Health date %r", value)
    return None


class AppleHealthSource(WorkoutSource):
    """Receives workouts pushed from an iOS Shortcut via webhook; never polls."""

    key = "apple_health"

    def __init__(self) -> None:
        self._pending: list[Activity] = []
        self._seen_ids: deque[str] = deque(maxlen=_DEDUP_HISTORY_SIZE)

    def ingest_webhook_payload(self, payload: dict[str, Any]) -> bool:
        """Parse one workout payload and queue it for the next coordinator poll.

        Returns True if the workout was accepted, False if it was a duplicate
        (already-seen id) or failed to parse (missing/unparseable required fields).
        """
        source_id = str(payload.get("id") or "")
        if not source_id:
            _LOGGER.warning("Apple Health webhook payload missing 'id'; ignoring")
            return False
        if source_id in self._seen_ids:
            _LOGGER.debug("Ignoring duplicate Apple Health workout %s", source_id)
            return False

        start = _parse_apple_datetime(payload.get("startDate"))
        end = _parse_apple_datetime(payload.get("endDate"))
        if start is None:
            _LOGGER.warning(
                "Apple Health webhook payload %s missing/unparseable startDate; ignoring",
                source_id,
            )
            return False

        duration_seconds = 0.0
        duration_raw = payload.get("duration")
        if duration_raw is not None:
            try:
                duration_seconds = float(str(duration_raw).replace(",", "")) * 60
            except ValueError:
                _LOGGER.warning(
                    "Apple Health workout %s has unparseable duration %r",
                    source_id,
                    duration_raw,
                )
        elif end is not None:
            duration_seconds = (end - start).total_seconds()

        activity = Activity(
            source=self.key,
            source_id=source_id,
            activity_type=_map_activity_type(payload.get("type")),
            start=start,
            duration_seconds=duration_seconds,
            distance_meters=_parse_distance_meters(payload.get("distance")),
            calories=_parse_calories_kcal(payload.get("calories")),
            name=payload.get("type"),
        )
        self._pending.append(activity)
        self._seen_ids.append(source_id)
        _LOGGER.debug("Queued Apple Health workout %s (%s)", source_id, activity.activity_type)
        return True

    async def async_authenticate(self) -> None:
        # No auth needed — data arrives via webhook, not an outbound API call.
        return

    async def async_fetch(self, target_day: date) -> WorkoutData:
        """Return and clear all activities received via webhook since the last call.

        target_day is ignored (unlike Garmin/Strava, which fetch one specific
        day): a workout can arrive here hours after it actually happened — e.g.
        the phone was locked overnight and the Shortcut only ran the next
        morning — so there's no reliable sense in which "today's" webhook
        deliveries and "today's" workouts are the same set. The per-activity-type
        sensors (sensor.py's CumulativeActivitySensor) attribute each activity by
        its own source_id rather than assuming everything returned here happened
        today, so draining the whole buffer regardless of date is safe and
        correct — nothing needs to be held back for a later, date-matched poll.
        """
        pending, self._pending = self._pending, []
        return WorkoutData(activities=pending, daily_summary=None)

    async def async_fetch_activities_range(
        self, start_day: date, end_day: date
    ) -> list[Activity]:
        """No historical data is available for Apple Health — see module docstring."""
        return []

    @classmethod
    def config_schema_fields(cls) -> dict[str, Any]:
        # Config flow generates a webhook URL rather than collecting fields.
        return {}
