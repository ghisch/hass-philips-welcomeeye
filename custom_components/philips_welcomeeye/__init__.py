"""Philips WelcomeEye: local control of the intercom, without the vendor cloud."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

import logging
from dataclasses import dataclass

from .const import (CONF_AV_WAIT, CONF_GID, CONF_LAST_PORT, CONF_LOCK_CHANNEL,
                    CONF_PUNCH_PORT, DEFAULT_AV_WAIT, LOCK_CHANNEL_GATE)
from .coordinator import WelcomeEyeCoordinator
from .device import WelcomeEyeDevice, WelcomeEyeError


@dataclass
class WelcomeEyeData:
    """What the platforms need, hung off the config entry."""

    device: WelcomeEyeDevice
    coordinator: WelcomeEyeCoordinator
    #: the options this entry was set up with; a change here is a real reload,
    #: a change to entry.data (the remembered port) is not
    options: dict


type WelcomeEyeConfigEntry = ConfigEntry[WelcomeEyeData]

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry) -> bool:
    """Set up one intercom from its config entry."""
    options = entry.options
    device = WelcomeEyeDevice(
        host=entry.data[CONF_HOST],
        gid=entry.data[CONF_GID],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        punch_port=options.get(CONF_PUNCH_PORT),
        lock_channel=options.get(CONF_LOCK_CHANNEL, LOCK_CHANNEL_GATE),
        hint_port=entry.data.get(CONF_LAST_PORT),
    )
    device.av_wait = options.get(CONF_AV_WAIT, DEFAULT_AV_WAIT)

    @callback
    def _store_port(port: int) -> None:
        if port != entry.data.get(CONF_LAST_PORT):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_LAST_PORT: port})

    def _port_changed(port: int) -> None:
        # called from a worker thread, so hop back onto the loop
        hass.add_job(_store_port, port)

    device.on_port_change = _port_changed

    try:
        await hass.async_add_executor_job(device.probe)
    except WelcomeEyeError as err:
        raise ConfigEntryNotReady(f"cannot reach the intercom: {err}") from err

    coordinator = WelcomeEyeCoordinator(hass, device)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = WelcomeEyeData(device=device, coordinator=coordinator,
                                        options=dict(entry.options))
    device.on_session_failure = lambda: hass.add_job(coordinator.async_request_refresh)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry) -> bool:
    """Tear down, making sure the device is released."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await hass.async_add_executor_job(entry.runtime_data.device.stop_stream_server)
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: WelcomeEyeConfigEntry) -> None:
    """Reload when the options change. Saving the remembered port also updates the
    entry, and reloading for that tore the stream server down on every new port -
    which is what looked like the integration crashing."""
    data = entry.runtime_data
    if data is not None and dict(entry.options) == data.options:
        _LOGGER.debug("entry data updated (remembered port); not reloading")
        return
    await hass.config_entries.async_reload(entry.entry_id)
