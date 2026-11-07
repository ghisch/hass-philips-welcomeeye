"""Periodic refresh of the device's diagnostic data."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .device import WelcomeEyeDevice, WelcomeEyeError

_LOGGER = logging.getLogger(__name__)

#: Every probe is a discovery and a login, and every discovery costs the device a
#: port allocation. Rarely is right. While a stream is running there is nothing to
#: learn that the stream is not already proving, so the poll skips itself then.
UPDATE_INTERVAL = timedelta(minutes=15)


class WelcomeEyeCoordinator(DataUpdateCoordinator[dict]):
    """Logs in occasionally and reports what the device says about itself."""

    def __init__(self, hass: HomeAssistant, device: WelcomeEyeDevice) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.device = device

    async def _async_update_data(self) -> dict:
        if self.device.stats.streaming and self.data:
            return self.data
        try:
            return await self.hass.async_add_executor_job(self.device.probe)
        except WelcomeEyeError as err:
            raise UpdateFailed(str(err)) from err
