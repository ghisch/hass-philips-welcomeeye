"""Diagnostic sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (SensorDeviceClass, SensorEntity,
                                             SensorEntityDescription, SensorStateClass)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WelcomeEyeConfigEntry, WelcomeEyeData
from .const import DOMAIN
from .entity import WelcomeEyeEntity


@dataclass(frozen=True, kw_only=True)
class WelcomeEyeSensorDescription(SensorEntityDescription):
    """A diagnostic reading, taken from the device or from stream state."""

    value: Callable[[WelcomeEyeData], Any]


def _clock_skew(data: WelcomeEyeData) -> float | None:
    device_time = (data.coordinator.data or {}).get("device_time")
    if not device_time:
        return None
    return round(device_time - datetime.now(tz=timezone.utc).timestamp(), 1)


SENSORS: tuple[WelcomeEyeSensorDescription, ...] = (
    WelcomeEyeSensorDescription(
        key="clock_skew",
        translation_key="clock_skew",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_clock_skew,
    ),
    WelcomeEyeSensorDescription(
        key="punch_port",
        translation_key="punch_port",
        entity_category=EntityCategory.DIAGNOSTIC,
        # randomised by the device and rotates often, so worth being able to see
        value=lambda d: d.device.punch_port,
    ),
    WelcomeEyeSensorDescription(
        key="model",
        translation_key="model",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (d.coordinator.data or {}).get("model"),
    ),
    WelcomeEyeSensorDescription(
        key="stream_clients",
        translation_key="stream_clients",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.device.stats.clients,
    ),
    WelcomeEyeSensorDescription(
        key="stream_frames",
        translation_key="stream_frames",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.device.stats.frames,
    ),
    WelcomeEyeSensorDescription(
        key="last_gate_open",
        translation_key="last_gate_open",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (
            datetime.fromtimestamp(d.device.last_unlock["at"], tz=timezone.utc)
            if d.device.last_unlock else None),
    ),
    WelcomeEyeSensorDescription(
        key="last_stream_error",
        translation_key="last_stream_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (d.device.stats.last_error or "none")[:255],
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: WelcomeEyeConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data = entry.runtime_data
    async_add_entities(
        WelcomeEyeSensor(data, entry.entry_id, description) for description in SENSORS
    )


class WelcomeEyeSensor(CoordinatorEntity, WelcomeEyeEntity, SensorEntity):
    """One diagnostic reading."""

    entity_description: WelcomeEyeSensorDescription

    def __init__(self, data: WelcomeEyeData, entry_id: str,
                 description: WelcomeEyeSensorDescription) -> None:
        CoordinatorEntity.__init__(self, data.coordinator)
        WelcomeEyeEntity.__init__(self, data.device, entry_id)
        self.entity_description = description
        self._data = data
        self._attr_unique_id = f"{data.device.gid}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self._data)
