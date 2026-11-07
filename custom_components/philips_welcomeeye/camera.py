"""The camera entity. One camera; the select chooses which source it shows."""
from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WelcomeEyeConfigEntry
from .entity import WelcomeEyeEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([WelcomeEyeCamera(entry.runtime_data.device, entry.entry_id)])


class WelcomeEyeCamera(WelcomeEyeEntity, Camera):
    """H.264 from the intercom, passed through untouched.

    The integration serves MPEG-TS on a loopback socket and hands Home Assistant a
    ``tcp://`` URL, so there is no go2rtc to configure and nothing is transcoded -
    the device's own H.264 and its own frame timestamps go straight through.

    Nothing runs until something asks for the stream, and nothing runs at all while
    the stream switch is off, because a stream held here takes the channel away
    from the indoor monitor.
    """

    _attr_translation_key = "camera"
    _attr_supported_features = CameraEntityFeature.STREAM | CameraEntityFeature.ON_OFF
    def __init__(self, device, entry_id: str) -> None:
        WelcomeEyeEntity.__init__(self, device, entry_id)
        Camera.__init__(self)
        self._attr_unique_id = f"{device.gid}_camera"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._device.on_channel_change = lambda: self.hass.add_job(self._async_restart_stream)

    async def async_will_remove_from_hass(self) -> None:
        self._device.on_channel_change = None
        await super().async_will_remove_from_hass()

    async def _async_restart_stream(self) -> None:
        """The source changed under a running stream: make the worker reconnect
        now rather than on its own back-off, which can be a minute."""
        if self.stream is None:
            return
        source = await self.stream_source()
        if source:
            try:
                self.stream.update_source(source)
            except Exception as err:  # noqa: BLE001 - best effort, worker retries anyway
                _LOGGER.debug("could not restart the stream: %s", err)
        self.async_write_ha_state()

    @property
    def use_stream_for_stills(self) -> bool:
        """Take stills from the stream rather than opening a second session.

        The device serves one channel at a time, so a separate snapshot session
        would fight the one already streaming.
        """
        return True

    @property
    def is_on(self) -> bool:
        return self._device.stream_enabled

    @property
    def is_streaming(self) -> bool:
        # a viewer connecting is not a stream; frames leaving is
        return self._device.stats.streaming

    @property
    def available(self) -> bool:
        return True

    async def stream_source(self) -> str | None:
        """Loopback MPEG-TS, or nothing while streaming is switched off."""
        return await self.hass.async_add_executor_job(self._device.stream_url)

    async def async_camera_image(self, width: int | None = None,
                                 height: int | None = None) -> bytes | None:
        if not self._device.stream_enabled:
            return None
        return await super().async_camera_image(width, height)

    async def async_turn_on(self) -> None:
        await self.hass.async_add_executor_job(self._device.set_stream_enabled, True)
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        await self.hass.async_add_executor_job(self._device.set_stream_enabled, False)
        self.async_write_ha_state()
