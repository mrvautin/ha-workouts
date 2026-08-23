"""DataUpdateCoordinator that polls a WorkoutSource on a fixed interval."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .activity_log import async_record_activities
from .const import CONSISTENCY_CHECK_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .models import WorkoutData
from .sources.base import WorkoutSource, WorkoutSourceAuthError, WorkoutSourceError
from .statistics_import import BackfillProgress, async_check_statistics_consistency

_LOGGER = logging.getLogger(__name__)


class WorkoutDataUpdateCoordinator(DataUpdateCoordinator[WorkoutData]):
    """Fetches today's activity and summary data from a single configured source."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, source: WorkoutSource) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{source.key}",
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self.entry = entry
        self.source = source
        self.backfill_progress = BackfillProgress()
        # Separate from backfill_progress above: tracks the opt-in historical
        # splits backfill (see activity_log.async_backfill_activity_splits),
        # which runs independently and on a much slower cadence.
        self.splits_backfill_progress = BackfillProgress()
        # Held by both this coordinator's periodic poll and the background history
        # backfill task, so the two never send concurrent request streams to the
        # same (possibly rate-limit-sensitive, possibly unofficial) source API.
        self.request_lock = asyncio.Lock()
        # When data last actually arrived from the source: set at the end of
        # every successful _async_update_data call. For Garmin/Strava that's a
        # real timed poll; for Apple Health (which never polls on a schedule)
        # it's whenever __init__.py's webhook handler calls
        # coordinator.async_request_refresh() after accepting a workout, which
        # still routes through this same _async_update_data method. Not HA's
        # own last_update_success — that's a bool, not a timestamp, and this
        # HA version's DataUpdateCoordinator doesn't track one itself.
        self.last_data_update: datetime | None = None
        # Piggybacks on the regular poll rather than running its own timer —
        # see statistics_import.async_check_statistics_consistency. Starts due
        # immediately so a fresh install/reload gets one early check rather
        # than waiting a full CONSISTENCY_CHECK_INTERVAL for the first one.
        self._next_consistency_check = dt_util.now()

    @callback
    def async_mark_updated_now(self) -> None:
        self.last_data_update = dt_util.now()

    async def _async_update_data(self) -> WorkoutData:
        async with self.request_lock:
            try:
                data = await self.source.async_fetch(dt_util.now().date())
            except WorkoutSourceAuthError as err:
                raise UpdateFailed(f"Authentication failed: {err}") from err
            except WorkoutSourceError as err:
                raise UpdateFailed(f"Error communicating with {self.source.key}: {err}") from err
            self.async_mark_updated_now()

        # Outside the request_lock/try block above: neither of these makes a
        # source API call (activity_log only writes to local storage; the
        # consistency check only reads already-stored statistics), so neither
        # needs the lock that serializes actual source requests, and a
        # failure in either shouldn't be treated as a fetch failure.
        if data.activities:
            await async_record_activities(self.hass, self.source.key, data.activities)

        now = dt_util.now()
        if now >= self._next_consistency_check:
            self._next_consistency_check = now + CONSISTENCY_CHECK_INTERVAL
            await async_check_statistics_consistency(self.hass, self.source.key)

        return data
