"""Tracks the all-time fastest split ever seen per named running distance
(1K/5K/10K/half marathon/marathon), persisted across restarts.

Why this exists rather than just reading Activity.fastest_split_5km_seconds
off the most recent activity: that field is the fastest CONTINUOUS split
WITHIN that one activity, not an all-time personal best — a real,
user-reported confusion (a 24:08 "fastest 5K" sensor value on a day whose
run otherwise averaged a much faster pace, while the user's genuine 5K PB
of 19:17 was sitting in an activity from weeks earlier). Garmin's own API
has no dedicated "personal records" endpoint (confirmed against its full
published OpenAPI spec — the closest thing, SummarizedActivityDTO.isPR, is
only a per-activity boolean, not a queryable best-ever value) — but since
every activity already carries its own fastest_split_* fields, the true
all-time best is just the minimum of that field across every activity ever
seen, which this module computes incrementally rather than rescanning full
history on every update.

Only ever shrinks (a "best" is a minimum) — never reset except by a full
source wipe (see statistics_import.async_wipe_source_data), matching the
non-decreasing nature of a personal record.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .models import Activity

_STORAGE_VERSION = 1

#: (persisted key, Activity field) for each distance Garmin's API reports a
#: fastest-continuous-split figure for.
DISTANCES: tuple[tuple[str, str], ...] = (
    ("1km", "fastest_split_1km_seconds"),
    ("5km", "fastest_split_5km_seconds"),
    ("10km", "fastest_split_10km_seconds"),
    ("half_marathon", "fastest_split_half_marathon_seconds"),
    ("marathon", "fastest_split_marathon_seconds"),
)


def _store(hass: HomeAssistant, entry_slug: str) -> Store[dict[str, float]]:
    return Store(hass, _STORAGE_VERSION, f"ha_workouts_personal_bests_{entry_slug}")


async def async_get_personal_bests(hass: HomeAssistant, entry_slug: str) -> dict[str, float]:
    """Return {distance_key: best_seconds} for whichever distances have a
    best recorded yet — a distance with no personal best seen so far is
    simply absent from the dict, not present with some placeholder value.
    """
    raw = await _store(hass, entry_slug).async_load()
    return raw or {}


async def async_update_personal_bests(
    hass: HomeAssistant, entry_slug: str, activities: list[Activity]
) -> dict[str, float]:
    """Fold any new bests from `activities` into the persisted record and
    save if anything improved. Returns the full, current best-per-distance
    dict (same shape as async_get_personal_bests) so callers don't need a
    separate read after writing.

    Safe to call repeatedly with overlapping/already-seen activities (e.g.
    every coordinator poll re-passes today's activity, and the backfill can
    re-walk a range) — folding in the same value twice is a no-op, since
    min(existing, same_value) == existing.
    """
    store = _store(hass, entry_slug)
    bests = await store.async_load() or {}
    changed = False

    for activity in activities:
        for key, field_name in DISTANCES:
            candidate = getattr(activity, field_name, None)
            if candidate is None:
                continue
            current = bests.get(key)
            if current is None or candidate < current:
                bests[key] = candidate
                changed = True

    if changed:
        await store.async_save(bests)
    return bests


async def async_wipe_personal_bests(hass: HomeAssistant, entry_slug: str) -> None:
    """Permanently delete the persisted personal-bests record — called
    alongside the rest of a source's data on a full wipe (see
    statistics_import.async_wipe_source_data's docstring for why entry_slug
    is a static per-source-type string that needs this explicit cleanup
    rather than naturally disappearing when a config entry is removed).
    """
    await _store(hass, entry_slug).async_remove()


_SCANNED_STORAGE_VERSION = 1


def _scanned_store(hass: HomeAssistant, entry_slug: str) -> Store[bool]:
    return Store(hass, _SCANNED_STORAGE_VERSION, f"ha_workouts_personal_bests_scanned_{entry_slug}")


async def async_has_scanned_history_for_bests(hass: HomeAssistant, entry_slug: str) -> bool:
    """Whether a full-history scan has ever completed for this source — see
    statistics_import.async_backfill_personal_bests, which is what performs
    that scan and then calls async_mark_history_scanned_for_bests below.

    Deliberately a SEPARATE flag from the statistics backfill's own
    "earliest known day"/"range fully covered" bookkeeping
    (statistics_import.py's _range_fully_covered /
    async_get_earliest_known_activity_day): those let the statistics
    backfill skip re-fetching activities once their *aggregated sums* are
    already covered, but personal bests need the actual per-activity
    fastest_split_* values, which that skip-path never even looks at. Using
    the SAME flag would mean an account whose statistics were already fully
    imported before this feature existed (a real case — exactly what
    happened in testing) would never get its history scanned for bests at
    all, silently stuck showing only whatever the live daily poll happens
    to see going forward.
    """
    raw = await _scanned_store(hass, entry_slug).async_load()
    return bool(raw)


async def async_mark_history_scanned_for_bests(hass: HomeAssistant, entry_slug: str) -> None:
    await _scanned_store(hass, entry_slug).async_save(True)


async def async_wipe_personal_bests_scanned_flag(hass: HomeAssistant, entry_slug: str) -> None:
    """Called alongside async_wipe_personal_bests on a full source wipe, so
    a subsequent re-add genuinely re-scans history rather than believing
    (from a flag that would otherwise have survived the wipe) that it
    already has, and permanently showing no personal bests at all.
    """
    await _scanned_store(hass, entry_slug).async_remove()
