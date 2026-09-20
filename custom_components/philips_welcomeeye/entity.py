"""Shared base so every entity lands under one device."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .device import WelcomeEyeDevice


class WelcomeEyeEntity(Entity):
    """An entity belonging to one intercom."""

    _attr_has_entity_name = True

    def __init__(self, device: WelcomeEyeDevice, entry_id: str) -> None:
        self._device = device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.gid)},
            manufacturer="CFI Extel (Philips WelcomeEye)",
            model=str(device.info.get("model") or "WelcomeEye"),
            name="WelcomeEye",
            configuration_url=f"http://{device.host}",
        )
        self._entry_id = entry_id
