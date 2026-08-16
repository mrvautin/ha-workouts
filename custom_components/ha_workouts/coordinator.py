"""DataUpdateCoordinator that polls a WorkoutSource on a fixed interval."""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN
from .models import WorkoutData
from .sources.base import WorkoutSource, WorkoutSourceAuthError, WorkoutSourceError
from .statistics_import import BackfillProgress

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
        # Held by both this coordinator's periodic poll and the background history
        # backfill task, so the two never send concurrent request streams to the
        # same (possibly rate-limit-sensitive, possibly unofficial) source API.
        self.request_lock = asyncio.Lock()

    async def _async_update_data(self) -> WorkoutData:
        async with self.request_lock:
            try:
                return await self.source.async_fetch(date.today())
            except WorkoutSourceAuthError as err:
                raise UpdateFailed(f"Authentication failed: {err}") from err
            except WorkoutSourceError as err:
                raise UpdateFailed(f"Error communicating with {self.source.key}: {err}") from err
