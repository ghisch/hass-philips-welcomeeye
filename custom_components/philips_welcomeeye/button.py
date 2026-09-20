"""The gate button. Pressing it physically opens the gate."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WelcomeEyeConfigEntry
from .device import WelcomeEyeError
from .entity import WelcomeEyeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([GateButton(entry.runtime_data.device, entry.entry_id)])


class GateButton(WelcomeEyeEntity, ButtonEntity):
    """Pulses the gate relay.

    The device has no relay of its own - it pulses an external gate controller,
    which toggles. There is no gate state to read back, so this is a button and
    not a cover: an optimistic open/closed entity would drift out of sync.
    """

    _attr_translation_key = "gate"
    _attr_icon = "mdi:gate"

    def __init__(self, device, entry_id: str) -> None:
        super().__init__(device, entry_id)
        self._attr_unique_id = f"{device.gid}_gate"

    async def async_press(self) -> None:
        """Open the gate.

        The device answers result=1 whether or not the relay fired, so the device
        layer refuses to send unless door video is actually flowing. A failure here
        is a real failure and is surfaced rather than swallowed.
        """
        try:
            result = await self.hass.async_add_executor_job(self._device.unlock)
        except WelcomeEyeError as err:
            raise HomeAssistantError(f"could not open the gate: {err}") from err
        if not result.get("acknowledged"):
            raise HomeAssistantError(
                "the gate command was sent but the device never acknowledged it")
        self.async_write_ha_state()
