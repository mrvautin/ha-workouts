"""Week/month/year-to-date totals per activity type, computed directly from
the activity log rather than HA's recorder statistics "change over period"
mechanism.

Why not just use a Statistics card with stat_type "change" and a calendar
period, pointed at the existing lifetime-cumulative statistic? Because that
path goes through HA core's own period-boundary lookup
(homeassistant/components/recorder/statistics.py's _get_oldest_sum_statistic),
which does a fairly intricate hour-rounded search for the "start of period"
row — and in real-world testing this produced a stuck, visibly wrong
year-to-date figure that didn't move even after new activities landed and the
underlying per-day statistics were independently confirmed correct via
Developer Tools > Statistics. Rather than depend on that path's correctness
for something as simple as "sum of my runs this week/month/year", these
sensors compute it directly and deterministically from Activity records we
already store and fully control (see activity_log.py) — no statistics-table
period math involved at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from homeassistant.core import HomeAssistant

from .activity_log import async_get_activities_in_range
from .models import Activity, ActivityType


@dataclass(slots=True)
class PeriodTotals:
    """Summed totals for one activity type over one period-to-date."""

    distance_km: float
    duration_minutes: float
    calories: float
    activity_count: int


def _period_start(today: date, period: str, week_start_day: int) -> date:
    if period == "week":
        # date.weekday(): Monday=0 .. Sunday=6, matching
        # const.WEEK_START_DAY_OPTIONS's convention.
        days_since_start = (today.weekday() - week_start_day) % 7
        return today - timedelta(days=days_since_start)
    if period == "month":
        return today.replace(day=1)
    if period == "year":
        return today.replace(month=1, day=1)
    raise ValueError(f"Unknown period: {period}")


def _sum_activities(activities: list[Activity]) -> PeriodTotals:
    return PeriodTotals(
        distance_km=sum((a.distance_meters or 0.0) for a in activities) / 1000,
        duration_minutes=sum((a.duration_seconds or 0.0) for a in activities) / 60,
        calories=sum((a.calories or 0.0) for a in activities),
        activity_count=len(activities),
    )


async def async_get_period_totals(
    hass: HomeAssistant,
    entry_slug: str,
    activity_type: ActivityType,
    period: str,
    today: date,
    week_start_day: int,
) -> PeriodTotals:
    """Sum this activity type's activities from the start of `period` through today.

    period is one of "week", "month", "year". today is passed in (rather than
    computed here with dt_util.now().date()) so callers all agree on exactly
    which "today" a batch of sensors is computed against — see
    sensor.py's PeriodToDateSensor, which computes all of one entity's
    week/month/year sensors from the same today value per coordinator update.
    """
    start_day = _period_start(today, period, week_start_day)
    activities = await async_get_activities_in_range(hass, entry_slug, start_day, today)
    matching = [a for a in activities if a.activity_type == activity_type]
    return _sum_activities(matching)
