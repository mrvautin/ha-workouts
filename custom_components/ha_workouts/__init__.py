"""The ha_workouts integration: pulls workout/health data from Garmin, Strava,
and Apple Health.

A user may add a Garmin entry, a Strava entry, an Apple Health entry, or any
combination, as separate config entries; each gets its own coordinator and its
own source-prefixed sensors (see sensor.py / statistics_import.py for the
naming scheme that keeps them chartable side by side).
"""
from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    CONF_SOURCE_TYPE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    SOURCE_APPLE_HEALTH,
    SOURCE_GARMIN,
    SOURCE_STRAVA,
)
from .coordinator import WorkoutDataUpdateCoordinator
from .sources.apple_health import AppleHealthSource
from .sources.base import WorkoutSource
from .sources.garmin import GarminSource
from .sources.strava import StravaSource

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR]


async def _build_source(hass: HomeAssistant, entry: ConfigEntry) -> WorkoutSource:
    source_type = entry.data[CONF_SOURCE_TYPE]
    if source_type == SOURCE_GARMIN:
        return GarminSource(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    if source_type == SOURCE_STRAVA:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
        session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        return StravaSource(session)
    if source_type == SOURCE_APPLE_HEALTH:
        return AppleHealthSource()
    raise ValueError(f"Unknown source type: {source_type}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    source = await _build_source(hass, entry)
    coordinator = WorkoutDataUpdateCoordinator(hass, entry, source)

    if isinstance(source, AppleHealthSource):
        _register_apple_health_webhook(hass, entry, source, coordinator)
        # Nothing to fetch on setup — data only ever arrives via webhook push,
        # so there is no "first refresh" to wait on the way Garmin/Strava do.
    else:
        await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _register_apple_health_webhook(
    hass: HomeAssistant,
    entry: ConfigEntry,
    source: AppleHealthSource,
    coordinator: WorkoutDataUpdateCoordinator,
) -> None:
    webhook_id = entry.data[CONF_WEBHOOK_ID]

    async def _handle_webhook(
        hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            _LOGGER.warning("Apple Health webhook received non-JSON body")
            return web.Response(status=400, text="Invalid JSON")

        accepted = source.ingest_webhook_payload(payload)
        if accepted:
            # Reflect the new workout in sensor state immediately rather than
            # waiting for the next 15-minute poll — the whole point of a push
            # source is that updates should show up close to real-time.
            await coordinator.async_request_refresh()
        return web.Response(status=200, text="OK" if accepted else "Ignored (duplicate)")

    webhook.async_register(
        hass, DOMAIN, entry.title, webhook_id, _handle_webhook, local_only=False
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (e.g. backfill depth), so the new
    depth is picked up and any newly-uncovered older history gets imported."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
