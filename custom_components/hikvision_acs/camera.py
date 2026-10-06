"""
摄像头实体：把门禁自带的摄像头接进 HA。

提供 RTSP 流源，交给 HA 的 stream 组件转流；同时提供抓拍图（门铃弹出时
先出图再起流，观感差别很大）。

低延迟说明：HA 自带的 stream 组件默认走 HLS，延迟通常 3~10 秒，门铃场景偏慢。
如果想要 WebRTC 的低延迟（实测约 0.7 秒出帧），需要额外装 HA 的
「WebRTC / go2rtc」相关集成或加载项，让本实体的流走 WebRTC。
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_RTSP_CHANNEL,
    CONF_USERNAME,
    DEFAULT_RTSP_CHANNEL,
    DEFAULT_USERNAME,
    DOMAIN,
    MANUFACTURER,
)
from .coordinator import HikvisionCoordinator
from .entity import HikvisionEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    coordinator: HikvisionCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HikvisionCamera(coordinator)])


class HikvisionCamera(HikvisionEntity, Camera):
    """门口摄像头。"""

    _attr_translation_key = "camera"
    _attr_brand = MANUFACTURER

    def __init__(self, coordinator: HikvisionCoordinator) -> None:
        HikvisionEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_camera"
        self._attr_model = (coordinator.data.device_info.get("model")
                            if coordinator.data else None)

    def _channel(self) -> int:
        entry = self.coordinator.entry
        return entry.options.get(
            CONF_RTSP_CHANNEL,
            entry.data.get(CONF_RTSP_CHANNEL, DEFAULT_RTSP_CHANNEL))

    async def stream_source(self) -> str | None:
        """给 HA 的 stream 组件用的 RTSP 地址。"""
        entry = self.coordinator.entry
        user = entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
        password = entry.data[CONF_PASSWORD]
        host = entry.data[CONF_HOST]
        return (f"rtsp://{user}:{password}@{host}:554"
                f"/Streaming/Channels/{self._channel()}")

    async def async_camera_image(self, width: int | None = None,
                                 height: int | None = None) -> bytes | None:
        """抓拍图。优先用主码流拿清晰画面，失败则返回 None 让 HA 用流截图。"""
        return await self.coordinator.client.async_get_snapshot(101)
