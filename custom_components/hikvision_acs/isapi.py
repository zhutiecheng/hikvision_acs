"""
海康 ISAPI 异步客户端。

用 aiohttp（HA 自带，无需第三方依赖）而非 urllib：HA 是异步的，在事件循环里跑阻塞
IO 会把整个 HA 卡住。

两个来自真机实测的要点：

1. **不能只看 HTTP 状态码。** 海康的接口经常返回 HTTP 200 但 XML 里
   `<statusCode>` 不是 1。必须解析 ResponseStatus，否则会把失败当成功。

2. **`AcsEvent` 查询强制要求 `major` 字段**，缺了返回
   HTTP 400 / MessageParametersLack / errorMsg="major"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree as ET

import aiohttp
from aiohttp import DigestAuth

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
XMLNS = "http://www.isapi.org/ver20/XMLSchema"


class HikvisionError(Exception):
    """海康集成的基类异常。"""


class HikvisionConnectionError(HikvisionError):
    """网络不可达 / 超时。"""


class HikvisionAuthError(HikvisionError):
    """认证失败。"""


class HikvisionResponseError(HikvisionError):
    """设备返回了业务错误（HTTP 200 但 statusCode != 1）。"""

    def __init__(self, message: str, status_code: str | None = None,
                 sub_status: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.sub_status = sub_status


@dataclass
class ResponseStatus:
    ok: bool
    status_code: str | None
    status_string: str | None
    sub_status: str | None
    error_msg: str | None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_response_status(text: str) -> ResponseStatus | None:
    """
    解析海康的 <ResponseStatus>。

    这是判断成败的唯一可靠依据——HTTP 200 不代表成功。
    """
    if "<ResponseStatus" not in text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if _local(root.tag) != "ResponseStatus":
        # 可能外面还包了一层
        found = None
        for child in root.iter():
            if _local(child.tag) == "ResponseStatus":
                found = child
                break
        if found is None:
            return None
        root = found

    values = {_local(c.tag): (c.text or "").strip() for c in root}
    code = values.get("statusCode", "")
    return ResponseStatus(
        ok=(code == "1"),
        status_code=code or None,
        status_string=values.get("statusString") or None,
        sub_status=values.get("subStatusCode") or None,
        error_msg=values.get("errorMsg") or None,
    )


def _values(text: str) -> dict[str, str]:
    """把一段扁平的 XML 抓成 {标签: 文本}。"""
    return {m.group(1): m.group(2) for m in
            re.finditer(r"<(\w+)>([^<]*)</\1>", text)}


class HikvisionClient:
    """一个门禁设备的 ISAPI 客户端。"""

    def __init__(self, session: aiohttp.ClientSession, host: str,
                 username: str, password: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self._session = session
        self.host = host
        self._username = username
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._scheme = "http"
        self._port: int | None = None

    # -- 基础请求 ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        port = f":{self._port}" if self._port else ""
        return f"{self._scheme}://{self.host}{port}"

    async def _request(self, method: str, path: str, *,
                       data: bytes | None = None,
                       content_type: str | None = None,
                       timeout: aiohttp.ClientTimeout | None = None) -> tuple[int, bytes, dict]:
        url = self.base_url + path
        headers = {"Content-Type": content_type} if content_type else {}
        try:
            async with self._session.request(
                method, url, data=data, headers=headers,
                auth=DigestAuth(self._username, self._password),
                timeout=timeout or self._timeout,
            ) as resp:
                body = await resp.read()
                return resp.status, body, dict(resp.headers)
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise HikvisionAuthError("认证失败，请检查用户名和密码") from err
            raise HikvisionError(f"HTTP {err.status}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HikvisionConnectionError(f"连接 {self.host} 失败: {err}") from err

    async def _request_text(self, method: str, path: str, *,
                            data: bytes | None = None,
                            content_type: str | None = None,
                            timeout: aiohttp.ClientTimeout | None = None) -> str:
        _, body, _ = await self._request(method, path, data=data,
                                         content_type=content_type, timeout=timeout)
        return body.decode("utf-8", "replace")

    @staticmethod
    def _raise_for_status(text: str, action: str) -> None:
        """HTTP 通了不代表业务成功——解析 ResponseStatus 再决定。"""
        status = parse_response_status(text)
        if status is not None and not status.ok:
            raise HikvisionResponseError(
                f"{action}失败：{status.status_string or '未知'} "
                f"({status.error_msg or status.sub_status or status.status_code})",
                status_code=status.status_code,
                sub_status=status.sub_status,
            )

    # -- 设备信息 ---------------------------------------------------------

    async def async_get_device_info(self) -> dict[str, str]:
        text = await self._request_text("GET", "/ISAPI/System/deviceInfo")
        return _values(text)

    async def async_get_declared_codes(self) -> dict[int, set[int]]:
        """
        取设备自述的可上报事件码（按大类）。

        用于发现固件行为变化：收到设备清单之外的码，往往意味着固件升级后语义变了。
        """
        text = await self._request_text(
            "GET", "/ISAPI/Event/notification/httpHosts/capabilities")
        result: dict[int, set[int]] = {}
        for major, key in ((1, "minorAlarm"), (2, "minorException"),
                           (3, "minorOperation"), (5, "minorEvent")):
            m = re.search(rf"<{key}[^>]*>([^<]*)</{key}>", text)
            if not m or not m.group(1).strip():
                continue
            codes: set[int] = set()
            for token in m.group(1).split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    codes.add(int(token, 16) if token.lower().startswith("0x")
                              else int(token))
                except ValueError:
                    continue
            result[major] = codes
        return result

    # -- 事件 -------------------------------------------------------------

    async def async_query_events(self, start: datetime, end: datetime,
                                 major: int = 5, max_results: int = 50,
                                 position: int = 0) -> list[dict]:
        """
        查询历史门禁事件。

        本机型强制要求 major 字段，缺了会返回 MessageParametersLack。
        """
        payload = json.dumps({
            "AcsEventCond": {
                "searchID": "ha",
                "searchResultPosition": position,
                "maxResults": max_results,
                "major": major,
                "minor": 0,
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            }
        }).encode()
        text = await self._request_text(
            "POST", "/ISAPI/AccessControl/AcsEvent?format=json",
            data=payload, content_type="application/json")
        try:
            data = json.loads(text)
        except ValueError as err:
            raise HikvisionError(f"事件查询返回了非 JSON：{text[:200]}") from err
        return (data.get("AcsEvent") or {}).get("InfoList") or []

    # -- HTTP 监听（事件主动推送）------------------------------------------

    async def async_get_http_hosts(self) -> str:
        return await self._request_text(
            "GET", "/ISAPI/Event/notification/httpHosts")

    async def async_set_http_host(self, index: int, ip: str, port: int,
                                  url: str) -> None:
        """
        配置设备把事件 POST 到指定地址。

        这条路径比 alertStream 长连接可靠得多：设备每个事件开一次短连接，
        不存在"会话被占住"和"客户端被强杀后设备察觉不到"的问题（实测教训）。
        """
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<HttpHostNotificationList version="2.0" xmlns="{XMLNS}">'
            '<HttpHostNotification>'
            f'<id>{index}</id>'
            f'<url>{url}</url>'
            '<protocolType>HTTP</protocolType>'
            '<parameterFormatType>json</parameterFormatType>'
            '<addressingFormatType>ipaddress</addressingFormatType>'
            f'<ipAddress>{ip}</ipAddress>'
            f'<portNo>{port}</portNo>'
            '<httpAuthenticationMethod>none</httpAuthenticationMethod>'
            '<uploadImagesDataType>binary</uploadImagesDataType>'
            '</HttpHostNotification>'
            '</HttpHostNotificationList>'
        ).encode()
        text = await self._request_text(
            "PUT", "/ISAPI/Event/notification/httpHosts",
            data=xml, content_type="application/xml")
        self._raise_for_status(text, "配置 HTTP 监听")

        # 回读确认——设备可能接受请求但没真正落配置
        current = await self.async_get_http_hosts()
        if f"<ipAddress>{ip}</ipAddress>" not in current or url not in current:
            raise HikvisionError(
                "HTTP 监听配置回读不一致，设备可能没有接受该配置")

    # -- 控制 -------------------------------------------------------------

    async def async_open_door(self, door: int = 1) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<RemoteControlDoor version="2.0" xmlns="{XMLNS}">'
            '<cmd>open</cmd></RemoteControlDoor>'
        ).encode()
        text = await self._request_text(
            "PUT", f"/ISAPI/AccessControl/RemoteControl/door/{door}",
            data=xml, content_type="application/xml")
        self._raise_for_status(text, "远程开门")

    # -- 抓拍 -------------------------------------------------------------

    async def async_get_snapshot(self, channel: int = 101) -> bytes | None:
        """
        取一张实时抓拍图。

        门铃响起时先出这张图再起视频流，观感差别很大（实测取图约 0.7 秒）。
        """
        try:
            status, body, headers = await self._request(
                "GET", f"/ISAPI/Streaming/channels/{channel}/picture",
                timeout=aiohttp.ClientTimeout(total=15))
        except HikvisionError:
            return None
        if status != 200 or not body:
            return None
        if body[:2] == b"\xff\xd8":
            return body
        _LOGGER.debug("抓拍返回的内容不是 JPEG：%s", headers.get("Content-Type"))
        return None

    async def async_probe(self) -> dict[str, str]:
        """连通性 + 认证自检，配置流程里用。"""
        return await self.async_get_device_info()
