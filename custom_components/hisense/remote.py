"""Hisense TV Remote platform."""
from __future__ import annotations

import logging
from homeassistant.components.remote import RemoteEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HisenseEntity

_LOGGER = logging.getLogger(__name__)

KEY_MAP = {
    "power": "KEY_POWER",
    "home": "KEY_HOME",
    "back": "KEY_RETURNS",
    "menu": "KEY_MENU",
    "ok": "KEY_OK",
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "volume_up": "KEY_VOLUMEUP",
    "volume_down": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE",
    "control_center": "KEY_CONTROL_CENTER",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hisense TV remote from config entry."""
    coordinators = hass.data[DOMAIN].get(config_entry.entry_id, {})
    tv_coordinators = [
        c for c in coordinators.values()
        if getattr(c, "device_type", None) == "电视"
    ]
    if tv_coordinators:
        async_add_entities([HisenseTVRemote(c) for c in tv_coordinators])


class HisenseTVRemote(HisenseEntity, RemoteEntity):
    """Representation of a Hisense TV remote."""

    _attr_has_entity_name = True
    _attr_name = "遥控器"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "tv_remote", "tv_remote", "mdi:remote")

    @property
    def is_on(self) -> bool:
        """Return True if TV is powered on."""
        return bool(self.status.get("power_on", False))

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on the TV."""
        await self.client.turn_on()
        self.coordinator.async_update_from_client()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off the TV."""
        await self.client.turn_off()
        self.coordinator.async_update_from_client()

    async def async_send_command(self, command: list[str], **kwargs) -> None:
        """Send key commands to the TV."""
        for item in command:
            cmd = str(item).strip()
            key = cmd.upper() if cmd.upper().startswith("KEY_") else KEY_MAP.get(cmd.lower(), "")
            if not key:
                key = f"KEY_{cmd.upper()}"
            ok = await self.client.send_key(key)
            if not ok:
                _LOGGER.warning("Failed to send key %s to Hisense TV", key)
        self.coordinator.async_update_from_client()
