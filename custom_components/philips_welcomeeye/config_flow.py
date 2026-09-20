"""Config flow: find a WelcomeEye on the network, or be told where it is."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (NumberSelector, NumberSelectorConfig,
                                            NumberSelectorMode)

from .const import (CONF_AV_WAIT, CONF_GID, CONF_LOCK_CHANNEL, CONF_PASSWORD_IS_DERIVED,
                    CONF_PUNCH_PORT, DEFAULT_AV_WAIT, DEFAULT_USERNAME, DOMAIN,
                    LOCK_CHANNEL_GATE)
from .device import WelcomeEyeDevice, WelcomeEyeError
from .glnklib.protocol import device_password, discover

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


class WelcomeEyeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one intercom."""

    VERSION = 1

    def __init__(self) -> None:
        self._found: dict[str, dict[str, Any]] = {}
        self._host: str | None = None
        self._gid: str | None = None

    # ------------------------------------------------------------------ steps
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer whatever answered a broadcast probe, plus a manual option."""
        if user_input is not None:
            choice = user_input["device"]
            if choice == MANUAL:
                return await self.async_step_manual()
            found = self._found[choice]
            self._host, self._gid = found["ip"], found["gid"]
            await self.async_set_unique_id(self._gid)
            self._abort_if_unique_id_configured()
            return await self.async_step_credentials()

        devices = await self.hass.async_add_executor_job(discover)
        self._found = {d["gid"]: d for d in devices if d.get("gid")}
        if not self._found:
            return await self.async_step_manual()

        choices = {
            gid: f"{gid} ({d.get('ip') or 'unknown address'})"
            for gid, d in self._found.items()
        }
        choices[MANUAL] = "Enter the address manually"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("device"): vol.In(choices)}),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the address and device id directly.

        Discovery is a UDP broadcast, so it does not survive a routed or VPN path
        even when the device is perfectly reachable.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._gid = user_input[CONF_GID].strip()
            if not self._gid:
                errors[CONF_GID] = "required"
            else:
                await self.async_set_unique_id(self._gid)
                self._abort_if_unique_id_configured()
                return await self.async_step_credentials()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=(self._host or "")): str,
                vol.Required(CONF_GID, default=(self._gid or "")): str,
            }),
            errors=errors,
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Credentials, then prove them by logging in."""
        errors: dict[str, str] = {}
        if user_input is not None:
            derived = (user_input[CONF_PASSWORD] if user_input[CONF_PASSWORD_IS_DERIVED]
                       else device_password(user_input[CONF_PASSWORD]))
            device = WelcomeEyeDevice(
                host=self._host, gid=self._gid,
                username=user_input[CONF_USERNAME], password=derived)
            try:
                info = await self.hass.async_add_executor_job(device.probe)
            except WelcomeEyeError as err:
                _LOGGER.debug("setup probe failed: %s", err)
                errors["base"] = ("invalid_auth" if "login rejected" in str(err)
                                  else "cannot_connect")
            else:
                return self.async_create_entry(
                    title=f"WelcomeEye {info.get('model') or self._gid}",
                    data={
                        CONF_HOST: self._host,
                        CONF_GID: self._gid,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        # stored already derived: it is the only form the device
                        # ever sees, so the plaintext is not worth keeping
                        CONF_PASSWORD: derived,
                    },
                )

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_PASSWORD_IS_DERIVED, default=False): bool,
            }),
            errors=errors,
            description_placeholders={"host": self._host or "", "gid": self._gid or ""},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return WelcomeEyeOptionsFlow()


class WelcomeEyeOptionsFlow(OptionsFlow):
    """Things that vary by installation rather than by device."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            port = user_input.get(CONF_PUNCH_PORT) or 0
            return self.async_create_entry(data={
                CONF_LOCK_CHANNEL: int(user_input[CONF_LOCK_CHANNEL]),
                CONF_AV_WAIT: float(user_input[CONF_AV_WAIT]),
                CONF_PUNCH_PORT: int(port) or None,
            })

        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_LOCK_CHANNEL,
                             default=opts.get(CONF_LOCK_CHANNEL, LOCK_CHANNEL_GATE)):
                    NumberSelector(NumberSelectorConfig(
                        min=0, max=7, step=1, mode=NumberSelectorMode.BOX)),
                vol.Required(CONF_AV_WAIT,
                             default=opts.get(CONF_AV_WAIT, DEFAULT_AV_WAIT)):
                    NumberSelector(NumberSelectorConfig(
                        min=1, max=60, step=0.5, mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s")),
                vol.Optional(CONF_PUNCH_PORT,
                             default=opts.get(CONF_PUNCH_PORT) or 0):
                    NumberSelector(NumberSelectorConfig(
                        min=0, max=65535, step=1, mode=NumberSelectorMode.BOX)),
            }),
        )
