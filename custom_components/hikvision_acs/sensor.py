"""传感器：最后开门人 / 方式 / 事件 / 今日开门次数。"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import eventcodes
from .const import DOMAIN
from .coordinator import HikvisionCoordinator
from .entity import HikvisionEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HikvisionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LastPersonSensor(coordinator),
        LastMethodSensor(coordinator),
        LastEventSensor(coordinator),
        TodayOpensSensor(coordinator),
    ])


class _BaseEventSensor(HikvisionEntity, SensorEntity):
    """跟着事件变化刷新的传感器基类。"""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.async_add_event_listener(self._handle_event)

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.async_remove_event_listener(self._handle_event)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event(self, event: eventcodes.NormalizedEvent) -> None:
        self.async_write_ha_state()

    @property
    def _last_event(self) -> eventcodes.NormalizedEvent | None:
        return self.coordinator.data.last_event if self.coordinator.data else None


class LastPersonSensor(_BaseEventSensor):
    """最后一个通过认证的人。"""

    _attr_translation_key = "last_person"
    _attr_icon = "mdi:account-check"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_person"

    @property
    def native_value(self) -> str | None:
        """找最近一条带身份的认证事件——门锁开关类事件没有身份，不能拿来当答案。"""
        for event in self.coordinator.data.events:
            if event.actor_kind == "person" and (event.person or event.person_name):
                return event.person_name or event.person
        return None

    @property
    def extra_state_attributes(self) -> dict:
        for event in self.coordinator.data.events:
            if event.actor_kind == "person" and (event.person or event.person_name):
                return event.as_attributes()
        return {}


class LastMethodSensor(_BaseEventSensor):
    """最后一次开门使用的方式（人脸 / 刷卡 / 开门按钮 / 远程开门 …）。"""

    _attr_translation_key = "last_method"
    _attr_icon = "mdi:key-variant"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_method"

    @property
    def native_value(self) -> str | None:
        for event in self.coordinator.data.events:
            if event.method:
                return event.method
        return None

    @property
    def extra_state_attributes(self) -> dict:
        for event in self.coordinator.data.events:
            if event.method:
                return event.as_attributes()
        return {}


class LastEventSensor(_BaseEventSensor):
    """最后一条事件（含未确认语义的码，便于现场排查）。"""

    _attr_translation_key = "last_event"
    _attr_icon = "mdi:history"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_last_event"

    @property
    def native_value(self) -> str | None:
        event = self._last_event
        return event.event_name if event else None

    @property
    def extra_state_attributes(self) -> dict:
        event = self._last_event
        if event is None:
            return {}
        attrs = event.as_attributes()
        attrs["major_name"] = event.major_name
        attrs["live"] = event.live
        attrs["declared"] = event.declared
        # 留档用：这是设备"配置的"认证模式，不是本次实际方式
        if event.configured_verify_mode:
            attrs["configured_verify_mode"] = event.configured_verify_mode
        return attrs


class TodayOpensSensor(_BaseEventSensor):
    """今日开门次数（按门锁打开事件计）。"""

    _attr_translation_key = "today_opens"
    _attr_icon = "mdi:door-open"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "次"

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_today_opens"

    @property
    def native_value(self) -> int:
        return self.coordinator.data.today_opens if self.coordinator.data else 0

    @property
    def extra_state_attributes(self) -> dict:
        return {"date": self.coordinator.data.today} if self.coordinator.data else {}
