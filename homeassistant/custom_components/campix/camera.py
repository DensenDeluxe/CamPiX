"""Camera entities created from the CamPiX catalog."""

from __future__ import annotations

import aiohttp
from aiohttp import web
from homeassistant.components.camera import Camera
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def _service_id(coordinator) -> str:
    data = coordinator.data if isinstance(coordinator.data, dict) else {}
    service_id = data.get("service_id")
    if isinstance(service_id, str) and service_id:
        return service_id
    return str(coordinator.entry.entry_id)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known = set()

    def _sync():
        cameras = coordinator.data.get("cameras") if isinstance(coordinator.data, dict) else []
        fresh = []
        for camera in cameras:
            if not isinstance(camera, dict):
                continue
            camera_id = camera.get("id")
            # Camera ids are unique on one CamPiX, and often repeated across services.
            if not isinstance(camera_id, str) or camera_id in known:
                continue
            known.add(camera_id)
            fresh.append(CamPiXCamera(coordinator, entry, camera_id, known))
        if fresh:
            async_add_entities(fresh)

    coordinator.async_add_listener(_sync)
    _sync()


class CamPiXCamera(Camera):
    """One CamPiX camera. Still images and MJPEG send the viewer token."""

    def __init__(self, coordinator, entry, camera_id, known):
        super().__init__()
        self.coordinator = coordinator
        self.entry = entry
        self._camera_id = camera_id
        self._attr_unique_id = f"campix:{_service_id(coordinator)}:{camera_id}"
        self._known = known
        self._attr_has_entity_name = False
        self._attr_should_poll = False
        current = self._camera()
        self._attr_name = current["name"] if current else "CamPiX"

    @property
    def available(self) -> bool:
        camera = self._camera()
        return bool(camera and camera.get("online"))

    @property
    def stream_source(self):
        # Home Assistant opens this URL itself and cannot attach the bearer token.
        return None

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}:{self._camera_id}")},
            name=self._attr_name,
            manufacturer="CamPiX",
            model="Camera",
            via_device=(DOMAIN, self.entry.entry_id),
        )

    async def async_added_to_hass(self):
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_coordinator))

    async def async_will_remove_from_hass(self):
        self._known.discard(self._camera_id)

    def _handle_coordinator(self):
        camera = self._camera()
        if camera is None:
            self.hass.async_create_task(self.async_remove())
            return
        self._attr_name = camera["name"]
        self.async_write_ha_state()

    def _camera(self):
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        for camera in data.get("cameras") or []:
            if isinstance(camera, dict) and camera.get("id") == self._camera_id:
                return camera
        return None

    async def async_camera_image(self, width=None, height=None):
        camera = self._camera()
        if not camera:
            return None
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{self.coordinator.base_url}{camera['still_path']}",
                headers={"Authorization": f"Bearer {self.coordinator.token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    return None
                return await response.read()
        except aiohttp.ClientError:
            return None

    async def handle_async_mjpeg_stream(self, request):
        camera = self._camera()
        if not camera:
            return None
        session = async_get_clientsession(self.hass)
        try:
            response = await session.get(
                f"{self.coordinator.base_url}{camera['mjpeg_path']}",
                headers={"Authorization": f"Bearer {self.coordinator.token}"},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30),
            )
        except aiohttp.ClientError:
            return None
        if response.status != 200:
            response.release()
            return None
        stream = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": response.headers.get(
                    "Content-Type", "multipart/x-mixed-replace; boundary=frame"
                )
            },
        )
        await stream.prepare(request)
        try:
            async for chunk in response.content.iter_chunked(8192):
                await stream.write(chunk)
        finally:
            response.release()
        return stream
