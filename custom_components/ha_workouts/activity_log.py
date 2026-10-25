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

from .backfill_progress import BackfillProgress
from .const import SPLITS_BACKFILL_PAUSE_SECONDS
from .models import Activity, ActivitySplit, ActivityType

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


async def async_reset_single_whole_activity_splits(hass: HomeAssistant, entry_slug: str) -> int:
    """One-time migration: reset any activity whose stored splits are the
    unmistakable signature of a real, now-fixed parsing bug back to None, so
    the existing splits backfill job (async_backfill_activity_splits, which
    only re-fetches activities with splits is None) naturally re-fetches
    real per-km data for them.

    The bug (sources/garmin.py, fixed alongside this migration): Garmin's
    JSON activity-detail endpoint's splitSummaries field formally supports
    per-km markers (splitType MARKER_KILOMETER) but — confirmed by testing
    against 11+ real activities — never actually returns them; the old
    parser instead kept a DIFFERENT splitType (INTERVAL_ACTIVE) that turned
    out to be a single whole-activity-duration summary bucket, not a real
    lap. Every activity synced before the fix (which downloads and parses
    the original .FIT file instead) ended up with exactly one "split"
    covering its entire distance — that specific shape (len(splits) == 1
    AND that split's distance within 1 metre of the activity's own total
    distance) is the unambiguous signature of this bug, distinct from a
    genuinely short activity that might legitimately have just one real
    km-split (which wouldn't coincidentally match the activity's FULL
    distance to within a metre — a real single first-km split of a longer
    run is shorter than the activity's total distance).

    Returns the number of activities reset, for a one-line log message at
    the call site.
    """
    activities = await async_load_activities(hass, entry_slug)
    reset_count = 0
    for activity in activities.values():
        if (
            activity.splits is not None
            and len(activity.splits) == 1
            and activity.distance_meters is not None
            and abs(activity.splits[0].distance_meters - activity.distance_meters) < 1.0
        ):
            activity.splits = None
            reset_count += 1
    if reset_count:
        await async_save_activities(hass, entry_slug, activities)
    return reset_count


async def async_wipe_activity_log(hass: HomeAssistant, entry_slug: str) -> None:
    """Permanently delete the persisted activity log for entry_slug.

    Used when the last config entry for a source type is removed (see
    __init__.py's async_remove_entry / statistics_import.async_wipe_source_data)
    so a later re-add starts genuinely fresh instead of silently reusing old
    data — entry_slug is a static per-source-type string (e.g. "coros"), not
    tied to any one config entry's id, so this data would otherwise survive
    delete+re-add indefinitely.
    """
    await _store(hass, entry_slug).async_remove()


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
    backfill_days: int,
    progress: BackfillProgress,
    request_lock: asyncio.Lock,
) -> None:
    """Background job: fetch per-km splits for past activities that don't have them yet.

    Garmin-only (the only source with a splits API right now — see
    sources/garmin.py). Shares its depth with the main activity/statistics
    backfill (CONF_BACKFILL_DAYS/"History to import") rather than having its
    own separate setting — deliberately simple, one knob for "how much
    history", even though this job is far more slowly paced than
    async_backfill_activity_statistics: that job fetches whole date ranges in
    a handful of requests, this one needs ONE request PER ACTIVITY, since
    Garmin's API has no batch/bulk splits endpoint. SPLITS_BACKFILL_PAUSE_SECONDS
    between each keeps this well clear of Garmin's rate limits even for a full
    multi-year history, at the cost of the job taking hours for a large depth.

    request_lock is shared with the coordinator's periodic poll and the main
    statistics backfill (see coordinator.py/statistics_import.py) so none of
    them ever send concurrent request streams to Garmin.
    """
    today = dt_util.now().date()
    start_day = date(1970, 1, 1) if backfill_days == 0 else today - timedelta(
        days=backfill_days
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
            # async_record_activities, NOT a hand-rolled save of the
            # `activities` dict loaded once at the top of this function —
            # this job can run for hours (one request per activity,
            # SPLITS_BACKFILL_PAUSE_SECONDS apart), so that dict is a
            # long-stale snapshot by the time later iterations save. Writing
            # it back wholesale on every iteration was a real, confirmed
            # bug: it silently clobbered fresher data the live coordinator
            # poll wrote for the SAME activity after this job's snapshot was
            # taken (e.g. corrected splits from a bug fix deployed mid-run)
            # right back to the stale pre-snapshot value, repeatedly, for
            # the rest of the job. async_record_activities re-loads the
            # CURRENT on-disk state immediately before merging and saving
            # just this one activity, so a concurrent fresher write always
            # wins rather than being silently overwritten — see its own
            # docstring, which already documents this exact guarantee.
            await async_record_activities(hass, entry_slug, [activity])

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
