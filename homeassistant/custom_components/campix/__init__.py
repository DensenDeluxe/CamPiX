"""CamPiX hub: one config entry, one camera entity per catalog camera."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import CamPiXCoordinator

PLATFORMS = ["camera"]


async def async_setup_entry(hass, entry):
    coordinator = CamPiXCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=coordinator.data.get("service") or "CamPiX",
        manufacturer="CamPiX",
        sw_version=str(coordinator.data.get("version") or ""),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
