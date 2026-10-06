"""
事件实体：把门禁事件变成 HA 自动化可以触发的东西。

这是「按门铃自动推流」「有人开门就通知」这类需求的接入点——
在自动化里直接选这个实体，不需要解析任何原始报文。
"""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eventcodes
from .const import DOMAIN, EVENT_TYPES
from .coordinator import HikvisionCoordinator
from .entity import HikvisionEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HikvisionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HikvisionEventEntity(coordinator)])


class HikvisionEventEntity(HikvisionEntity, EventEntity):
    """设备上发生的最后一个可触发事件。"""

    _attr_event_types = EVENT_TYPES
    _attr_translation_key = "access_event"
    _attr_icon = "mdi:doorbell"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        HikvisionEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_event"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.async_add_event_listener(self._handle_event)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.async_remove_event_listener(self._handle_event)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event(self, event: eventcodes.NormalizedEvent) -> None:
        if not event.ha_event:
            # 语义未确认的事件码不触发自动化——宁可漏触发，也不要误触发
            # （把一次门铃误判成开门，比少报一次严重得多）
            return
        self._trigger_event(event.ha_event, event.as_attributes())
        self.async_write_ha_state()
