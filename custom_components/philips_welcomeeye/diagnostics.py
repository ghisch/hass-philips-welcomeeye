"""Downloadable diagnostics, with the credentials left out."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import WelcomeEyeConfigEntry

#: The GID is the RC4 key for the whole local protocol and the device hands it out
#: unauthenticated, but there is no reason to put it in a file people paste into
#: bug reports alongside their address.
REDACT = {CONF_PASSWORD, CONF_USERNAME, "gid"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WelcomeEyeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = entry.runtime_data
    device, coordinator = data.device, data.coordinator
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": dict(entry.options),
        },
        "device": {
            "model": (coordinator.data or {}).get("model"),
            "protocol_version": (coordinator.data or {}).get("protocol_version"),
            "version_json": (coordinator.data or {}).get("version_json"),
            "device_time": (coordinator.data or {}).get("device_time"),
            "punch_port": device.punch_port,
            "fixed_port": device.fixed_port,
            "channel": device.channel,
            "lock_channel": device.lock_channel,
            "stream_enabled": device.stream_enabled,
        },
        "stream": {
            "clients": device.stats.clients,
            "frames": device.stats.frames,
            "bytes_sent": device.stats.bytes_sent,
            "started_at": device.stats.started_at,
            "last_error": device.stats.last_error,
        },
        "last_unlock": device.last_unlock,
    }
