"""Calendar entities: one per activity type (running, cycling, ...), each
showing that type's logged activities with pace/split detail.

One calendar per type, rather than one calendar for everything, so a
dashboard can show just the type(s) someone actually cares about — e.g. only
running — instead of every activity type mixed into a single calendar with no
way to filter it down.

Backed by activity_log.py's persisted, per-entry activity log rather than the
coordinator's live data (which only ever holds the most recent poll) — a
calendar has to answer "what happened between date A and date B", which needs
real queryable history.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .activity_log import async_get_activities_in_range
from .const import DOMAIN
from .coordinator import WorkoutDataUpdateCoordinator
from .models import Activity, ActivitySplit, ActivityType


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: WorkoutDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ActivityTypeCalendar(coordinator, entry, activity_type)
            for activity_type in ActivityType
        ]
    )


def _format_pace(seconds_per_km: float | None) -> str | None:
    if not seconds_per_km:
        return None
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}:{seconds:02d}/km"


def _format_duration(total_seconds: float) -> str:
    total_seconds = round(total_seconds)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _build_description(activity: Activity) -> str:
    lines: list[str] = []
    if activity.distance_meters:
        lines.append(f"Distance: {activity.distance_meters / 1000:.2f} km")
    lines.append(f"Duration: {_format_duration(activity.duration_seconds)}")
    if activity.distance_meters and activity.duration_seconds:
        avg_pace = _format_pace(activity.duration_seconds / (activity.distance_meters / 1000))
        if avg_pace:
            lines.append(f"Average pace: {avg_pace}")
    if activity.calories:
        lines.append(f"Calories: {activity.calories:.0f}")
    if activity.avg_heart_rate:
        lines.append(f"Average heart rate: {activity.avg_heart_rate:.0f} bpm")
    if activity.elevation_gain_meters:
        lines.append(f"Elevation gain: {activity.elevation_gain_meters:.0f} m")

    if activity.splits:
        lines.append("")
        lines.append("Splits:")
        for split in activity.splits:
            lines.append(_format_split_line(split))
    elif activity.splits is None:
        lines.append("")
        lines.append(
            "(Pace/split detail not fetched for this activity yet — see the "
            "\"Pace/splits import status\" sensor, or Configure to backfill it.)"
        )

    return "\n".join(lines)


def _format_split_line(split: ActivitySplit) -> str:
    pace = _format_pace(split.avg_pace_seconds_per_km)
    pace_part = f", {pace}" if pace else ""
    return (
        f"  km {split.index}: {_format_duration(split.elapsed_seconds)}{pace_part} "
        f"(at {_format_duration(split.cumulative_elapsed_seconds)})"
    )


class ActivityTypeCalendar(CalendarEntity):
    """One calendar of logged activities for a single activity type (running,
    cycling, ...) within this config entry.

    Split out per type (rather than one calendar mixing every type together)
    so a dashboard can show just the type(s) someone actually wants — e.g. add
    only the Running calendar, leave Cycling off it entirely.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "activity_type_calendar"

    def __init__(
        self,
        coordinator: WorkoutDataUpdateCoordinator,
        entry: ConfigEntry,
        activity_type: ActivityType,
    ) -> None:
        self._hass = coordinator.hass
        self._entry_slug = coordinator.source.key
        self._activity_type = activity_type
        self._attr_unique_id = f"{entry.entry_id}_{activity_type.value}_calendar"
        self._attr_translation_placeholders = {"activity_type": activity_type.value}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=coordinator.source.key.capitalize(),
        )

    @property
    def event(self) -> CalendarEvent | None:
        # Activities are always in the past — there is no "next upcoming
        # event" for a workout log, so this is always None. HA's calendar
        # dashboard/API still work fine via async_get_events below; this
        # property is only used for the "next event" style summary card.
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        activities = await async_get_activities_in_range(
            hass, self._entry_slug, start_date.date(), end_date.date()
        )
        return [
            self._to_event(activity)
            for activity in activities
            if activity.activity_type == self._activity_type
        ]

    def _to_event(self, activity: Activity) -> CalendarEvent:
        # as_local on a naive datetime just attaches the local tzinfo without
        # converting — correct here since Activity.start is already the
        # source's own local wall-clock time (see e.g.
        # sources/garmin.py's _parse_garmin_datetime), not UTC.
        start = dt_util.as_local(activity.start)
        end = start + timedelta(seconds=activity.duration_seconds)
        return CalendarEvent(
            start=start,
            end=end,
            summary=_build_summary(activity),
            description=_build_description(activity),
            uid=activity.source_id,
        )


def _build_summary(activity: Activity) -> str:
    # HA's CalendarEvent has no attributes bag (only summary/description/
    # location, all plain text — see homeassistant.components.calendar), so
    # key stats are packed into the summary itself rather than left buried in
    # description, which needs opening the event to read. The activity type
    # is implied by which calendar this is, so it's left out here.
    parts = [activity.name] if activity.name else []
    if activity.distance_meters:
        parts.append(f"{activity.distance_meters / 1000:.2f} km")
    if activity.distance_meters and activity.duration_seconds:
        avg_pace = _format_pace(activity.duration_seconds / (activity.distance_meters / 1000))
        if avg_pace:
            parts.append(avg_pace)
    if not parts:
        parts.append("Workout")
    return " — ".join(parts)
