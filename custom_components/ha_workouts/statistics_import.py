"""Backfills per-activity-type long-term statistics from historical activity data.

Imports lifetime-cumulative running totals (distance/duration/calories per
ActivityType, never resetting) into HA's recorder statistics tables as EXTERNAL
statistics (statistic_id like "ha_workouts:garmin_running_distance_km", source=
DOMAIN) via async_add_external_statistics — the same mechanism the Energy
dashboard uses for non-entity data sources — so Statistics Graph cards have
useful history immediately after setup instead of growing from zero.

External, rather than entity-attached (sensor.*) statistics, is a deliberate
choice, not the original design: an entity-attached statistic_id is expected by
HA's recorder to be driven by that entity's own state_class (see
homeassistant/components/sensor/recorder.py's compile_statistics), which
auto-compiles its OWN competing sum from live state history the moment
state_class is set — independently of, and inconsistent with, statistics we
import ourselves. Leaving state_class unset avoids that auto-compile, but then
trips a *different* built-in mechanism: sensor/recorder.py's
update_statistics_issues creates a permanent "entity no longer has a state
class" repair notice for any sensor.* statistic_id that still has metadata with
source="recorder" but no live state_class — which is unavoidable for us, since
async_import_statistics requires source to equal "recorder" for any
entity-shaped statistic_id. There's no supported way to have both a
state_class-free entity AND a clean repair-free entity-attached statistic.
External statistics sidestep this entirely: HA's compile_statistics and the
repair check both only ever look at real sensor.* entity states, so a
colon-separated statistic_id is invisible to both, permanently — see
homeassistant/components/kitchen_sink/__init__.py's own use of this same
pattern (energy_consumption_kwh, gas_consumption_m3, etc.) for the sanctioned
precedent. The sensor.* entity itself (see sensor.py's CumulativeActivitySensor)
still exists and shows the live running total, but carries no statistics of its
own — statistic_id_slug() below is what backs the actual chartable history.

IMPORTANT: `sum` must be genuinely cumulative-forever, not a per-day or per-period
total. HA's Statistics Graph card computes both its "Sum" and "Change" stat types
as a pure diff of the `sum` column between two points (see
homeassistant/components/recorder/statistics.py, `_augment_result_with_change` and
the frontend's statistics-chart-data.ts) — neither mode consults `last_reset` at
read time; that field only affects how the recorder accumulates `sum` from a live
entity's changing *state* during compilation, and has no effect on how already-
stored `sum` values are diffed back out for a chart. A `sum` that resets to ~0 each
day will therefore always show a large spurious negative "change" spike on every
reset boundary, no matter what `last_reset` is set to. The correct pattern (per HA's
own sensor entity docs) is a monotonically non-decreasing `sum`, like a utility
meter's lifetime total — Change mode's period-boundary diffing is what turns that
into "how much this day/week/month" bars, not resets baked into the stored data.

The live per-activity-type sensors (sensor.py's CumulativeActivitySensor) match
this: they report a running lifetime total, seeded from this backfill's last
imported value on first start, not "today's total from scratch".

The import is gap-aware: it checks whether the full requested range [floor_day,
yesterday] is already covered by statistics for each sensor. If it's fully covered,
nothing is re-fetched — this makes re-running the backfill (e.g. after extending the
configured depth) cheap. If ANY gap exists anywhere in the range — not just at the
oldest edge — the entire range is re-fetched and the complete cumulative series is
rewritten from scratch (running total recomputed day-by-day over the whole range).

This "recompute whole range on any gap" design, rather than trying to detect and
patch individual holes by anchoring new segments onto their neighbors, is
deliberately simple: a gap can appear anywhere (e.g. if HA restarts mid-backfill,
interrupting a long multi-chunk fetch after only some chunks were processed — the
accumulated activities for the whole run are only imported once ALL chunks finish,
so an interruption silently drops the entire in-progress range with no partial
statistics written). Anchoring a new segment onto one existing neighbor only works
when the gap is at the series' edge; a mid-series hole has real data on both sides,
which an anchor-and-prepend approach can't reconcile without risking a different
kind of inconsistency. Recomputing the whole series from re-fetched activity data
is self-healing after any such interruption, at the cost of more API calls when a
gap does need filling (never on a fully-covered re-run).

Runs as a background task (kicked off from sensor.async_setup_entry) and reports its
own progress via BackfillProgress so a status sensor can surface it to the user.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import Activity, ActivityType
from .sources.base import WorkoutSource, WorkoutSourceRateLimitedError

_LOGGER = logging.getLogger(__name__)

#: Activity types that get a distance sensor in addition to duration/calories.
DISTANCE_ACTIVITY_TYPES = {
    ActivityType.RUNNING,
    ActivityType.CYCLING,
    ActivityType.SWIMMING,
    ActivityType.WALKING,
    ActivityType.HIKING,
}

# Both sources paginate in a handful of requests per chunk (Garmin: 20
# activities/request; Strava: up to 200/request but with a tighter 100
# reads/15min budget). 90-day chunks cuts a 5-year backfill to ~21 outer
# requests instead of ~61. The pause between chunks is per-source (see
# WorkoutSource.backfill_chunk_pause_seconds) since Garmin's undocumented
# limit warrants more headroom than Strava's published one.
_CHUNK_DAYS = 90
# If the source still rate-limits us despite the pacing above, back off and
# retry rather than aborting a multi-year import over one transient 429.
_RATE_LIMIT_RETRY_DELAYS = (60, 300, 900)  # 1 min, 5 min, 15 min


def entity_id_slug(entry_slug: str, activity_type: ActivityType, metric: str) -> str:
    """Build the entity_id for a per-activity-type live sensor.

    Purely a display entity now — see module docstring. Its entity_id is
    otherwise unrelated to where its statistics are stored (statistic_id_slug).
    """
    return f"sensor.{entry_slug}_{activity_type.value}_{metric}"


def statistic_id_slug(entry_slug: str, activity_type: ActivityType, metric: str) -> str:
    """Build the external statistic_id backing a per-activity-type sensor's history.

    Deliberately NOT entity-shaped (see module docstring for why): this is what
    both the backfill and the live sensor's incremental updates read/write, and
    what Statistics Graph cards should be pointed at for charting.
    """
    return f"{DOMAIN}:{entry_slug}_{activity_type.value}_{metric}"


def _metrics_for(activity_type: ActivityType) -> list[str]:
    metrics = ["duration_minutes", "calories"]
    if activity_type in DISTANCE_ACTIVITY_TYPES:
        metrics.append("distance_km")
    return metrics


@dataclass
class BackfillProgress:
    """Live progress state for one config entry's backfill, read by the status sensor.

    on_change is set by the status sensor to a HA @callback (async_write_ha_state)
    so it can push updates to itself the moment progress changes, instead of polling.
    Must only be invoked from the event loop, same as the rest of this module.
    """

    state: str = "idle"  # idle | running | backing_off | complete | error
    oldest_day_imported: date | None = None
    target_day: date | None = None
    days_imported_this_run: int = 0
    error: str | None = None
    on_change: Callable[[], None] | None = field(default=None, compare=False)

    def notify(self) -> None:
        if self.on_change is not None:
            self.on_change()


async def async_backfill_activity_statistics(
    hass: HomeAssistant,
    entry_slug: str,
    source: WorkoutSource,
    backfill_days: int,
    progress: BackfillProgress,
    request_lock: asyncio.Lock,
) -> None:
    """Ensure statistics fully cover [backfill_days ago, yesterday] for every sensor.

    If the range is already fully covered (checked day-by-day, not just at the
    oldest edge — see module docstring for why), nothing is re-fetched: cheap to
    call again after extending the configured depth. If any gap exists anywhere in
    the range, the whole range is re-fetched and every sensor's cumulative series
    is rewritten from scratch, so a hole left by e.g. an interrupted previous run
    is always self-healing rather than permanent.

    request_lock is held only around each individual chunk fetch (not the whole,
    potentially multi-hour, backfill) — shared with the coordinator's periodic poll
    so the two never send concurrent request streams to the source, while still
    letting the coordinator's poll interleave between backfill chunks rather than
    being blocked for the run's full duration.
    """
    today = dt_util.now().date()
    end_day = today - timedelta(days=1)
    start_day = date(1970, 1, 1) if backfill_days == 0 else today - timedelta(days=backfill_days)

    progress.state = "running"
    progress.target_day = start_day
    progress.days_imported_this_run = 0
    progress.notify()

    try:
        if start_day > end_day:
            progress.state = "complete"
            progress.notify()
            return

        if await _range_fully_covered(hass, entry_slug, start_day, end_day):
            _LOGGER.debug(
                "No backfill gap for %s; [%s, %s] already covered",
                entry_slug,
                start_day,
                end_day,
            )
            progress.state = "complete"
            progress.oldest_day_imported = start_day
            progress.notify()
            return

        by_type_and_day: dict[ActivityType, dict[date, list[Activity]]] = defaultdict(
            lambda: defaultdict(list)
        )

        chunk_end = end_day
        while chunk_end >= start_day:
            chunk_start = max(start_day, chunk_end - timedelta(days=_CHUNK_DAYS - 1))
            activities = await _fetch_chunk_with_backoff(
                source, chunk_start, chunk_end, progress, request_lock
            )
            for activity in activities:
                by_type_and_day[activity.activity_type][activity.start.date()].append(
                    activity
                )

            progress.days_imported_this_run += (chunk_end - chunk_start).days + 1
            progress.oldest_day_imported = chunk_start
            progress.notify()
            _LOGGER.debug(
                "Backfill fetched %s to %s for %s (%d days so far)",
                chunk_start,
                chunk_end,
                entry_slug,
                progress.days_imported_this_run,
            )

            chunk_end = chunk_start - timedelta(days=1)
            if chunk_end >= start_day:
                await asyncio.sleep(source.backfill_chunk_pause_seconds)

        for activity_type in ActivityType:
            day_buckets = by_type_and_day.get(activity_type, {})
            for metric in _metrics_for(activity_type):
                statistic_id = statistic_id_slug(entry_slug, activity_type, metric)
                await _rewrite_metric_series(
                    hass, statistic_id, activity_type, metric, day_buckets, start_day, end_day
                )

        progress.state = "complete"
        progress.notify()
    except Exception as err:  # surfaced to the status sensor, not raised
        _LOGGER.exception("Backfill failed for %s", entry_slug)
        progress.state = "error"
        progress.error = str(err)
        progress.notify()


async def _fetch_chunk_with_backoff(
    source: WorkoutSource,
    chunk_start: date,
    chunk_end: date,
    progress: BackfillProgress,
    request_lock: asyncio.Lock,
) -> list[Activity]:
    """Fetch one date-range chunk, retrying with increasing delays on rate limiting.

    Neither Garmin's unofficial API nor Strava's documents a reliable retry-after
    for this case; the fixed delays here are a conservative guess. Any other error
    (auth failure, network) still raises immediately rather than retrying, since
    backing off won't fix those.

    request_lock is only held around the actual request, not the (up to 15-minute)
    backoff sleep — otherwise the coordinator's periodic poll would be blocked for
    the full backoff duration instead of just interleaving between chunks.
    """
    for attempt, delay in enumerate((0, *_RATE_LIMIT_RETRY_DELAYS)):
        if delay:
            progress.state = "backing_off"
            progress.notify()
            _LOGGER.warning(
                "Source rate-limited the backfill; waiting %ds before retrying "
                "%s to %s (attempt %d)",
                delay,
                chunk_start,
                chunk_end,
                attempt + 1,
            )
            await asyncio.sleep(delay)
            progress.state = "running"
            progress.notify()

        try:
            async with request_lock:
                return await source.async_fetch_activities_range(chunk_start, chunk_end)
        except WorkoutSourceRateLimitedError:
            if attempt == len(_RATE_LIMIT_RETRY_DELAYS):
                raise

    return []  # unreachable, satisfies type checking


async def _range_fully_covered(
    hass: HomeAssistant, entry_slug: str, start_day: date, end_day: date
) -> bool:
    """Check whether every day in [start_day, end_day] has a statistics row for
    every one of this entry's sensors — not just whether the oldest edge is
    covered. A single day missing anywhere (e.g. a hole left by an interrupted
    previous run) means the range is not fully covered.
    """
    statistic_ids = {
        statistic_id_slug(entry_slug, activity_type, metric)
        for activity_type in ActivityType
        for metric in _metrics_for(activity_type)
    }
    expected_days = (end_day - start_day).days + 1

    def _query() -> bool:
        rows = statistics_during_period(
            hass,
            datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(end_day, datetime.max.time(), tzinfo=timezone.utc),
            statistic_ids,
            "day",
            None,
            {"sum"},
        )
        if len(rows) < len(statistic_ids):
            return False
        return all(len(series) >= expected_days for series in rows.values())

    return await get_instance(hass).async_add_executor_job(_query)


_METRIC_UNITS: dict[str, tuple[str, str | None]] = {
    "distance_km": ("km", "distance"),
    "duration_minutes": ("min", None),
    "calories": ("kcal", None),
}


def _build_metric_metadata(
    statistic_id: str, activity_type: ActivityType, metric: str
) -> StatisticMetaData:
    unit, unit_class = _METRIC_UNITS[metric]
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{activity_type.value.replace('_', ' ').title()} {metric.replace('_', ' ')}",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_class=unit_class,
        unit_of_measurement=unit,
    )


async def _rewrite_metric_series(
    hass: HomeAssistant,
    statistic_id: str,
    activity_type: ActivityType,
    metric: str,
    day_buckets: dict[date, list[Activity]],
    start_day: date,
    end_day: date,
) -> None:
    """Recompute and overwrite the entire [start_day, end_day] cumulative series.

    Deliberately does not try to anchor onto any existing data (see module
    docstring for why): async_add_external_statistics upserts per-day rows, so
    writing every day fresh from the just-fetched activities always produces
    one self-consistent, genuinely monotonic series, whether or not a gap
    previously existed anywhere in this range.
    """
    if start_day > end_day:
        return

    metadata = _build_metric_metadata(statistic_id, activity_type, metric)

    # sum must be a genuinely lifetime-cumulative running total (never resets) —
    # see module docstring for why: HA's Statistics Graph "Change" mode computes
    # daily/weekly/monthly bars as a pure diff between period sums, with no
    # awareness of last_reset, so a value that resets would produce large
    # spurious negative spikes there.
    statistic_data: list[StatisticData] = []
    running = 0.0
    day = start_day
    while day <= end_day:
        running += _sum_metric(day_buckets.get(day, []), metric)
        # Local noon avoids DST-boundary ambiguity when the recorder truncates to the hour.
        bucket_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=12
        )
        statistic_data.append(StatisticData(start=bucket_start, sum=running, state=running))
        day += timedelta(days=1)

    async_add_external_statistics(hass, metadata, statistic_data)
    # async_add_external_statistics only QUEUES the write on the recorder's
    # background thread — it returns immediately, before the row is actually
    # committed. This was a real, repeatedly-hit bug: the backfill task would
    # resolve as "done" (see async_backfill_activity_statistics) and
    # immediately trigger creating the live CumulativeActivitySensor, whose
    # seeding query (get_last_statistics, called from async_added_to_hass)
    # would then run before this write had actually landed — finding nothing
    # and seeding at 0, permanently disconnecting the live sensor's running
    # total from the backfill's correct value. Waiting for the recorder to
    # drain its queue here makes the write actually durable before this
    # function (and therefore the backfill task) reports completion.
    await get_instance(hass).async_block_till_done()
    _LOGGER.debug(
        "Rewrote %d days of statistics for %s", len(statistic_data), statistic_id
    )


async def async_apply_activity_deltas(
    hass: HomeAssistant,
    statistic_id: str,
    activity_type: ActivityType,
    metric: str,
    deltas_by_day: dict[date, float],
) -> None:
    """Fold newly-seen activities into the statistics series, keyed by each
    activity's own real calendar date — not "now".

    This is the ONLY way live per-activity-type sensor updates should reach the
    statistics table. statistic_id here is always the EXTERNAL statistic_id
    from statistic_id_slug() (e.g. "ha_workouts:garmin_running_distance_km"),
    never the sensor.* entity_id itself — see module docstring for why.

    Keying by real date matters most for Apple Health: workouts arrive via
    webhook, in whatever order and however long after the fact the user's
    Shortcut happens to run (a whole history's worth can land within the same
    second). An earlier version of this function wrote every update at
    dt_util.utcnow(), stamping the entire lifetime total onto a single "right
    now" bucket with no rows on any of the real activity dates — a Statistics
    Graph card then had nothing to diff day-by-day against: one big number on
    one day, nothing before it. Backfilled sources (Garmin/Strava) never call
    this with a non-today date, since their full history already arrives via
    _rewrite_metric_series; this function gives Apple Health the same "one row
    per real day" shape, incrementally, as each webhook delivery lands.

    async_add_external_statistics can only overwrite a bucket's absolute sum,
    not apply a relative delta, so inserting a delta on a past day requires
    re-stamping every day from that point forward with the shifted cumulative
    total — otherwise later days would still show their old (too-low) sum and
    the series would appear to dip back down the next day. This reads back each
    existing day's own contribution (its sum minus the previous day's), applies
    the new deltas on top, and rewrites the whole [earliest affected day, today]
    tail as a fresh running total in one pass.

    (Historical note: an earlier version wrote to the sensor.* entity's own
    statistic_id with state_class set, letting HA's recorder auto-compile its
    own statistics for the same statistic_id independently of these writes —
    re-establishing its own "zero point" from the live state on every HA
    restart. That produced two competing sum sequences that diverged by a
    fixed offset, seen as a spurious spike in Statistics Graph cards. Moving
    to a separate external statistic_id — see module docstring — eliminates
    the conflict at the source rather than working around it.)
    """
    if not deltas_by_day:
        return

    earliest_day = min(deltas_by_day)
    today = dt_util.now().date()
    # Enough raw rows to comfortably cover from the oldest affected day to today,
    # plus slack — this integration only ever writes at most one row per day.
    rows_needed = (today - earliest_day).days + 30

    def _query_existing() -> dict[date, float]:
        # Deliberately NOT statistics_during_period(..., period="day", ...): that
        # reduces/re-labels rows onto LOCAL-timezone day boundaries, whereas every
        # row this integration writes is stamped at noon UTC (see
        # _rewrite_metric_series). Converting a local-midnight-labeled boundary
        # back to a UTC date with .date() silently shifts the day by one whenever
        # the local UTC offset is non-zero (e.g. Australia/Adelaide, UTC+9:30) —
        # this was a real bug: it mapped 15 Aug's row to "14 Aug" and made a new
        # activity look like it landed on a day with no prior total, overwriting
        # the whole series' running total back down to just that day's delta.
        # get_last_statistics returns raw, unreduced rows, so the noon-UTC
        # timestamp we ourselves wrote round-trips through .date() safely for any
        # real-world UTC offset.
        rows = get_last_statistics(hass, rows_needed, statistic_id, False, {"sum"})
        series = rows.get(statistic_id, [])
        return {
            dt_util.utc_from_timestamp(row["start"]).date(): row["sum"] or 0.0
            for row in series
        }

    existing_by_day = await get_instance(hass).async_add_executor_job(_query_existing)

    metadata = _build_metric_metadata(statistic_id, activity_type, metric)
    statistic_data: list[StatisticData] = []
    running = existing_by_day.get(earliest_day - timedelta(days=1), 0.0)
    day = earliest_day
    while day <= today:
        prior_total = existing_by_day.get(day - timedelta(days=1), running)
        day_own_total = existing_by_day.get(day, prior_total)
        day_own_contribution = max(day_own_total - prior_total, 0.0)
        running += day_own_contribution + deltas_by_day.get(day, 0.0)
        bucket_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=12
        )
        statistic_data.append(StatisticData(start=bucket_start, sum=running, state=running))
        day += timedelta(days=1)

    async_add_external_statistics(hass, metadata, statistic_data)
    # See _rewrite_metric_series for why this wait matters:
    # async_add_external_statistics only queues the write, it doesn't block
    # until it's committed.
    await get_instance(hass).async_block_till_done()


def _sum_metric(activities: list[Activity], metric: str) -> float:
    if metric == "distance_km":
        return sum((a.distance_meters or 0) for a in activities) / 1000
    if metric == "duration_minutes":
        return sum((a.duration_seconds or 0) for a in activities) / 60
    if metric == "calories":
        return sum((a.calories or 0) for a in activities)
    return 0.0
