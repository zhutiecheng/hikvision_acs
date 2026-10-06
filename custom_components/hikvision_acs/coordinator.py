"""
数据协调器：把设备事件接进来，维护状态，并广播给实体。

事件入口有两条，互补：

* **主通道：HA webhook。** 集成把设备的 HTTP 监听指向
  `http(s)://<HA>/api/webhook/<id>`，设备每个事件 POST 一次。
  不需要额外端口、不需要容器、不需要独立服务——这是把已验证的 HTTP 监听
  路径直接落到 HA 基础设施上。

  为什么不用 ISAPI 的 alertStream 长连接：实测该设备只允许一条会话，且因为
  "有事件才写数据"，客户端异常断开后设备长时间不释放，之后所有连接被拒
  （有时 404，有时直接不响应）。做在 HA 里这种故障会很难排查。

* **兜底：轮询 AcsEvent。** 按 serialNo 去重，补齐 webhook 漏掉的（网络抖动、
  HA 重启期间、设备没推成功）。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import eventcodes
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_USE_HTTP_LISTENING,
    CONF_WEBHOOK_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HA_EVENT_NEW_EVENT,
    KEEP_EVENTS,
)
from .isapi import (
    HikvisionAuthError,
    HikvisionClient,
    HikvisionConnectionError,
    HikvisionError,
)

_LOGGER = logging.getLogger(__name__)

# 门锁打开事件：判断"门被打开"最可靠的信号
DOOR_OPENED_CODE = (5, 21)

FALLBACK_LOOKBACK_MINUTES = 30


@dataclass
class HikvisionData:
    """协调器对外暴露的数据快照。"""

    online: bool = False
    device_info: dict[str, str] = field(default_factory=dict)
    events: deque[eventcodes.NormalizedEvent] = field(
        default_factory=lambda: deque(maxlen=KEEP_EVENTS))
    last_event: eventcodes.NormalizedEvent | None = None
    last_open_event: eventcodes.NormalizedEvent | None = None
    today_opens: int = 0
    today: str = ""
    push_configured: bool = False
    declared_codes: dict[int, set[int]] = field(default_factory=dict)


class HikvisionCoordinator(DataUpdateCoordinator[HikvisionData]):
    """轮询兜底 + webhook 推送。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry,
                 client: HikvisionClient) -> None:
        self.client = client
        self.entry = entry
        self.webhook_id: str | None = None
        self._seen: deque[str] = deque(maxlen=500)
        self._seen_set: set[str] = set()
        self._event_listeners: list = []
        self._window_start = dt_util.utcnow()

        scan_interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {client.host}",
            update_interval=timedelta(seconds=scan_interval),
            config_entry=entry,
        )
        self.data = HikvisionData(today=dt_util.now().strftime("%Y-%m-%d"))

    # -- 事件订阅（给实体用）----------------------------------------------

    @callback
    def async_add_event_listener(self, listener) -> None:
        """注册一个新事件回调。回调收到 NormalizedEvent。"""
        self._event_listeners.append(listener)

    @callback
    def async_remove_event_listener(self, listener) -> None:
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    # -- 去重 -------------------------------------------------------------

    def _is_duplicate(self, event: eventcodes.NormalizedEvent) -> bool:
        """
        按 serialNo 去重。

        设备侧的 serialNo 是全局递增的事件流水号，webhook 推送与轮询兜底会
        大量重叠，靠它去重既准确又便宜。
        """
        key = event.dedup_key
        if key is None:
            # 没有流水号（少见）：退化成"同一秒同一事件码只算一次"
            key = f"{event.time}|{event.major}|{event.minor}|{event.person}"
        if key in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            oldest = self._seen[0]
            self._seen_set.discard(oldest)
        self._seen.append(key)
        self._seen_set.add(key)
        return False

    # -- 事件入口 ---------------------------------------------------------

    @callback
    def async_handle_raw(self, raw: dict) -> bool:
        """
        处理一条原始报文（webhook 与轮询共用）。

        返回是否为新事件（已去重的返回 False）。
        """
        event = eventcodes.normalize(raw)
        if event is None:
            return False
        if self._is_duplicate(event):
            return False
        self._apply(event)
        return True

    @callback
    def _apply(self, event: eventcodes.NormalizedEvent) -> None:
        """把一条新事件并进状态，并广播出去。"""
        data = self.data
        today = dt_util.now().strftime("%Y-%m-%d")
        if data.today != today:
            data.today = today
            data.today_opens = 0

        data.events.appendleft(event)
        data.last_event = event
        if (event.major, event.minor) == DOOR_OPENED_CODE and event.code_verified:
            data.today_opens += 1
            data.last_open_event = event
        elif event.opens_door and event.actor_kind == "person":
            # 认证通过也算一次开门意图，但上面那条才是权威的"门真的开了"
            data.last_open_event = event

        if not event.declared and event.code_verified is False:
            # 收到设备清单之外的码，通常意味着固件行为变了，值得告警
            if not eventcodes.is_declared(event.major, event.minor):
                _LOGGER.warning(
                    "收到设备未声明的事件码 major=%s minor=%s(0x%x)：%s",
                    event.major, event.minor, event.minor, event.event_name)

        _LOGGER.debug("门禁事件 %s (%s,%s) 方式=%s 人=%s",
                      event.event_name, event.major, event.minor,
                      event.method, event.person)

        for listener in list(self._event_listeners):
            try:
                listener(event)
            except Exception:                            # noqa: BLE001
                _LOGGER.exception("事件监听器抛异常，已忽略该监听器")

        self.hass.bus.async_fire(HA_EVENT_NEW_EVENT, event.as_attributes())
        self.async_set_updated_data(data)

    # -- webhook ----------------------------------------------------------

    async def async_setup_push(self) -> bool:
        """
        注册 HA webhook 并把设备的 HTTP 监听指过来。

        失败不阻断集成启动——退化成纯轮询仍然可用，只是事件延迟变大。
        """
        if not self.entry.data.get(CONF_USE_HTTP_LISTENING, True):
            _LOGGER.info("已按配置关闭 HTTP 监听，仅使用轮询")
            return False

        try:
            base = get_url(self.hass, allow_internal=True, prefer_external=False)
        except Exception as err:                         # noqa: BLE001
            _LOGGER.warning("无法确定 HA 自身地址，跳过 HTTP 监听配置：%s", err)
            return False

        parsed = urlparse(base)
        ha_host, ha_port = parsed.hostname, parsed.port
        if not ha_host:
            _LOGGER.warning("HA 地址解析失败（%s），跳过 HTTP 监听配置", base)
            return False

        self.webhook_id = self.entry.data.get(CONF_WEBHOOK_ID)
        if not self.webhook_id:
            _LOGGER.warning("配置项里没有 webhook_id，跳过 HTTP 监听配置")
            return False

        webhook.async_register(
            self.hass, DOMAIN,
            f"海康门禁 {self.client.host}",
            self.webhook_id,
            self._async_handle_webhook,
            allowed_methods=["POST"],
            local_only=True,
        )

        scheme = parsed.scheme or "http"
        path = f"/api/webhook/{self.webhook_id}"
        try:
            await self.client.async_set_http_host(
                1, ha_host, ha_port or (443 if scheme == "https" else 80), path)
        except HikvisionAuthError as err:
            _LOGGER.error("配置 HTTP 监听时认证失败：%s", err)
            return False
        except HikvisionError as err:
            _LOGGER.warning(
                "配置设备 HTTP 监听失败：%s。将仅使用轮询兜底（事件延迟最多 %s 秒）",
                err, self.update_interval)
            return False

        if scheme == "https":
            _LOGGER.warning(
                "HA 使用 HTTPS（%s）。设备端可能因证书校验失败而无法推送事件，"
                "若收不到实时事件请改用 HA 的 HTTP 地址或配好证书", base)
        _LOGGER.info("已让设备把事件推送到 %s%s", base.rstrip("/"), path)
        self.data.push_configured = True
        return True

    async def async_remove_push(self) -> None:
        if self.webhook_id:
            webhook.async_unregister(self.hass, self.webhook_id)
            self.webhook_id = None

    async def _async_handle_webhook(self, hass: HomeAssistant, webhook_id: str,
                                    request) -> None:
        """设备推来的一个事件。"""
        try:
            body = await request.read()
        except Exception as err:                         # noqa: BLE001
            _LOGGER.warning("读取设备推送失败：%s", err)
            return

        raw = eventcodes.parse_notification(body)
        if raw is not None:
            self.async_handle_raw(raw)
        else:
            _LOGGER.debug("无法解析设备推送（%d 字节）：%r", len(body), body[:200])

    # -- 轮询兜底 ---------------------------------------------------------

    async def _async_update_data(self) -> HikvisionData:
        data = self.data
        try:
            data.device_info = await self.client.async_get_device_info()
            data.online = True
        except HikvisionAuthError as err:
            raise UpdateFailed(f"认证失败：{err}") from err
        except HikvisionConnectionError as err:
            data.online = False
            raise UpdateFailed(str(err)) from err

        if not data.declared_codes:
            try:
                data.declared_codes = await self.client.async_get_declared_codes()
            except HikvisionError as err:
                _LOGGER.debug("读取设备事件码能力失败：%s", err)

        await self._async_poll_events()
        return data

    async def _async_poll_events(self) -> None:
        """
        按时间窗回查事件表补漏。

        时间窗刻意往回多退一点并依赖 serialNo 去重——宁可重复拉取，也不要漏事件。
        """
        now = dt_util.now()
        start = max(self._window_start, now - timedelta(minutes=FALLBACK_LOOKBACK_MINUTES))
        # 设备时间可能与本机略有偏差，回退一点避免掐头
        start = start - timedelta(seconds=30)
        self._window_start = now

        for major in (5, 3, 1, 2):
            try:
                infos = await self.client.async_query_events(start, now, major=major)
            except HikvisionError as err:
                _LOGGER.debug("轮询 major=%s 失败：%s", major, err)
                continue
            for info in infos:
                self.async_handle_raw({
                    "dateTime": info.get("time"),
                    "eventType": "AccessControllerEvent",
                    "AccessControllerEvent": {
                        **info,
                        "majorEventType": info.get("major"),
                        "subEventType": info.get("minor"),
                        # 回查结果不是实时事件
                        "currentEvent": False,
                    },
                })
