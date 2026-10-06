"""按钮：远程开门。"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DOOR, DEFAULT_DOOR, DOMAIN
from .coordinator import HikvisionCoordinator
from .entity import HikvisionEntity
from .isapi import HikvisionError


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HikvisionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HikvisionOpenDoorButton(coordinator)])


class HikvisionOpenDoorButton(HikvisionEntity, ButtonEntity):
    """按一下就开门。"""

    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_open_door"

    async def async_press(self) -> None:
        door = self.coordinator.entry.options.get(
            CONF_DOOR, self.coordinator.entry.data.get(CONF_DOOR, DEFAULT_DOOR))
        try:
            await self.coordinator.client.async_open_door(door)
        except HikvisionError as err:
            raise HomeAssistantError(f"远程开门失败：{err}") from err
