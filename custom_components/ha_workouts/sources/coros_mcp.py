"""Client for Coros's official OAuth2 + MCP program ("COROS MCP",
support.coros.com), used ALONGSIDE (not instead of) the unofficial Training
Hub API in coros.py.

Training Hub (coros.py) covers activities/splits — the reverse-engineered
API is stable, plain JSON, and everything this integration's core aggregate
sensors need. This module exists only for the handful of health/fitness
metrics that Training Hub simply does not expose at all (steps, sleep,
recovery, fitness assessment, training load) — confirmed absent from
Training Hub by direct probing, and confirmed present here the same way,
against a real account.

Two real, structural quirks worth knowing before touching this file:

1. Responses are free-form prose text, not structured JSON — e.g.
   'Recovery: 100%\nLevel: Heavy training allowed\nEstimated Full Recovery: 0h'
   inside a doubly-JSON-encoded string. There is no published schema for
   this text; the parsers below are regex/line-based against real captured
   output and must be treated as best-effort — a Coros wording change could
   silently break one without raising an error. Each parser returns None for
   fields it can't find rather than raising, so a wording drift degrules to
   a missing value, not a crash.

2. Registration is fully self-service (RFC 7591 Dynamic Client
   Registration: POST {issuer}/connect/register, no human review, returns a
   public client_id with no secret) and login is genuinely programmatic —
   despite this being "real OAuth2", the login step submits username/
   password directly to Coros's own login form during the authorize
   redirect chain (see CorosMcpClient.async_login), so no browser/external
   step is needed in the config flow, unlike a typical OAuth integration.
   This is NOT the same login/session as Training Hub (coros.py) — logging
   in here does not invalidate a Training Hub session or vice versa.

Ported from Coros's own reference implementation (coroslab/COROS-MCP's
skill/coros_mcp_login_gateway/scripts/coros_mcp_login.py — a synchronous,
stdlib-only CLI helper) to this integration's async aiohttp style. Verified
end-to-end against a real account before being wired into CorosSource:
login, DCR, token exchange, tools/list, and real calls to all six tools
this integration uses.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from ..sources.base import WorkoutSourceAuthError, WorkoutSourceError

_LOGGER = logging.getLogger(__name__)

_ISSUER = "https://mcp.coros.com"
_MCP_URL = "https://mcp.coros.com/mcp"
_SCOPES = "openid offline_access mcp.tools"
_CLIENT_NAME = "ha-workouts"
# A loopback redirect_uri is required by the authorize/token flow's own
# validation, but nothing ever actually listens on it — the whole flow
# (see async_login) never leaves server-to-server requests, so no callback
# is ever received or needs to be.
_REDIRECT_URI = "http://127.0.0.1:43123/callback"
_MCP_PROTOCOL_VERSION = "2025-06-18"


class CorosMcpAuthError(WorkoutSourceAuthError):
    """The MCP OAuth login/token flow failed."""


@dataclass(slots=True)
class McpTokenSet:
    """An OAuth token pair for the MCP API, plus the client_id it was issued
    under (needed again for a refresh — DCR clients have no fixed secret to
    re-derive it from)."""

    access_token: str
    refresh_token: str
    expires_at_epoch: float
    client_id: str

    def is_expired(self, skew_seconds: int = 60) -> bool:
        return time.time() + skew_seconds >= self.expires_at_epoch


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class CorosMcpClient:
    """Handles DCR + login + token refresh + JSON-RPC tool calls for Coros MCP.

    Stateless across restarts by design: callers persist McpTokenSet
    themselves (config entry data) and pass it back in; this class never
    caches a token on disk itself.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _register_client(self) -> str:
        """RFC 7591 Dynamic Client Registration — no approval, no secret
        (token_endpoint_auth_method: "none", a public client using PKCE
        instead). Cheap enough to call fresh each login rather than cache;
        Coros does not appear to rate-limit or deduplicate registrations.
        """
        async with self._session.post(
            f"{_ISSUER}/connect/register",
            json={
                "client_name": _CLIENT_NAME,
                "redirect_uris": [_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": _SCOPES,
                "token_endpoint_auth_method": "none",
            },
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise CorosMcpAuthError(f"Coros MCP client registration failed: {body[:500]}")
            payload = await resp.json()
        client_id = payload.get("client_id")
        if not client_id:
            raise CorosMcpAuthError("Coros MCP client registration response missing client_id")
        return client_id

    def _authorize_url(self, client_id: str, challenge: str, state: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "scope": _SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": _MCP_URL,
                "state": state,
            }
        )
        return f"{_ISSUER}/oauth2/authorize?{query}"

    async def async_login(self, email: str, password: str) -> McpTokenSet:
        """Log in with a Coros email/password, entirely server-to-server.

        Despite being "real" OAuth2 authorization-code + PKCE, no browser or
        external redirect is needed: this walks the same redirect chain a
        browser would (authorize -> Coros's own login form -> callback ->
        resume -> final redirect-with-code), submitting the password
        directly to Coros's login form at the point a human would otherwise
        see it, then exchanges the resulting code exactly as PKCE requires.
        Every hop is a plain HTTP request/response; aiohttp is told not to
        auto-follow redirects (allow_redirects=False) so each Location
        header can be inspected and driven manually.

        Uses a dedicated, short-lived ClientSession for this redirect chain
        instead of the shared HA session passed to __init__ — HA's shared
        session runs SSRF-blocking middleware that inspects every
        redirect's Location header and raises the moment it sees ANY
        loopback address, even one this code deliberately never follows
        (allow_redirects=False, we only ever read the header). _REDIRECT_URI
        being a loopback address is a legitimate, fixed constant of this
        OAuth flow (nothing ever actually listens on it — see that
        constant's own comment), not a real SSRF risk, but HA's guard can't
        tell the difference and there's no per-request way to opt out of
        it — confirmed by hitting exactly this error against a real
        account. A plain aiohttp.ClientSession here has no such guard.
        """
        client_id = await self._register_client()
        verifier = _pkce_verifier()
        challenge = _pkce_challenge(verifier)
        state = secrets.token_urlsafe(24)
        authorize_url = self._authorize_url(client_id, challenge, state)

        async with aiohttp.ClientSession() as login_session:
            async with login_session.get(authorize_url, allow_redirects=False) as resp:
                coros_login_url = resp.headers.get("Location")
            if not coros_login_url:
                raise CorosMcpAuthError("Coros MCP did not redirect to the Coros login form")

            login_form = self._login_form(coros_login_url, email, password)
            async with login_session.post(
                coros_login_url, data=login_form, allow_redirects=False
            ) as resp:
                callback_url = resp.headers.get("Location")
            if not callback_url:
                # Coros's login form redirects back with an error query
                # param on bad credentials rather than a non-redirect HTTP
                # status, so a missing Location here is the actual "wrong
                # email/password" signal — matches
                # CorosSource.async_authenticate's plain-401-ish handling
                # for the Training Hub login, just via a different
                # mechanism on Coros's side.
                raise WorkoutSourceAuthError("Invalid Coros email or password (MCP login)")

            async with login_session.get(callback_url, allow_redirects=False) as resp:
                resume_url = resp.headers.get("Location")
            if not resume_url:
                raise CorosMcpAuthError("Coros MCP callback did not resume authorization")

            async with login_session.get(resume_url, allow_redirects=False) as resp:
                final_redirect = resp.headers.get("Location")
            if not final_redirect:
                raise CorosMcpAuthError(
                    "Coros MCP authorization did not return a client callback"
                )

        code, returned_state = self._extract_code_and_state(final_redirect)
        if not code or returned_state != state:
            raise CorosMcpAuthError("Coros MCP authorization response missing code or state mismatch")

        return await self._exchange_code(client_id, code, verifier)

    def _login_form(self, coros_login_url: str, email: str, password: str) -> dict[str, str]:
        query = parse_qs(urlparse(coros_login_url).query)

        def _first(key: str, default: str = "") -> str:
            return query.get(key, [default])[0]

        return {
            "client_id": _first("client_id"),
            "redirect_uri": _first("redirect_uri"),
            "state": _first("state"),
            "scope": _first("scope"),
            "response_type": _first("response_type", "code"),
            "activityType": "",
            "language": "en",
            "country": "US",
            "userName": email,
            "password": password,
            "checkStatus": "1",
            "getAllHistoryIn24Hours": "0",
        }

    @staticmethod
    def _extract_code_and_state(redirect_url: str) -> tuple[str | None, str | None]:
        query = parse_qs(urlparse(redirect_url).query)
        return query.get("code", [None])[0], query.get("state", [None])[0]

    async def _exchange_code(self, client_id: str, code: str, verifier: str) -> McpTokenSet:
        async with self._session.post(
            f"{_ISSUER}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise CorosMcpAuthError(f"Coros MCP token exchange failed: {payload}")
        return self._token_set_from_response(payload, client_id)

    async def async_refresh(self, tokens: McpTokenSet) -> McpTokenSet:
        async with self._session.post(
            f"{_ISSUER}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": tokens.client_id,
                "refresh_token": tokens.refresh_token,
            },
        ) as resp:
            payload = await resp.json()
            if resp.status != 200:
                raise CorosMcpAuthError(f"Coros MCP token refresh failed: {payload}")
        return self._token_set_from_response(payload, tokens.client_id)

    @staticmethod
    def _token_set_from_response(payload: dict[str, Any], client_id: str) -> McpTokenSet:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not access_token or not refresh_token:
            raise CorosMcpAuthError("Coros MCP token response missing access_token/refresh_token")
        return McpTokenSet(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_epoch=time.time() + int(payload.get("expires_in", 3600)),
            client_id=client_id,
        )

    async def async_call_tool(
        self, tokens: McpTokenSet, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """Call one MCP tool and return its raw text response.

        Every call re-sends "initialize" first: the server is stateless
        (see module docstring's changelog note from earlier research) and
        does not accept a prior session being reused across separate HTTP
        requests, so there is no handshake worth caching across calls.
        """
        headers = {
            "Authorization": f"Bearer {tokens.access_token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
        }
        await self._mcp_request(
            headers,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": _CLIENT_NAME, "version": "1.0.0"},
                },
            },
        )
        response = await self._mcp_request(
            headers,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise WorkoutSourceError(f"Coros MCP {tool_name} response missing result")
        if result.get("isError"):
            raise WorkoutSourceError(f"Coros MCP {tool_name} returned an error: {result}")
        content = result.get("content") or []
        if not content or "text" not in content[0]:
            raise WorkoutSourceError(f"Coros MCP {tool_name} response missing text content")
        text = content[0]["text"]
        # Some tools double-encode: the text block is itself a JSON string
        # that decodes to the real prose (observed directly against a real
        # account — see module docstring). Unwrap it if so; otherwise the
        # text is already the real prose.
        if text.startswith('"') and text.endswith('"'):
            with contextlib.suppress(ValueError):
                text = json.loads(text)
        return text

    async def _mcp_request(
        self, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            async with self._session.post(_MCP_URL, headers=headers, json=body) as resp:
                if resp.status == 401:
                    raise WorkoutSourceAuthError("Coros MCP token rejected or expired")
                content_type = resp.headers.get("Content-Type", "")
                raw = await resp.text()
        except aiohttp.ClientError as err:
            raise WorkoutSourceError(f"Error communicating with Coros MCP: {err}") from err

        if "text/event-stream" in content_type:
            return _parse_sse_json(raw)
        return json.loads(raw) if raw else {}


def _parse_sse_json(raw: str) -> dict[str, Any]:
    """Parse a Streamable HTTP SSE body down to its final JSON-RPC payload.

    Coros's MCP server can answer either plain JSON or SSE framing for the
    same request (confirmed by two independent reference clients) — only
    the last `data:` event line matters; earlier ones, if any, are
    intermediate progress notifications this integration has no use for.
    """
    data_lines: list[str] = []
    events: list[str] = []
    for line in raw.splitlines():
        if not line:
            if data_lines:
                events.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if data_lines:
        events.append("\n".join(data_lines))
    if not events:
        return {}
    return json.loads(events[-1])
