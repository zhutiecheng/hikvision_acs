"""海康威视门禁 HA 集成。"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ATTR_DOOR,
    CONF_DOOR,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_USERNAME,
    DOMAIN,
    SERVICE_OPEN_DOOR,
)
from .coordinator import HikvisionCoordinator
from .isapi import HikvisionClient, HikvisionError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SENSOR,
]

OPEN_DOOR_SCHEMA = vol.Schema({
    vol.Optional("entry_id"): cv.string,
    vol.Optional(ATTR_DOOR): vol.All(int, vol.Range(min=1, max=4)),
})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = HikvisionClient(
        session,
        entry.data[CONF_HOST],
        entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
        entry.data[CONF_PASSWORD],
    )
    coordinator = HikvisionCoordinator(hass, entry, client)

    await coordinator.async_config_entry_first_refresh()

    # 推送配置失败不算致命——退化成纯轮询仍然可用
    await coordinator.async_setup_push()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: HikvisionCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_remove_push()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        remaining = hass.data.get(DOMAIN, {})
        remaining.pop(entry.entry_id, None)
        if not remaining and hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR):
            # 最后一台设备卸载后把服务摘掉，别留一个会报"没有已配置设备"的空服务
            hass.services.async_remove(DOMAIN, SERVICE_OPEN_DOOR)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项改了要重载：轮询间隔和码流都影响实体。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR):
        return

    async def _async_open_door(call: ServiceCall) -> dict:
        entry_id = call.data.get("entry_id")
        coordinators: dict[str, HikvisionCoordinator] = hass.data.get(DOMAIN, {})
        if not coordinators:
            raise ValueError("没有已配置的海康门禁设备")

        if entry_id:
            targets = [coordinators[entry_id]] if entry_id in coordinators else []
            if not targets:
                raise ValueError(f"找不到 entry_id={entry_id} 的设备")
        else:
            targets = list(coordinators.values())

        results = []
        for coordinator in targets:
            door = call.data.get(ATTR_DOOR, coordinator.entry.options.get(
                CONF_DOOR, coordinator.entry.data.get(CONF_DOOR, 1)))
            try:
                await coordinator.client.async_open_door(door)
                results.append({"host": coordinator.client.host, "door": door, "ok": True})
            except HikvisionError as err:
                _LOGGER.error("远程开门失败（%s 门 %s）：%s",
                              coordinator.client.host, door, err)
                results.append({
                    "host": coordinator.client.host, "door": door,
                    "ok": False, "error": str(err),
                })
        return {"results": results}

    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_DOOR, _async_open_door,
        schema=OPEN_DOOR_SCHEMA, supports_response=SupportsResponse.OPTIONAL,
    )
