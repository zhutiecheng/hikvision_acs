"""
设备侧客户端：alertStream 订阅、历史事件对账、远程开门、抓拍。

只用标准库，不引入任何第三方依赖——这台机器上跑的东西越少越不容易坏。
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from base64 import b64encode
from hashlib import md5
from typing import Callable

log = logging.getLogger("acs.device")

BOUNDARY_FALLBACK = b"MIME_boundary"


# ---------------------------------------------------------------------------
# HTTP Digest 认证（标准库的 HTTPDigestAuthHandler 不支持流式读取，这里自己实现）
# ---------------------------------------------------------------------------

class DigestAuth:
    """极简 HTTP Digest (MD5) 认证，够 ISAPI 用。"""

    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self._realm = ""
        self._nonce = ""
        self._qop = ""
        self._opaque = ""
        self._nc = 0
        self._lock = threading.Lock()

    @staticmethod
    def _parse_challenge(header: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header):
            out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
        return out

    def update_from(self, www_authenticate: str) -> bool:
        if not www_authenticate.lower().startswith("digest"):
            return False
        d = self._parse_challenge(www_authenticate)
        with self._lock:
            self._realm = d.get("realm", "")
            self._nonce = d.get("nonce", "")
            self._qop = d.get("qop", "").split(",")[0].strip()
            self._opaque = d.get("opaque", "")
            self._nc = 0
        return True

    def header(self, method: str, uri: str, body: bytes = b"") -> str:
        with self._lock:
            self._nc += 1
            nc = f"{self._nc:08x}"
            realm, nonce, qop, opaque = self._realm, self._nonce, self._qop, self._opaque

        def h(data: bytes) -> str:
            return md5(data).hexdigest()

        ha1 = h(f"{self.user}:{realm}:{self.password}".encode())
        ha2 = h(f"{method}:{uri}".encode())
        if qop in ("auth", "auth-int"):
            response = h(f"{ha1}:{nonce}:{nc}:{'0' * 32}:{qop}:{ha2}".encode())
        else:
            response = h(f"{ha1}:{nonce}:{ha2}".encode())

        parts = [
            f'username="{self.user}"', f'realm="{realm}"', f'nonce="{nonce}"',
            f'uri="{uri}"', f'response="{response}"', "algorithm=MD5",
        ]
        if qop:
            parts += [f"qop={qop}", f"nc={nc}", f"cnonce=\"{'0' * 32}\""]
        if opaque:
            parts.append(f'opaque="{opaque}"')
        return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------------
# multipart/mixed 分帧
# ---------------------------------------------------------------------------

def split_parts(buffer: bytes, boundary: bytes) -> tuple[list[bytes], bytes]:
    """
    从缓冲区切出完整 part，返回 (完整part列表, 剩余缓冲区)。

    closing delimiter 的判定不能只看是否以 "--" 开头：网络分片可能正好切在
    "--boundary" 中间，此时剩余缓冲区形如 "\\r\\n--M…"，按前缀判断会把它
    误认成结束符丢弃，导致分隔符残字混进下一个 part 的头部。
    只有当 "--" 后面确实跟了行结束符时才能确认结束。
    """
    chunks = buffer.split(b"--" + boundary)
    remainder = chunks.pop()
    complete = [c for c in chunks if c.strip(b"\r\n")]

    stripped = remainder.lstrip(b"\r\n")
    if stripped == b"" or stripped.startswith(b"--\r") or stripped.startswith(b"--\n"):
        return complete, b""
    return complete, remainder


def parse_part(part: bytes) -> tuple[str, bytes, dict | None]:
    """拆一个 part 成 (头部文本, 原始body, JSON或None)。"""
    part = part.strip(b"\r\n")
    if b"\r\n\r\n" in part:
        head, body = part.split(b"\r\n\r\n", 1)
    else:
        head, body = b"", part
    body = body.rstrip(b"\r\n")
    obj = None
    if body[:1] in (b"{", b"["):
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            obj = None
    return head.decode("utf-8", "replace"), body, obj


# ---------------------------------------------------------------------------
# 设备客户端
# ---------------------------------------------------------------------------

class DeviceClient:
    def __init__(self, host: str, user: str, password: str,
                 timeout: int = 10, picture_timeout: int = 20):
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout
        self.picture_timeout = picture_timeout
        self.base = f"http://{host}"

    # -- 基础请求 ---------------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str | None = None, stream: bool = False,
                 read_timeout: int | None = None):
        auth = DigestAuth(self.user, self.password)
        url = self.base + path

        for attempt in (1, 2):
            req = urllib.request.Request(url, data=body, method=method)
            if content_type:
                req.add_header("Content-Type", content_type)
            if attempt == 2:
                req.add_header("Authorization", auth.header(method, path, body or b""))
            try:
                resp = urllib.request.urlopen(req, timeout=read_timeout or self.timeout)
                return resp
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    if auth.update_from(e.headers.get("WWW-Authenticate", "")):
                        continue
                raise
        raise RuntimeError("unreachable")

    def get(self, path: str) -> tuple[int, str]:
        try:
            with self._request("GET", path) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def put(self, path: str, body: bytes, content_type: str = "application/xml") -> tuple[int, str]:
        try:
            with self._request("PUT", path, body, content_type) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post(self, path: str, body: bytes, content_type: str = "application/json") -> tuple[int, str]:
        try:
            with self._request("POST", path, body, content_type) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    # -- HTTP 监听（事件主动推送）-----------------------------------------

    def get_http_hosts(self) -> tuple[int, str]:
        return self.get("/ISAPI/Event/notification/httpHosts")

    def set_http_host(self, index: int, ip: str, port: int, url: str,
                      fmt: str = "json", upload_images: str = "binary") -> tuple[int, str]:
        """
        配置设备把事件 POST 到我们的服务。

        这条路径比 alertStream 长连接可靠得多：设备每个事件开一次短连接，
        不存在"会话被占住"和"客户端被强杀后设备察觉不到"的问题。
        """
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<HttpHostNotificationList version="2.0" '
            'xmlns="http://www.isapi.org/ver20/XMLSchema">'
            '<HttpHostNotification>'
            f'<id>{index}</id>'
            f'<url>{url}</url>'
            '<protocolType>HTTP</protocolType>'
            f'<parameterFormatType>{fmt}</parameterFormatType>'
            '<addressingFormatType>ipaddress</addressingFormatType>'
            f'<ipAddress>{ip}</ipAddress>'
            f'<portNo>{port}</portNo>'
            '<httpAuthenticationMethod>none</httpAuthenticationMethod>'
            f'<uploadImagesDataType>{upload_images}</uploadImagesDataType>'
            '</HttpHostNotification>'
            '</HttpHostNotificationList>'
        ).encode()
        return self.put("/ISAPI/Event/notification/httpHosts", xml)

    def test_http_host(self, index: int = 1) -> tuple[int, str]:
        """让设备立刻按配置推一条测试报文过来，用于端到端自检。"""
        return self.post(f"/ISAPI/Event/notification/httpHosts/{index}/test", b"")

    # -- 业务接口 ---------------------------------------------------------

    def device_info(self) -> dict[str, str]:
        _, xml = self.get("/ISAPI/System/deviceInfo")
        return {m.group(1): m.group(2) for m in
                re.finditer(r"<(\w+)>([^<]*)</\1>", xml)}

    def open_door(self, door: int = 1) -> tuple[bool, str]:
        """远程开门。返回 (是否成功, 说明)。"""
        xml = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
               '<cmd>open</cmd></RemoteControlDoor>').encode()
        code, text = self.put(f"/ISAPI/AccessControl/RemoteControl/door/{door}", xml)
        if code == 200:
            return True, "ok"
        return False, f"HTTP {code}: {text[:300]}"

    def query_events(self, start: str, end: str, major: int = 5,
                     position: int = 0, max_results: int = 30) -> dict:
        """查历史门禁事件。本机型强制要求 major 字段，缺了会返回 MessageParametersLack。"""
        payload = json.dumps({
            "AcsEventCond": {
                "searchID": "gw",
                "searchResultPosition": position,
                "maxResults": max_results,
                "major": major,
                "minor": 0,
                "startTime": start,
                "endTime": end,
            }
        }).encode()
        code, text = self.post("/ISAPI/AccessControl/AcsEvent?format=json", payload)
        if code != 200:
            raise RuntimeError(f"查询事件失败 HTTP {code}: {text[:200]}")
        return json.loads(text)

    # -- 事件流 -----------------------------------------------------------

    def stream_events(self, on_event: Callable[[dict, bytes | None], None],
                      should_stop: Callable[[], bool],
                      on_connect: Callable[[], None] | None = None) -> None:
        """
        订阅 alertStream，一直读到 should_stop() 为真。

        on_event(归一化前的原始 JSON, 图片字节或 None)
        本方法会抛异常；重连由调用方负责。
        """
        path = "/ISAPI/Event/notification/alertStream"
        # 读超时给长一点：没事件时连接是静默的，不能因为这个断开
        resp = self._request("GET", path, stream=True, read_timeout=self.picture_timeout)

        boundary = BOUNDARY_FALLBACK
        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype, re.I)
        if m:
            boundary = m.group(1).strip().strip('"').encode()

        if on_connect:
            on_connect()

        buffer = b""
        assert resp.fp is not None
        while not should_stop():
            chunk = resp.fp.read(8192)
            if not chunk:
                raise ConnectionError("alertStream 连接被对端关闭")
            buffer += chunk
            parts, buffer = split_parts(buffer, boundary)
            for part in parts:
                head, body, obj = parse_part(part)
                if obj is None:
                    # 二进制 part：联动抓拍图
                    if "image" in head.lower() or body[:2] == b"\xff\xd8":
                        on_event({}, body)
                    continue
                on_event(obj, None)
        resp.close()


# ---------------------------------------------------------------------------
# 事件流守护线程：自动重连
# ---------------------------------------------------------------------------

class StreamWorker:
    """
    alertStream 长连接守护线程。

    退避要足够保守：设备只允许一条 alertStream 会话，而且因为它是"有事件才写数据"，
    客户端被强杀后设备要过很久才发现并释放会话。因此每失败一次都可能把设备的
    会话额度占得更死——快速重试只会让情况更糟。
    """

    def __init__(self, client: DeviceClient, on_raw_event: Callable[[dict, bytes | None], None],
                 backoff_min: float = 5.0, backoff_max: float = 300.0):
        self.client = client
        self.on_raw_event = on_raw_event
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.last_error: str | None = None
        self.reconnects = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="alertStream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = self.backoff_min
        while not self._stop.is_set():
            try:
                self.client.stream_events(
                    self.on_raw_event, self._stop.is_set,
                    on_connect=self._on_connect,
                )
            except Exception as e:                      # noqa: BLE001 - 任何异常都重连
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("alertStream 断开(%s)，%.1fs 后重连", self.last_error, backoff)
            else:
                self.connected = False

            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self.backoff_max)
            self.reconnects += 1
            if self.connected:
                backoff = self.backoff_min
        self.connected = False
        log.info("alertStream 线程退出")

    def _on_connect(self) -> None:
        self.connected = True
        self.last_error = None
        log.info("alertStream 已连接")


def tcp_reachable(host: str, port: int = 80, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
