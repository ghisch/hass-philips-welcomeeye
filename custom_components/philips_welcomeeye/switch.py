"""Switch controlling whether the camera stream may run at all."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WelcomeEyeConfigEntry
from .entity import WelcomeEyeEntity


async def async_setup_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([StreamSwitch(entry.runtime_data.device, entry.entry_id)])


class StreamSwitch(WelcomeEyeEntity, SwitchEntity):
    """Master enable for streaming.

    The device encodes one channel at a time and whoever got there first keeps it,
    so a stream held open by Home Assistant blocks the indoor monitor. Off by
    default, and turning it off tears any running session down immediately.
    """

    _attr_translation_key = "stream"
    _attr_icon = "mdi:video"

    def __init__(self, device, entry_id: str) -> None:
        super().__init__(device, entry_id)
        self._attr_unique_id = f"{device.gid}_stream_enabled"

    @property
    def is_on(self) -> bool:
        return self._device.stream_enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.set_stream_enabled, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.set_stream_enabled, False)
        self.async_write_ha_state()
