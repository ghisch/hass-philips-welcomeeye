"""Periodic refresh of the device's diagnostic data."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .device import WelcomeEyeDevice, WelcomeEyeError

_LOGGER = logging.getLogger(__name__)

#: A probe is a login only - it never starts A/V, so it does not take the channel
#: away from the indoor monitor. Still no reason to do it often.
UPDATE_INTERVAL = timedelta(minutes=5)


class WelcomeEyeCoordinator(DataUpdateCoordinator[dict]):
    """Logs in occasionally and reports what the device says about itself."""

    def __init__(self, hass: HomeAssistant, device: WelcomeEyeDevice) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.device = device

    async def _async_update_data(self) -> dict:
        try:
            return await self.hass.async_add_executor_job(self.device.probe)
        except WelcomeEyeError as err:
            raise UpdateFailed(str(err)) from err
