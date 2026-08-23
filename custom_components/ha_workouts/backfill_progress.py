"""Shared progress-tracking dataclass for background backfill jobs.

Deliberately its own module, not part of statistics_import.py or
activity_log.py: both of those modules need this type (statistics_import.py
for the main activity/statistics backfill, activity_log.py for the splits
backfill), and activity_log.py also imports helpers FROM statistics_import.py
(async_add_external_statistics-adjacent statistic_id_slug is unrelated, but
BackfillProgress specifically would create a cycle if it lived in either of
those two modules and the other imported it).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date


@dataclass
class BackfillProgress:
    """Live progress state for one config entry's backfill, read by a status sensor.

    Generic over which backfill it's tracking — used both for the main
    activity/statistics backfill (statistics_import.async_backfill_activity_statistics)
    and the opt-in splits backfill (activity_log.async_backfill_activity_splits).

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
