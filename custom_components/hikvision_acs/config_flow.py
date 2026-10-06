"""配置流程：全部在 UI 里完成，客户不需要编辑任何 YAML。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DOOR,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_RTSP_CHANNEL,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_USE_HTTP_LISTENING,
    CONF_WEBHOOK_ID,
    DEFAULT_DOOR,
    DEFAULT_RTSP_CHANNEL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DEFAULT_USE_HTTP_LISTENING,
    DOMAIN,
)
from .isapi import (
    HikvisionAuthError,
    HikvisionClient,
    HikvisionConnectionError,
    HikvisionError,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_USERNAME, default=DEFAULT_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Optional(CONF_DOOR, default=DEFAULT_DOOR): vol.All(int, vol.Range(min=1, max=4)),
    vol.Optional(CONF_RTSP_CHANNEL, default=DEFAULT_RTSP_CHANNEL):
        vol.In({101: "主码流（清晰，起流稍慢）", 102: "子码流（起流快，推荐）"}),
    vol.Optional(CONF_USE_HTTP_LISTENING, default=DEFAULT_USE_HTTP_LISTENING): bool,
})


class HikvisionConfigFlow(ConfigFlow, domain=DOMAIN):
    """添加一台门禁设备。"""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            client = HikvisionClient(
                host,
                user_input.get(CONF_USERNAME, DEFAULT_USERNAME),
                user_input[CONF_PASSWORD],
            )
            try:
                info = await client.async_probe()
            except HikvisionAuthError:
                errors["base"] = "invalid_auth"
            except HikvisionConnectionError:
                errors["base"] = "cannot_connect"
            except HikvisionError as err:
                _LOGGER.debug("探测设备失败：%s", err)
                errors["base"] = "unknown"
            except Exception:                            # noqa: BLE001
                _LOGGER.exception("探测设备时发生意外错误")
                errors["base"] = "unknown"
            finally:
                await client.async_close()
            if not errors:
                if info.get("deviceType") not in (None, "", "ACS"):
                    errors["base"] = "not_access_controller"
                else:
                    unique = info.get("serialNumber") or host
                    await self.async_set_unique_id(unique)
                    self._abort_if_unique_id_configured()

                    title = info.get("model") or "海康门禁"
                    return self.async_create_entry(
                        title=f"{title} ({host})",
                        data={
                            **user_input,
                            CONF_HOST: host,
                            # webhook 由 HA 管理，这里只保存它的 id
                            CONF_WEBHOOK_ID: uuid.uuid4().hex,
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
            self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            client = HikvisionClient(
                entry.data[CONF_HOST],
                entry.data.get(CONF_USERNAME, DEFAULT_USERNAME),
                user_input[CONF_PASSWORD],
            )
            try:
                await client.async_probe()
            except HikvisionAuthError:
                errors["base"] = "invalid_auth"
            except HikvisionError:
                errors["base"] = "cannot_connect"
            finally:
                await client.async_close()
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]})

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HikvisionOptionsFlow()


class HikvisionOptionsFlow(OptionsFlow):
    """改轮询间隔、门号、码流。"""

    async def async_step_init(
            self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        data = self.config_entry.data
        options = self.config_entry.options

        schema = vol.Schema({
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL,
                                    data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            ): vol.All(int, vol.Range(min=5, max=600)),
            vol.Optional(
                CONF_DOOR,
                default=options.get(CONF_DOOR, data.get(CONF_DOOR, DEFAULT_DOOR)),
            ): vol.All(int, vol.Range(min=1, max=4)),
            vol.Optional(
                CONF_RTSP_CHANNEL,
                default=options.get(CONF_RTSP_CHANNEL,
                                    data.get(CONF_RTSP_CHANNEL, DEFAULT_RTSP_CHANNEL)),
            ): vol.In({101: "主码流", 102: "子码流"}),
            vol.Optional(
                CONF_USE_HTTP_LISTENING,
                default=options.get(CONF_USE_HTTP_LISTENING,
                                    data.get(CONF_USE_HTTP_LISTENING,
                                             DEFAULT_USE_HTTP_LISTENING)),
            ): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
