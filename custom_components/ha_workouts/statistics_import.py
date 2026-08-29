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
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .activity_log import async_get_activities_in_range, async_record_activities
from .backfill_progress import BackfillProgress
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

# For "All available history" (backfill_days=0, start_day=1970-01-01): stop
# walking further back once this many consecutive chunks come back with zero
# activities. Without this, an account with a real history of only, say, a
# few months would still walk chunk-by-chunk all the way back to 1970 — every
# one of those chunks genuinely empty, but each one still costs a full
# request + backfill_chunk_pause_seconds wait, for no benefit. This is a real
# bug that was hit in practice: a real account's backfill was still fetching
# chunks back to 2018 with zero real data past ~Feb 2026. 3 consecutive empty
# chunks (~270 days with zero activities) is generous enough to not falsely
# stop during a genuinely long inactive stretch (e.g. an injury break) while
# still cutting off a many-years-long walk through empty history quickly.
_CONSECUTIVE_EMPTY_CHUNKS_TO_STOP = 3

# Serializes async_apply_activity_deltas calls per statistic_id. Each call reads
# the latest stored sum, computes a new running total, then writes it back —
# a classic read-modify-write race if two calls for the SAME statistic_id ever
# overlap (observed in production: HA startup can fire a CumulativeActivitySensor's
# coordinator listener more than once in quick succession — e.g. its own explicit
# post-seed call plus a coordinator refresh scheduled by listener registration —
# each spawning an unawaited hass.async_create_task(async_apply_activity_deltas(...))
# for the same statistic_id). Without this lock, both calls read the same
# baseline before either writes, so the same day's activity gets added to the
# running total twice — a real, confirmed double-count bug, not hypothetical.
# A plain dict (not weak-keyed) is fine: the number of distinct statistic_ids is
# small and fixed per config entry, so this never grows unbounded.
_statistic_write_locks: dict[str, asyncio.Lock] = {}


def _lock_for(statistic_id: str) -> asyncio.Lock:
    lock = _statistic_write_locks.get(statistic_id)
    if lock is None:
        lock = asyncio.Lock()
        _statistic_write_locks[statistic_id] = lock
    return lock


# Persisted, cross-instance record of which activity source_ids have already
# had their delta applied to each statistic_id. The lock above only prevents
# two writes for the same statistic_id from corrupting each other's read — it
# does NOT stop the same activity's delta being added twice if it's genuinely
# presented to async_apply_activity_deltas more than once (e.g. by two
# separately-constructed CumulativeActivitySensor instances for the same
# statistic_id, each with their own independent in-memory
# _counted_source_ids, coexisting briefly if HA runs async_setup_entry more
# than once for a slow-starting config entry — observed in production via a
# "Setup timed out for bootstrap" warning with multiple concurrent
# async_apply_activity_deltas tasks pending for the same statistic). Each
# entity's own _counted_source_ids remains the fast-path check to avoid
# calling this function at all for activities it's already seen locally; this
# store is the durable backstop that makes a genuine double-call a no-op
# regardless of what caused it. Keyed by statistic_id (not shared across
# metrics) since each metric's write is independent.
_STORAGE_VERSION = 1


def _applied_source_ids_store(hass: HomeAssistant, statistic_id: str) -> Store[list[str]]:
    # statistic_id already only contains slug-safe characters (see
    # statistic_id_slug) other than ":", which Store's key must not contain.
    key = f"ha_workouts_applied_source_ids_{statistic_id.replace(':', '_')}"
    return Store(hass, _STORAGE_VERSION, key)


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


_EARLIEST_DAY_STORAGE_VERSION = 1


def _earliest_known_activity_day_store(hass: HomeAssistant, entry_slug: str) -> Store[str]:
    return Store(hass, _EARLIEST_DAY_STORAGE_VERSION, f"ha_workouts_earliest_day_{entry_slug}")


async def async_get_earliest_known_activity_day(hass: HomeAssistant, entry_slug: str) -> date | None:
    """The earliest day this backfill has confirmed has (or could have) real
    activity data, if that's ever been established — see
    async_backfill_activity_statistics's early-stop logic for how.

    Also exposed as sensor.py's HistoryStartSensor, both to make this visible
    (rather than a purely internal cache) and to double as the persistence
    that lets future backfill runs skip re-walking a confirmed-empty range —
    see that function's docstring for why re-deriving this from scratch every
    run would otherwise be a real, confirmed performance bug for "All
    available history" on an account with only a few months of real data.
    """
    raw = await _earliest_known_activity_day_store(hass, entry_slug).async_load()
    return date.fromisoformat(raw) if raw else None


async def async_set_earliest_known_activity_day(
    hass: HomeAssistant, entry_slug: str, day: date
) -> None:
    await _earliest_known_activity_day_store(hass, entry_slug).async_save(day.isoformat())


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

    # If a previous run already walked back through empty history and hit the
    # early-stop below, don't re-walk that same confirmed-empty range again —
    # start from whichever is later: the requested depth, or the previously
    # discovered real boundary. Only relevant for "All available history";
    # for a fixed depth start_day is already bounded and this is a no-op.
    cached_earliest = await async_get_earliest_known_activity_day(hass, entry_slug)
    if cached_earliest is not None and cached_earliest > start_day:
        start_day = cached_earliest

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

        # Tracks where the fetch loop actually stopped, which becomes the
        # real start_day passed to _rewrite_metric_series below — see the
        # early-stop logic further down for why this can end up later than
        # the originally-requested start_day.
        earliest_day_reached = end_day
        consecutive_empty_chunks = 0

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

            earliest_day_reached = chunk_start
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

            if activities:
                consecutive_empty_chunks = 0
            else:
                consecutive_empty_chunks += 1
                if consecutive_empty_chunks >= _CONSECUTIVE_EMPTY_CHUNKS_TO_STOP:
                    # For a fixed depth (backfill_days != 0) this can't
                    # trigger meaningfully early — start_day is already a
                    # bounded, user-chosen window — but for "All available
                    # history" (backfill_days=0, start_day=1970-01-01) this
                    # is what stops a real account (with, say, only a few
                    # months of genuine history) from walking chunk-by-chunk
                    # all the way back to 1970, burning a full request +
                    # pacing delay per chunk for years of guaranteed-empty
                    # history. See _CONSECUTIVE_EMPTY_CHUNKS_TO_STOP's
                    # comment for why 3 chunks specifically.
                    _LOGGER.debug(
                        "Stopping backfill for %s after %d consecutive empty "
                        "chunks — assuming no history before %s",
                        entry_slug,
                        consecutive_empty_chunks,
                        chunk_start,
                    )
                    await async_set_earliest_known_activity_day(
                        hass, entry_slug, earliest_day_reached
                    )
                    break

            chunk_end = chunk_start - timedelta(days=1)
            if chunk_end >= start_day:
                await asyncio.sleep(source.backfill_chunk_pause_seconds)

        for activity_type in ActivityType:
            day_buckets = by_type_and_day.get(activity_type, {})
            for metric in _metrics_for(activity_type):
                statistic_id = statistic_id_slug(entry_slug, activity_type, metric)
                await _rewrite_metric_series(
                    hass,
                    statistic_id,
                    activity_type,
                    metric,
                    day_buckets,
                    earliest_day_reached,
                    end_day,
                )

        # Also persist every fetched activity into the activity log (see
        # activity_log.py) — without this, the log only ever contains
        # activities seen via the live daily poll (today's), so a user
        # running the opt-in splits backfill (activity_log's
        # async_backfill_activity_splits) right after their first setup would
        # find nothing to backfill: there'd be no historical activity RECORDS
        # for it to fill splits into yet, even though the statistics tables
        # above already have the aggregated distance/duration/calories.
        all_activities = [
            activity
            for day_bucket in by_type_and_day.values()
            for activities_on_day in day_bucket.values()
            for activity in activities_on_day
        ]
        if all_activities:
            await async_record_activities(hass, entry_slug, all_activities)

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
    """Recompute and overwrite the [start_day, end_day] cumulative series.

    Rewrites every day IN this range from scratch based on freshly-fetched
    activities (see module docstring for why: async_add_external_statistics
    upserts per-day rows, so this always produces one self-consistent,
    genuinely monotonic series across the range, whether or not a gap
    previously existed anywhere in it).

    Critically, this does NOT assume start_day is the true beginning of all
    history — for any fixed backfill depth (not "All available history",
    backfill_days=0), start_day is just the current edge of a ROLLING window
    (today minus the configured depth), which moves forward by one day every
    single day. Starting `running` at 0.0 unconditionally, as an earlier
    version of this function did, meant every re-run of the backfill —
    which happens on essentially every restart once at least a day has
    passed, since _range_fully_covered always finds "yesterday" freshly
    uncovered — silently overwrote whatever real cumulative total already
    existed at the new start_day with 0, permanently severing continuity
    with every earlier, out-of-window day. This was a real, confirmed bug:
    a statistics consistency check caught a series dropping from a real
    value straight to 0.00 exactly 365 days before "today", repeating on
    every subsequent day as the window rolled forward.
    (`existing_by_day` is the same lookup technique
    async_apply_activity_deltas uses, and for the same reason: get_last_statistics
    returns raw, unreduced rows, keyed by their real noon-UTC timestamp's
    `.date()`, which round-trips safely regardless of the recorder's local
    timezone — see that function's docstring for the timezone bug this avoids.)
    """
    if start_day > end_day:
        return

    # Shares _lock_for(statistic_id) with async_apply_activity_deltas — without
    # this, the two could race: the live poll reads a baseline before this
    # rewrite lands, writes today's row on top of it, and then this rewrite
    # raises an EARLIER day's total using fresher fetched data without
    # touching today's already-written row — leaving today's cumulative sum
    # stuck lower than yesterday's. Confirmed in production: a backfill run
    # raised yesterday's total after the live poll had already stamped
    # today's total using the old, lower baseline, producing a negative
    # "change" for today. See async_apply_activity_deltas's own lock comment
    # for the analogous same-process bug this same lock already prevents.
    async with _lock_for(statistic_id):
        await _rewrite_metric_series_locked(
            hass, statistic_id, activity_type, metric, day_buckets, start_day, end_day
        )


async def _rewrite_metric_series_locked(
    hass: HomeAssistant,
    statistic_id: str,
    activity_type: ActivityType,
    metric: str,
    day_buckets: dict[date, list[Activity]],
    start_day: date,
    end_day: date,
) -> None:
    metadata = _build_metric_metadata(statistic_id, activity_type, metric)

    def _query_baseline() -> float:
        # Deliberately NOT get_last_statistics(hass, 1, ...): that returns
        # the single most recent row in the WHOLE table, which — critically
        # — is very often already inside [start_day, end_day] itself (e.g.
        # yesterday's row from the previous backfill run, which this very
        # call is about to overwrite). That was a real bug in an earlier
        # version of this fix: it silently returned 0.0 as "no genuine
        # baseline" even when perfectly good older history existed, because
        # the wrong row was being inspected. What's actually needed is the
        # latest row STRICTLY BEFORE start_day.
        #
        # Also deliberately NOT statistics_during_period(..., period="day",
        # ...): that reduces/re-labels rows onto LOCAL-timezone day
        # boundaries (see async_apply_activity_deltas's docstring for the
        # exact, previously-hit bug this causes — a noon-UTC row can get
        # mislabeled a day off in a non-zero-UTC-offset timezone). Since this
        # baseline lookup needs to draw a precise line at exactly start_day,
        # that mislabeling risk is unacceptable here.
        #
        # Instead: request enough of the most recent RAW (unreduced) rows to
        # be certain of reaching past start_day even for a multi-year
        # backfill depth, then filter to the one immediately before
        # start_day by its real noon-UTC timestamp's .date() — the same
        # safe technique used throughout this module.
        rows_needed = (end_day - start_day).days + 30
        rows = get_last_statistics(hass, rows_needed, statistic_id, False, {"sum"})
        series = rows.get(statistic_id, [])
        # get_last_statistics returns newest-first.
        for row in series:
            if dt_util.utc_from_timestamp(row["start"]).date() < start_day:
                return row["sum"] or 0.0
        return 0.0

    def _query_tail() -> dict[date, float]:
        # Rows the live poll (async_apply_activity_deltas) already wrote AFTER
        # end_day — typically just "today", since the backfill's end_day is
        # always yesterday. These were each stamped as (baseline-at-poll-time
        # + that day's own delta), using whatever end_day's total was BEFORE
        # this rewrite potentially raises it (e.g. a fresh backfill fetch
        # finding more of yesterday's activity than the live poll had seen).
        # Left alone, a tail row would stay stuck at its old, now-too-low
        # absolute sum — producing a negative "change" for today once
        # end_day's total moves past it. Re-stamping each tail day's own
        # contribution on top of the freshly rewritten running total keeps
        # the whole series monotonic across the rewrite boundary.
        rows = get_last_statistics(hass, 30, statistic_id, False, {"sum"})
        series = rows.get(statistic_id, [])
        return {
            dt_util.utc_from_timestamp(row["start"]).date(): row["sum"] or 0.0
            for row in series
            if dt_util.utc_from_timestamp(row["start"]).date() > end_day
        }

    baseline = await get_instance(hass).async_add_executor_job(_query_baseline)
    tail_by_day = await get_instance(hass).async_add_executor_job(_query_tail)

    # sum must be a genuinely lifetime-cumulative running total (never resets) —
    # see module docstring for why: HA's Statistics Graph "Change" mode computes
    # daily/weekly/monthly bars as a pure diff between period sums, with no
    # awareness of last_reset, so a value that resets would produce large
    # spurious negative spikes there.
    statistic_data: list[StatisticData] = []
    running = baseline
    day = start_day
    while day <= end_day:
        running += _sum_metric(day_buckets.get(day, []), metric)
        # Local noon avoids DST-boundary ambiguity when the recorder truncates to the hour.
        bucket_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(
            hour=12
        )
        statistic_data.append(StatisticData(start=bucket_start, sum=running, state=running))
        day += timedelta(days=1)

    prior_tail_total = running
    for tail_day in sorted(tail_by_day):
        prior_day_total = tail_by_day.get(tail_day - timedelta(days=1), prior_tail_total)
        tail_day_contribution = max(tail_by_day[tail_day] - prior_day_total, 0.0)
        running += tail_day_contribution
        bucket_start = datetime.combine(
            tail_day, datetime.min.time(), tzinfo=timezone.utc
        ).replace(hour=12)
        statistic_data.append(StatisticData(start=bucket_start, sum=running, state=running))
        prior_tail_total = tail_by_day[tail_day]

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
    entry_slug: str,
    statistic_id: str,
    activity_type: ActivityType,
    metric: str,
    deltas_by_day: dict[date, list[tuple[str, float]]],
) -> None:
    """Fold newly-seen activities into the statistics series, keyed by each
    activity's own real calendar date — not "now".

    This is the ONLY way live per-activity-type sensor updates should reach the
    statistics table. statistic_id here is always the EXTERNAL statistic_id
    from statistic_id_slug() (e.g. "ha_workouts:garmin_running_distance_km"),
    never the sensor.* entity_id itself — see module docstring for why.

    deltas_by_day maps each day to a list of (Activity.source_id, value) pairs
    rather than a single pre-summed float — each entry is individually checked
    against a persisted "already applied" ledger (see
    _applied_source_ids_store) before being folded into that day's total. This
    is what makes the whole call idempotent: calling it twice with the exact
    same activities is a safe no-op the second time, which matters because the
    caller's own dedup (CumulativeActivitySensor._counted_source_ids) is
    per-entity-instance, in-memory state — it can't protect against two
    separate entity instances for the same statistic_id both believing the
    same activity is new (observed in production: HA can run
    async_setup_entry more than once for a slow-starting config entry,
    producing two CumulativeActivitySensor instances that each independently
    scheduled an async_apply_activity_deltas call for the same activity,
    double-counting its distance).

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
    the series would appear to dip back down the next day.

    Each affected day's OWN contribution is derived fresh from the activity
    log (async_get_activities_in_range), the same source of truth
    _rewrite_metric_series uses — deliberately NOT inferred by diffing this
    day's previously-stored sum against the previous day's (an earlier
    version did exactly that). That diff is only valid if neither stored
    value has changed since it was written, which doesn't hold here: this
    function runs concurrently with the backfill's _rewrite_metric_series
    (see the shared _lock_for(statistic_id) above), which can raise an
    EARLIER day's stored sum after this function already stamped a LATER
    day's total on top of the old, lower value — a real, confirmed bug: a
    backfill run corrected yesterday's total upward using freshly-fetched
    data without anything then re-deriving today's, permanently leaving
    today's cumulative sum stuck below yesterday's (a negative day-over-day
    "change"). Recomputing every affected day's contribution from the
    activity log itself, rather than from a potentially-stale prior write,
    makes this self-healing across that race instead of just avoiding it.

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

    # See _lock_for's docstring: this makes the read-modify-write below atomic
    # per statistic_id, so two overlapping calls for the same statistic_id
    # (e.g. from a coordinator listener firing more than once in quick
    # succession at startup) can't both read the same stale baseline and each
    # add their delta on top of it — a real, confirmed double-count bug.
    async with _lock_for(statistic_id):
        store = _applied_source_ids_store(hass, statistic_id)
        already_applied: set[str] = set(await store.async_load() or [])

        # Filter out any (source_id, value) pair already recorded as applied —
        # see the docstring above for why this de-dup can't rely solely on the
        # caller's in-memory tracking.
        deduped_by_day: dict[date, float] = {}
        newly_applied: list[str] = []
        for day, entries in deltas_by_day.items():
            day_total = 0.0
            for source_id, value in entries:
                if source_id in already_applied:
                    continue
                day_total += value
                newly_applied.append(source_id)
            if day_total:
                deduped_by_day[day] = day_total

        if not deduped_by_day:
            return

        earliest_day = min(deduped_by_day)
        today = dt_util.now().date()

        def _query_baseline() -> float:
            # The latest stored row STRICTLY BEFORE earliest_day, whatever
            # date it's actually stamped on — the live-write path only ever
            # writes a row on a day that had a genuine delta (unlike the
            # backfill, it does NOT guarantee a row for every single day, e.g.
            # a rest day), so a same-day-only or "yesterday only" lookup was a
            # real, repeatedly-hit bug: it silently fell back to 0.0 whenever
            # the most recent prior write wasn't exactly the day before,
            # discarding the entire running total built up by every earlier
            # write and every backfilled day. Same technique as
            # _rewrite_metric_series's _query_baseline, for the same reason.
            rows_needed = (today - earliest_day).days + 30
            rows = get_last_statistics(hass, rows_needed, statistic_id, False, {"sum"})
            series = rows.get(statistic_id, [])
            for row in series:
                if dt_util.utc_from_timestamp(row["start"]).date() < earliest_day:
                    return row["sum"] or 0.0
            return 0.0

        baseline = await get_instance(hass).async_add_executor_job(_query_baseline)

        # async_get_activities_in_range is itself async (reads a Store via
        # hass's own executor internally), so call it directly rather than
        # wrapping it in another executor job.
        activities = await async_get_activities_in_range(hass, entry_slug, earliest_day, today)
        activities_by_day: dict[date, list[Activity]] = {}
        for activity in activities:
            if activity.activity_type == activity_type:
                activities_by_day.setdefault(activity.start.date(), []).append(activity)

        metadata = _build_metric_metadata(statistic_id, activity_type, metric)
        statistic_data: list[StatisticData] = []
        running = baseline
        day = earliest_day
        while day <= today:
            # Each day's own contribution comes from the activity log itself
            # (see docstring for why this replaced diffing old stored sums) —
            # NOT from deltas_by_day, which only holds newly-applied source_ids
            # for days that happened to have one this call; a day already
            # fully reflected by a previous call still needs its real total
            # here so the whole [earliest_day, today] tail stays correct.
            running += _sum_metric(activities_by_day.get(day, []), metric)
            bucket_start = datetime.combine(
                day, datetime.min.time(), tzinfo=timezone.utc
            ).replace(hour=12)
            statistic_data.append(StatisticData(start=bucket_start, sum=running, state=running))
            day += timedelta(days=1)

        async_add_external_statistics(hass, metadata, statistic_data)
        # See _rewrite_metric_series for why this wait matters:
        # async_add_external_statistics only queues the write, it doesn't
        # block until it's committed. Still inside the lock: the next waiting
        # call must not start its own read until this write has actually
        # landed, or it would read the same stale baseline anyway.
        await get_instance(hass).async_block_till_done()

        # Record these source_ids as applied only now that the write has
        # actually landed — if this task were cancelled or HA crashed before
        # this point, the next attempt should still see these activities as
        # unapplied and retry them, rather than silently losing them.
        already_applied.update(newly_applied)
        await store.async_save(list(already_applied))


async def async_check_statistics_consistency(hass: HomeAssistant, entry_slug: str) -> None:
    """Periodic safety net: log a loud warning if any statistic for this entry
    is no longer monotonically non-decreasing.

    This is a detector, not a fixer — it never modifies data. The lock and
    persisted dedup ledger in async_apply_activity_deltas are the actual
    prevention for the race that caused a real double-counting bug (see that
    function's docstring); this exists in case some other, currently-unknown
    bug corrupts a series in the future. A monotonicity violation is a hard,
    unambiguous invariant violation for these series (see module docstring:
    sum must be genuinely cumulative-forever) — cheap to check (no source API
    calls, just a read of already-stored statistics) and catches corruption
    from a stray write, a manual "Adjust a statistic" mistake, or any other
    cause, without needing to know the specific mechanism in advance.

    Deliberately does NOT attempt to auto-correct: a wrong automatic "fix"
    based on incomplete information is how the original double-counting bug
    happened in the first place. A logged warning at least surfaces the
    problem promptly instead of leaving it to be discovered by chance when
    someone happens to look at a graph.
    """
    statistic_ids = {
        statistic_id_slug(entry_slug, activity_type, metric)
        for activity_type in ActivityType
        for metric in _metrics_for(activity_type)
    }

    def _check() -> list[str]:
        problems: list[str] = []
        for statistic_id in statistic_ids:
            rows = get_last_statistics(hass, 400, statistic_id, False, {"sum"})
            series = rows.get(statistic_id, [])
            # get_last_statistics returns newest-first.
            prev_sum: float | None = None
            for row in reversed(series):
                current = row["sum"] or 0.0
                if prev_sum is not None and current < prev_sum - 1e-6:
                    problems.append(
                        f"{statistic_id}: sum dropped from {prev_sum:.2f} to "
                        f"{current:.2f} at {dt_util.utc_from_timestamp(row['start']).date()}"
                    )
                prev_sum = current
        return problems

    problems = await get_instance(hass).async_add_executor_job(_check)
    for problem in problems:
        _LOGGER.warning(
            "Statistics consistency check found a non-monotonic series "
            "(likely corrupted data — consider using Developer Tools > "
            "Statistics > Adjust a statistic to fix it): %s",
            problem,
        )


def _sum_metric(activities: list[Activity], metric: str) -> float:
    if metric == "distance_km":
        return sum((a.distance_meters or 0) for a in activities) / 1000
    if metric == "duration_minutes":
        return sum((a.duration_seconds or 0) for a in activities) / 60
    if metric == "calories":
        return sum((a.calories or 0) for a in activities)
    return 0.0
