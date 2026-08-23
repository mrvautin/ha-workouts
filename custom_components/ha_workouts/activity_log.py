"""Persisted, per-config-entry log of individual activities (with splits, when
available), queryable by date range.

This exists because HA's CalendarEntity (see calendar.py) must answer "what
happened between date A and date B" — the coordinator only ever holds
whatever a single poll returned, never a queryable history. The long-term
statistics tables (statistics_import.py) aren't a substitute either: they
store per-activity-type cumulative totals for charting, not individual
activity records, and have no per-km split detail at all.

Uses HA's Store helper (a debounced-write JSON file under .storage/) rather
than the recorder database — this data isn't a time-series metric the
recorder's statistics/history machinery is built for, and Store's
load-everything-into-memory model is a fine fit for what's realistically a few
hundred to a few thousand activities over the life of an install.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import SPLITS_BACKFILL_PAUSE_SECONDS
from .models import Activity, ActivitySplit, ActivityType
from .statistics_import import BackfillProgress

if TYPE_CHECKING:
    from .sources.garmin import GarminSource

_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION = 1


def _store(hass: HomeAssistant, entry_slug: str) -> Store[list[dict]]:
    return Store(hass, _STORAGE_VERSION, f"ha_workouts_activity_log_{entry_slug}")


def _activity_to_dict(activity: Activity) -> dict:
    data = asdict(activity)
    data["activity_type"] = activity.activity_type.value
    data["start"] = activity.start.isoformat()
    # splits is already a list of plain dicts via asdict()'s recursion into
    # the nested ActivitySplit dataclasses — no further conversion needed.
    return data


def _activity_from_dict(data: dict) -> Activity:
    splits_data = data.get("splits")
    splits = (
        [ActivitySplit(**split) for split in splits_data] if splits_data is not None else None
    )
    return Activity(
        source=data["source"],
        source_id=data["source_id"],
        activity_type=ActivityType(data["activity_type"]),
        start=datetime.fromisoformat(data["start"]),
        duration_seconds=data["duration_seconds"],
        distance_meters=data.get("distance_meters"),
        calories=data.get("calories"),
        avg_heart_rate=data.get("avg_heart_rate"),
        max_heart_rate=data.get("max_heart_rate"),
        elevation_gain_meters=data.get("elevation_gain_meters"),
        name=data.get("name"),
        splits=splits,
    )


async def async_load_activities(hass: HomeAssistant, entry_slug: str) -> dict[str, Activity]:
    """Load the full persisted log, keyed by Activity.source_id."""
    raw = await _store(hass, entry_slug).async_load()
    if not raw:
        return {}
    activities = (_activity_from_dict(item) for item in raw)
    return {activity.source_id: activity for activity in activities}


async def async_save_activities(
    hass: HomeAssistant, entry_slug: str, activities: dict[str, Activity]
) -> None:
    """Overwrite the persisted log with the given full set of activities."""
    await _store(hass, entry_slug).async_save([_activity_to_dict(a) for a in activities.values()])


async def async_record_activities(
    hass: HomeAssistant, entry_slug: str, new_activities: list[Activity]
) -> None:
    """Merge new_activities into the persisted log (upsert by source_id) and save.

    Upserting rather than only-inserting matters for splits specifically: an
    activity can first be recorded via the live daily poll (with splits, if
    GarminSource.async_fetch fetched them successfully) and later be
    re-encountered by the splits backfill job, or vice-versa if the live fetch
    failed to get splits but the backfill job later fills them in — the later
    write should win rather than the log getting stuck with an incomplete
    first-seen copy forever.
    """
    if not new_activities:
        return
    existing = await async_load_activities(hass, entry_slug)
    for activity in new_activities:
        existing[activity.source_id] = activity
    await async_save_activities(hass, entry_slug, existing)


async def async_get_activities_in_range(
    hass: HomeAssistant, entry_slug: str, start_day: date, end_day: date
) -> list[Activity]:
    """Return activities whose start date falls within [start_day, end_day], sorted by start."""
    activities = await async_load_activities(hass, entry_slug)
    in_range = [a for a in activities.values() if start_day <= a.start.date() <= end_day]
    in_range.sort(key=lambda a: a.start)
    return in_range


async def async_backfill_activity_splits(
    hass: HomeAssistant,
    entry_slug: str,
    source: GarminSource,
    splits_backfill_days: int,
    progress: BackfillProgress,
    request_lock: asyncio.Lock,
) -> None:
    """Opt-in background job: fetch per-km splits for past activities that don't have them yet.

    Garmin-only (the only source with a splits API right now — see
    sources/garmin.py). Deliberately separate from, and far more slowly paced
    than, async_backfill_activity_statistics: that job fetches whole date
    ranges in a handful of requests; this one needs ONE request PER ACTIVITY,
    since Garmin's API has no batch/bulk splits endpoint. Left as opt-in
    (default Off, see const.DEFAULT_SPLITS_BACKFILL_DAYS) rather than
    automatic, since even a modest running history can mean hundreds of extra
    requests — SPLITS_BACKFILL_PAUSE_SECONDS between each keeps this well
    clear of Garmin's rate limits even for a full multi-year history, at the
    cost of the job taking hours for a large backfill depth.

    request_lock is shared with the coordinator's periodic poll and the main
    statistics backfill (see coordinator.py/statistics_import.py) so none of
    them ever send concurrent request streams to Garmin.
    """
    if splits_backfill_days < 0:
        # "Off" (the default) — see const.SPLITS_BACKFILL_DAYS_OPTIONS.
        return

    today = dt_util.now().date()
    start_day = date(1970, 1, 1) if splits_backfill_days == 0 else today - timedelta(
        days=splits_backfill_days
    )

    activities = await async_load_activities(hass, entry_slug)
    # splits is None means "never fetched" — see models.Activity's docstring
    # for why that's distinct from an empty list ("fetched, no laps").
    pending = sorted(
        (
            a
            for a in activities.values()
            if a.splits is None and start_day <= a.start.date() <= today
        ),
        key=lambda a: a.start,
        reverse=True,  # most recent first — most likely to matter to the user sooner
    )

    if not pending:
        progress.state = "complete"
        progress.notify()
        return

    progress.state = "running"
    progress.target_day = start_day
    progress.days_imported_this_run = 0
    progress.notify()

    try:
        for i, activity in enumerate(pending):
            async with request_lock:
                activity.splits = await source.async_fetch_splits(activity.source_id)
            activities[activity.source_id] = activity
            # Save incrementally (not just once at the end) so a HA restart
            # mid-job doesn't lose already-fetched splits — the next run's
            # `pending` query naturally picks up wherever this one left off,
            # since it re-derives "still missing splits" from stored state
            # rather than tracking its own separate progress cursor.
            await async_save_activities(hass, entry_slug, activities)

            progress.days_imported_this_run = i + 1
            progress.oldest_day_imported = activity.start.date()
            progress.notify()

            if i < len(pending) - 1:
                await asyncio.sleep(SPLITS_BACKFILL_PAUSE_SECONDS)

        progress.state = "complete"
        progress.notify()
    except Exception as err:  # surfaced to the status sensor, not raised
        _LOGGER.exception("Splits backfill failed for %s", entry_slug)
        progress.state = "error"
        progress.error = str(err)
        progress.notify()
