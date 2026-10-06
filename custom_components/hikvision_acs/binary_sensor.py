"""二元传感器：在线状态、门锁状态。"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eventcodes
from .const import DOMAIN
from .coordinator import HikvisionCoordinator
from .entity import HikvisionEntity

LOCK_OPEN = (5, 21)      # 门锁打开
LOCK_CLOSE = (5, 22)     # 门锁关闭


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HikvisionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        HikvisionOnlineSensor(coordinator),
        HikvisionDoorLockSensor(coordinator),
    ])


class HikvisionOnlineSensor(HikvisionEntity, BinarySensorEntity):
    """设备是否可达。"""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "online"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_online"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data and self.coordinator.data.online)

    @property
    def available(self) -> bool:
        # 在线传感器本身必须在设备离线时仍然可用，否则就永远看不到"离线"
        return True


class HikvisionDoorLockSensor(HikvisionEntity, BinarySensorEntity):
    """
    门锁状态。

    注意：这反映的是**门锁继电器**的状态（开锁/上锁），不是门扇的实际开合。
    门扇实际状态需要接门磁（SENSOR 端子），设备才会产生门磁类事件上报。
    """

    _attr_device_class = BinarySensorDeviceClass.LOCK
    _attr_translation_key = "door_lock"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_door_lock"
        self._is_open = False
        self._restore_from_history()

    def _restore_from_history(self) -> None:
        """HA 重启后用最近一条门锁事件恢复状态，避免显示成"已上锁"。"""
        for event in self.coordinator.data.events:
            code = (event.major, event.minor)
            if code == LOCK_OPEN:
                self._is_open = True
                return
            if code == LOCK_CLOSE:
                self._is_open = False
                return

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.async_add_event_listener(self._handle_event)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.async_remove_event_listener(self._handle_event)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event(self, event: eventcodes.NormalizedEvent) -> None:
        code = (event.major, event.minor)
        if code == LOCK_OPEN:
            self._is_open = True
        elif code == LOCK_CLOSE:
            self._is_open = False
        else:
            return
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._is_open
