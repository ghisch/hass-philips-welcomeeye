"""Which camera the single camera entity shows."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WelcomeEyeConfigEntry
from .const import CHANNELS
from .entity import WelcomeEyeEntity


async def async_setup_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([ChannelSelect(entry.runtime_data.device, entry.entry_id)])


class ChannelSelect(WelcomeEyeEntity, SelectEntity):
    """Door station or second camera.

    A select rather than a button: there are two named sources and the current one
    matters, which a button cannot express. Switching drops any running stream so
    viewers reconnect to the new channel - the device will not serve both.
    """

    _attr_translation_key = "channel"
    _attr_icon = "mdi:camera-switch"
    _attr_options = CHANNELS

    def __init__(self, device, entry_id: str) -> None:
        super().__init__(device, entry_id)
        self._attr_unique_id = f"{device.gid}_channel"

    @property
    def current_option(self) -> str:
        return self._device.channel

    async def async_select_option(self, option: str) -> None:
        await self.hass.async_add_executor_job(self._device.set_channel, option)
        self.async_write_ha_state()
