"""Catalog polling for CamPiX cameras."""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CATALOG_PATH, DOMAIN

_LOGGER = logging.getLogger(__name__)


class CamPiXCoordinator(DataUpdateCoordinator):
    """Refresh the authenticated camera catalog."""

    def __init__(self, hass, entry):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.entry = entry

    @property
    def base_url(self) -> str:
        data = self.entry.data
        scheme = "https" if data.get("tls") else "http"
        host = str(data["host"])
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{scheme}://{host}:{data['port']}"

    @property
    def token(self) -> str:
        return self.entry.data["token"]

    async def _async_update_data(self):
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{self.base_url}{CATALOG_PATH}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 401:
                    raise ConfigEntryAuthFailed
                if response.status != 200:
                    raise UpdateFailed("CamPiX catalog was not available")
                payload = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise UpdateFailed("CamPiX catalog was not available") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("cameras"), list):
            raise UpdateFailed("CamPiX catalog was not available")
        return payload
