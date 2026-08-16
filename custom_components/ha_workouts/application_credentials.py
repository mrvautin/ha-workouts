"""Application Credentials platform for ha_workouts: Strava's OAuth2 endpoints.

Each user registers their own Strava API app at strava.com/settings/api and
enters its Client ID/Secret via Settings -> Application Credentials before
adding the Strava source (see README for step-by-step setup instructions).
No credentials are bundled with this integration.
"""
from __future__ import annotations

from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(
        authorize_url="https://www.strava.com/oauth/authorize",
        token_url="https://www.strava.com/oauth/token",
    )
