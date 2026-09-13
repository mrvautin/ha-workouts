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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_COROS_MCP_ACCESS_TOKEN,
    CONF_COROS_MCP_CLIENT_ID,
    CONF_COROS_MCP_EXPIRES_AT,
    CONF_COROS_MCP_REFRESH_TOKEN,
    CONF_COROS_REGION,
    CONF_SOURCE_TYPE,
    CONF_WEBHOOK_ID,
    DEFAULT_COROS_REGION,
    DOMAIN,
    SOURCE_APPLE_HEALTH,
    SOURCE_COROS,
    SOURCE_GARMIN,
    SOURCE_STRAVA,
)
from .coordinator import WorkoutDataUpdateCoordinator
from .sources.apple_health import AppleHealthSource
from .sources.base import WorkoutSource
from .sources.coros import CorosSource
from .sources.coros_mcp import McpTokenSet
from .sources.garmin import GarminSource
from .sources.strava import StravaSource
from .statistics_import import async_wipe_source_data

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
    if source_type == SOURCE_COROS:
        return CorosSource(
            hass,
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
            entry.data.get(CONF_COROS_REGION, DEFAULT_COROS_REGION),
            mcp_tokens=_mcp_tokens_from_entry(entry),
            mcp_token_update_callback=lambda tokens: _async_persist_mcp_tokens(
                hass, entry, tokens
            ),
        )
    raise ValueError(f"Unknown source type: {source_type}")


def _mcp_tokens_from_entry(entry: ConfigEntry) -> McpTokenSet | None:
    """Build an McpTokenSet from config entry data, or None if the user
    never connected Coros MCP (see const.py's CONF_COROS_MCP_* docstring)."""
    access_token = entry.data.get(CONF_COROS_MCP_ACCESS_TOKEN)
    if access_token is None:
        return None
    return McpTokenSet(
        access_token=access_token,
        refresh_token=entry.data[CONF_COROS_MCP_REFRESH_TOKEN],
        expires_at_epoch=entry.data[CONF_COROS_MCP_EXPIRES_AT],
        client_id=entry.data[CONF_COROS_MCP_CLIENT_ID],
    )


@callback
def _async_persist_mcp_tokens(hass: HomeAssistant, entry: ConfigEntry, tokens: McpTokenSet) -> None:
    """Called by CorosSource whenever it refreshes its MCP token — Coros's
    refresh tokens are one-time-use (confirmed by earlier research on this
    OAuth program), so the new pair must be saved immediately or a restart
    between refreshes would retry an already-dead refresh_token and
    permanently lock the user out of MCP until they reconnect it.
    """
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_COROS_MCP_CLIENT_ID: tokens.client_id,
            CONF_COROS_MCP_ACCESS_TOKEN: tokens.access_token,
            CONF_COROS_MCP_REFRESH_TOKEN: tokens.refresh_token,
            CONF_COROS_MCP_EXPIRES_AT: tokens.expires_at_epoch,
        },
    )


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
    # Captured BEFORE unloading platforms (which is what actually purges
    # these from the entity registry) — async_remove_entry below needs them
    # to clear each sensor's auto-compiled statistics on a real delete, but
    # by the time that hook runs the registry has already forgotten this
    # entry's entities entirely (confirmed empirically: querying the
    # registry from inside async_remove_entry finds nothing). Stashed on
    # hass.data under a dedicated key, separate from the coordinator map,
    # so it survives past the pop() below.
    entity_ids = [
        entity.entity_id
        for entity in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    ]
    hass.data.setdefault(f"{DOMAIN}_entity_ids", {})[entry.entry_id] = entity_ids

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wipe this source's imported data once its LAST config entry is deleted.

    entry_slug (see statistics_import.py/sensor.py) is a static per-source-
    type string — e.g. every Coros entry, regardless of account, shares the
    same "coros" slug — not tied to any one config entry's id. So the
    activity log, statistics, and backfill-progress caches all already
    outlive a single entry's delete+re-add today, for every source, not just
    Coros: without this, "remove and re-add to start fresh" silently doesn't,
    since a plain re-add finds the old data still there and treats it as
    already imported (see async_wipe_source_data's docstring for the exact
    Coros bug that surfaced this).

    Only wipes when no OTHER entry of the same source type remains — those
    entries currently share this same data (a separate, pre-existing
    limitation: multiple accounts of one source type all collide on the same
    slug), so removing one while a sibling is still configured must not
    delete data the sibling is still using.
    """
    entity_ids = hass.data.get(f"{DOMAIN}_entity_ids", {}).pop(entry.entry_id, [])
    source_type = entry.data.get(CONF_SOURCE_TYPE)
    if source_type is None:
        return
    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.data.get(CONF_SOURCE_TYPE) == source_type
    ]
    if remaining:
        return
    await async_wipe_source_data(hass, source_type, entity_ids)
