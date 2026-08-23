"""Config flow for ha_workouts. Supports Garmin Connect (email/password), Strava
(OAuth2 via Home Assistant's Application Credentials system), and Apple Health
(a generated webhook URL for an iOS Shortcut to POST workouts to).

A user can add any combination of Garmin, Strava, and Apple Health entries —
each is a separate config entry, so sensors from any source can be added
independently and coexist (their entity_ids are prefixed by source key, see
statistics_import.py).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import webhook
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BACKFILL_DAYS_OPTIONS,
    CONF_BACKFILL_DAYS,
    CONF_SOURCE_TYPE,
    CONF_SPLITS_BACKFILL_DAYS,
    CONF_WEBHOOK_ID,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_SPLITS_BACKFILL_DAYS,
    DOMAIN,
    SOURCE_APPLE_HEALTH,
    SOURCE_GARMIN,
    SOURCE_STRAVA,
    SPLITS_BACKFILL_DAYS_OPTIONS,
    STRAVA_OAUTH_SCOPES,
)
from .sources.base import WorkoutSourceAuthError, WorkoutSourceError
from .sources.garmin import GarminSource

_LOGGER = logging.getLogger(__name__)

STEP_GARMIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_SOURCE_PICKER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SOURCE_TYPE): vol.In(
            [SOURCE_GARMIN, SOURCE_STRAVA, SOURCE_APPLE_HEALTH]
        )
    }
)

_BACKFILL_SCHEMA = vol.Schema(
    {vol.Required(CONF_BACKFILL_DAYS, default="1 year"): vol.In(BACKFILL_DAYS_OPTIONS)}
)


class HaWorkoutsConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle a config flow for ha_workouts.

    Composed with AbstractOAuth2FlowHandler for the Strava path, but Garmin
    (plain email/password) bypasses OAuth machinery entirely via its own steps.
    """

    VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._email: str | None = None
        self._password: str | None = None
        self._pending_oauth_data: dict[str, Any] | None = None
        self._pending_title: str | None = None
        self._apple_health_webhook_id: str | None = None

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict:
        # force: Strava silently reuses a previous, possibly narrower grant for
        # this app + user if approval_prompt=auto and they've authorized before
        # (e.g. during earlier testing) — force always shows the consent screen
        # so the requested scopes are actually (re-)granted.
        return {"scope": STRAVA_OAUTH_SCOPES, "approval_prompt": "force"}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step: choose which source to add."""
        if user_input is not None:
            if user_input[CONF_SOURCE_TYPE] == SOURCE_GARMIN:
                return await self.async_step_garmin()
            if user_input[CONF_SOURCE_TYPE] == SOURCE_APPLE_HEALTH:
                return await self.async_step_apple_health()
            return await self.async_step_pick_implementation()

        return self.async_show_form(step_id="user", data_schema=_SOURCE_PICKER_SCHEMA)

    # --- Garmin: plain email/password form ---------------------------------

    async def async_step_garmin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(f"{SOURCE_GARMIN}_{email.lower()}")
            self._abort_if_unique_id_configured()

            source = GarminSource(self.hass, email, password)
            try:
                await source.async_authenticate()
            except WorkoutSourceAuthError:
                errors["base"] = "invalid_auth"
            except WorkoutSourceError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Garmin credentials")
                errors["base"] = "unknown"
            else:
                self._email = email
                self._password = password
                return await self.async_step_backfill()

        return self.async_show_form(
            step_id="garmin", data_schema=STEP_GARMIN_SCHEMA, errors=errors
        )

    # --- Apple Health: no auth, just generate & display a webhook URL ------

    async def async_step_apple_health(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Generate a webhook URL for the user to paste into their iOS Shortcut.

        No backfill step: Apple Health has no historical archive to pull from
        (see sources/apple_health.py) — only workouts pushed after setup ever
        appear, so there's no depth to choose.
        """
        if user_input is not None:
            return self.async_create_entry(
                title="Apple Health",
                data={
                    CONF_SOURCE_TYPE: SOURCE_APPLE_HEALTH,
                    CONF_WEBHOOK_ID: self._apple_health_webhook_id,
                },
            )

        if self._apple_health_webhook_id is None:
            self._apple_health_webhook_id = webhook.async_generate_id()

        webhook_url = webhook.async_generate_url(self.hass, self._apple_health_webhook_id)
        return self.async_show_form(
            step_id="apple_health",
            data_schema=vol.Schema({}),
            description_placeholders={
                "webhook_url": webhook_url,
                "toolbox_pro_url": "[Toolbox Pro](https://apps.apple.com/app/id1476205977)",
            },
        )

    # --- Strava: OAuth2, picks up after AbstractOAuth2FlowHandler's dance --

    async def async_step_pick_implementation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Override purely to inject description_placeholders.

        AbstractOAuth2FlowHandler's own version of this step (which this calls
        into via super()) renders the "pick_implementation" form without any
        placeholders — hassfest forbids literal URLs baked into strings.json,
        so the Strava setup link has to arrive as a placeholder instead. With
        only one Application Credentials implementation registered (the normal
        case here), the base class actually skips this form entirely and goes
        straight to async_step_auth — but hassfest validates strings.json
        statically regardless of whether the form is ever shown at runtime, so
        the placeholder still has to be supplied here.
        """
        result = await super().async_step_pick_implementation(user_input)
        if (
            result.get("type") == FlowResultType.FORM
            and result.get("step_id") == "pick_implementation"
        ):
            result["description_placeholders"] = {
                "strava_api_url": "[strava.com/settings/api](https://www.strava.com/settings/api)",
            }
        return result

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Called by AbstractOAuth2FlowHandler once the OAuth2 token exchange succeeds.

        Fetches the athlete profile directly (not via OAuth2Session, since no
        config entry exists yet for it to read/refresh the token against) purely
        to build a readable title and a stable unique_id.
        """
        _LOGGER.debug("Strava token exchange granted scope: %s", data["token"].get("scope"))

        try:
            http = async_get_clientsession(self.hass)
            resp = await http.get(
                "https://www.strava.com/api/v3/athlete",
                headers={"Authorization": f"Bearer {data['token']['access_token']}"},
            )
            async with resp:
                if resp.status == 403:
                    body = await resp.text()
                    _LOGGER.error(
                        "Strava returned 403 for /athlete right after token exchange. "
                        "Granted scope was %r. Response body: %s",
                        data["token"].get("scope"),
                        body,
                    )
                    # Since Strava's June 2026 developer program change, this is
                    # almost always an unsubscribed app owner account (status
                    # "Inactive"), not a scope problem — see oauth_scope_missing
                    # string for the full explanation shown to the user.
                    if '"Inactive"' in body:
                        _LOGGER.error(
                            "Strava app is Inactive — the account that owns this "
                            "API application needs an active Strava subscription"
                        )
                    return self.async_abort(reason="oauth_scope_missing")
                resp.raise_for_status()
                profile = await resp.json()
        except Exception:
            _LOGGER.exception("Failed to fetch Strava athlete profile after auth")
            return self.async_abort(reason="oauth_failed")

        athlete_id = profile.get("id")
        name = " ".join(filter(None, [profile.get("firstname"), profile.get("lastname")]))

        await self.async_set_unique_id(f"{SOURCE_STRAVA}_{athlete_id}")
        self._abort_if_unique_id_configured()

        self._pending_oauth_data = {**data, CONF_SOURCE_TYPE: SOURCE_STRAVA}
        self._pending_title = f"Strava ({name or athlete_id})"
        return await self.async_step_backfill()

    # --- Shared: history depth, then create the entry -----------------------

    async def async_step_backfill(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            backfill_days = BACKFILL_DAYS_OPTIONS[user_input[CONF_BACKFILL_DAYS]]
            if self._pending_oauth_data is not None:
                return self.async_create_entry(
                    title=self._pending_title,
                    data=self._pending_oauth_data,
                    options={CONF_BACKFILL_DAYS: backfill_days},
                )
            return self.async_create_entry(
                title=f"Garmin ({self._email})",
                data={
                    CONF_SOURCE_TYPE: SOURCE_GARMIN,
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                },
                options={CONF_BACKFILL_DAYS: backfill_days},
            )

        return self.async_show_form(step_id="backfill", data_schema=_BACKFILL_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HaWorkoutsOptionsFlow()


class HaWorkoutsOptionsFlow(OptionsFlow):
    """Configure options after setup.

    For Garmin/Strava: lets the user change how far back history is imported.
    Increasing the depth triggers the coordinator to fetch and import only the
    newly-uncovered older gap the next time it refreshes; it does not re-import
    days already covered by existing statistics. Garmin additionally gets a
    splits-backfill depth option (see activity_log.async_backfill_activity_splits)
    — Strava has no splits support yet, so it's Garmin-only.

    For Apple Health: re-displays the webhook URL (there's no backfill depth to
    configure — see sources/apple_health.py). This is the only way to see the
    URL again after initial setup, e.g. to set up the Shortcut on a second phone
    or after forgetting it — the config flow only shows it once, during setup.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self.config_entry.data.get(CONF_SOURCE_TYPE) == SOURCE_APPLE_HEALTH:
            return await self.async_step_apple_health_webhook()

        is_garmin = self.config_entry.data.get(CONF_SOURCE_TYPE) == SOURCE_GARMIN

        if user_input is not None:
            backfill_days = BACKFILL_DAYS_OPTIONS[user_input[CONF_BACKFILL_DAYS]]
            options = {CONF_BACKFILL_DAYS: backfill_days}
            if is_garmin:
                options[CONF_SPLITS_BACKFILL_DAYS] = SPLITS_BACKFILL_DAYS_OPTIONS[
                    user_input[CONF_SPLITS_BACKFILL_DAYS]
                ]
            return self.async_create_entry(data=options)

        current_days = self.config_entry.options.get(
            CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS
        )
        current_label = next(
            (label for label, days in BACKFILL_DAYS_OPTIONS.items() if days == current_days),
            "1 year",
        )
        schema_fields = {
            vol.Required(
                CONF_BACKFILL_DAYS, default=current_label
            ): vol.In(BACKFILL_DAYS_OPTIONS)
        }
        if is_garmin:
            current_splits_days = self.config_entry.options.get(
                CONF_SPLITS_BACKFILL_DAYS, DEFAULT_SPLITS_BACKFILL_DAYS
            )
            current_splits_label = next(
                (
                    label
                    for label, days in SPLITS_BACKFILL_DAYS_OPTIONS.items()
                    if days == current_splits_days
                ),
                "Off",
            )
            schema_fields[
                vol.Required(CONF_SPLITS_BACKFILL_DAYS, default=current_splits_label)
            ] = vol.In(SPLITS_BACKFILL_DAYS_OPTIONS)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_fields))

    async def async_step_apple_health_webhook(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Nothing to save — this step is purely informational.
            return self.async_create_entry(data={})

        webhook_id = self.config_entry.data[CONF_WEBHOOK_ID]
        webhook_url = webhook.async_generate_url(self.hass, webhook_id)
        return self.async_show_form(
            step_id="apple_health_webhook",
            data_schema=vol.Schema({}),
            description_placeholders={"webhook_url": webhook_url},
        )
