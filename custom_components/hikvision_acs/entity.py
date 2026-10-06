"""实体基类：统一设备信息，让所有实体挂在同一个设备下。"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import HikvisionCoordinator


class HikvisionEntity(CoordinatorEntity[HikvisionCoordinator]):
    """所有海康门禁实体的基类。"""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        super().__init__(coordinator)
        info = coordinator.data.device_info if coordinator.data else {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            manufacturer=MANUFACTURER,
            name=coordinator.entry.title,
            model=info.get("model") or "门禁一体机",
            sw_version=info.get("firmwareVersion"),
            hw_version=info.get("serialNumber"),
            configuration_url=f"http://{coordinator.client.host}",
        )

    @property
    def _device_info_dict(self) -> dict:
        return self.coordinator.data.device_info if self.coordinator.data else {}
