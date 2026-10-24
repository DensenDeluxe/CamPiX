"""Discovery and one-time administrator pairing for CamPiX."""

from __future__ import annotations

import logging
import re

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME

from .const import DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_TLS = "tls"


class CannotConnect(Exception):
    pass


class InvalidAuth(Exception):
    pass


class PairFailed(Exception):
    pass


class TooManyTokens(Exception):
    pass


class AlreadyConfigured(Exception):
    pass


def _format_host(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 255:
        raise CannotConnect
    if any(ord(char) < 33 or char in " /\\?#@" for char in host):
        raise CannotConnect
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _property(properties, key):
    if not isinstance(properties, dict):
        return None
    value = properties.get(key)
    if value is None:
        value = properties.get(key.encode())
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{16}", value):
        return value
    return None


async def async_pair(host, port, tls, username, password, revoke_id=None, revoke_if=None):
    """Log in once, store a viewer token, and log out. The password is not returned.

    revoke_if(service_id) may delete the new token before logout when this CamPiX
    service is already configured. The token is then not returned.
    """
    base = f"{'https' if tls else 'http'}://{_format_host(host)}:{int(port)}"
    timeout = aiohttp.ClientTimeout(total=20)
    # Discovery fills in an IP address. aiohttp stores a cookie for an IP host
    # only when the jar opts in; otherwise the session never returns and pairing
    # stops after a correct password.
    async with aiohttp.ClientSession(
        timeout=timeout,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    ) as session:
        try:
            async with session.post(
                f"{base}/api/v1/auth/login",
                json={"username": username, "password": password},
            ) as response:
                if response.status == 401:
                    raise InvalidAuth
                if response.status != 200:
                    _LOGGER.warning("CamPiX login returned HTTP %s", response.status)
                    raise CannotConnect
                payload = await response.json(content_type=None)
                secure_cookie = _secure_session_cookie(response)
        except aiohttp.ClientError as exc:
            raise CannotConnect from exc
        if secure_cookie and not tls:
            _LOGGER.warning("CamPiX session cookie is Secure and the form is using HTTP")
            raise PairFailed
        csrf = payload.get("csrf") if isinstance(payload, dict) else None
        if not isinstance(csrf, str) or not csrf:
            raise CannotConnect
        headers = {"X-CSRF-Token": csrf}
        if revoke_id:
            try:
                async with session.delete(
                    f"{base}/api/v1/integrations/tokens/{revoke_id}",
                    headers=headers,
                ) as response:
                    if response.status not in {200, 404}:
                        raise CannotConnect
            except aiohttp.ClientError as exc:
                raise CannotConnect from exc
        try:
            async with session.post(
                f"{base}/api/v1/integrations/homeassistant/pair",
                json={"name": "Home Assistant"},
                headers=headers,
            ) as response:
                if response.status == 401:
                    _LOGGER.warning("CamPiX accepted the password but rejected the session cookie")
                    raise PairFailed
                if response.status == 409:
                    raise TooManyTokens
                if response.status != 200:
                    _LOGGER.warning("CamPiX pairing returned HTTP %s", response.status)
                    raise CannotConnect
                paired = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise CannotConnect from exc
        token = paired.get("token") if isinstance(paired, dict) else None
        token_id = paired.get("token_id") if isinstance(paired, dict) else None
        if (
            not isinstance(token, str)
            or not token.startswith("cx_")
            or not isinstance(token_id, str)
        ):
            raise CannotConnect
        try:
            async with session.get(
                f"{base}/api/v1/integrations/homeassistant",
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if response.status != 200:
                    raise CannotConnect
                catalog = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise CannotConnect from exc
        service_id = catalog.get("service_id") if isinstance(catalog, dict) else None
        if not isinstance(service_id, str) or not re.fullmatch(r"[a-f0-9]{16}", service_id):
            raise CannotConnect
        if revoke_if is not None and revoke_if(service_id):
            try:
                async with session.delete(
                    f"{base}/api/v1/integrations/tokens/{token_id}",
                    headers=headers,
                ) as response:
                    removed = response.status in {200, 404}
                    await response.read()
            except aiohttp.ClientError as exc:
                raise CannotConnect from exc
            await _logout(session, base, headers)
            if not removed:
                raise CannotConnect
            raise AlreadyConfigured
        await _logout(session, base, headers)
    return {"token": token, "token_id": token_id, "service_id": service_id}


def _secure_session_cookie(response) -> bool:
    cookie = response.cookies.get("campix_session")
    return bool(cookie and cookie.get("secure"))


async def _logout(session, base, headers):
    try:
        async with session.post(f"{base}/api/v1/auth/logout", headers=headers) as response:
            await response.read()
    except aiohttp.ClientError:
        return


class CamPiXConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Discover CamPiX and pair it with one administrator login."""

    VERSION = 1

    def __init__(self):
        self._discovered = None

    async def async_step_zeroconf(self, discovery_info):
        host = str(
            getattr(discovery_info, "host", None) or getattr(discovery_info, "ip_address", "")
        )
        port = int(getattr(discovery_info, "port", None) or DEFAULT_PORT)
        service_id = _property(getattr(discovery_info, "properties", {}) or {}, "id")
        self._discovered = {
            "host": host,
            "port": port,
            "tls": port == 443,
            "service_id": service_id,
        }
        if service_id:
            await self.async_set_unique_id(service_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        discovered = self._discovered or {}
        if user_input is not None:
            try:
                paired = await async_pair(
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    bool(user_input.get(CONF_TLS)),
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    revoke_if=self._already_configured,
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except PairFailed:
                errors["base"] = "pair_failed"
            except TooManyTokens:
                errors["base"] = "too_many_tokens"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except AlreadyConfigured:
                return self.async_abort(reason="already_configured")
            else:
                await self.async_set_unique_id(paired["service_id"])
                return self.async_create_entry(
                    title="CamPiX",
                    data={
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: int(user_input[CONF_PORT]),
                        CONF_TLS: bool(user_input.get(CONF_TLS)),
                        "token": paired["token"],
                        "token_id": paired["token_id"],
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=discovered.get("host", "")): str,
                    vol.Required(CONF_PORT, default=discovered.get("port", DEFAULT_PORT)): int,
                    vol.Required(CONF_TLS, default=discovered.get("tls", False)): bool,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                paired = await async_pair(
                    entry.data[CONF_HOST],
                    entry.data[CONF_PORT],
                    entry.data.get(CONF_TLS, False),
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    revoke_id=entry.data.get("token_id"),
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except PairFailed:
                errors["base"] = "pair_failed"
            except TooManyTokens:
                errors["base"] = "too_many_tokens"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        "token": paired["token"],
                        "token_id": paired["token_id"],
                    },
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    def _already_configured(self, service_id):
        return any(entry.unique_id == service_id for entry in self._async_current_entries())
