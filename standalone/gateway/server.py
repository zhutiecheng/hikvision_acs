#!/usr/bin/env python3
"""
门禁事件网关 + 中控屏服务。

职责：
  1. 订阅设备 alertStream（自动重连），把事件归一化、落库
  2. 用 AcsEvent 定期对账，补上连接断开期间漏掉的事件
  3. 通过 SSE 把实时事件推给中控屏
  4. 接收中控屏的开门指令，转发到设备 ISAPI
  5. 也接受设备主动 POST（HTTP 监听模式）作为备用入口

只依赖标准库。配置全部走环境变量。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import eventcodes
from device import DeviceClient, StreamWorker, parse_part, split_parts, tcp_reachable
from store import Store

logging.basicConfig(
    level=os.environ.get("ACS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("acs.gateway")

STATIC_DIR = Path(__file__).parent / "static"
CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class Config:
    def __init__(self) -> None:
        self.host = os.environ.get("ACS_HOST", "")
        if not self.host:
            raise SystemExit("必须设置环境变量 ACS_HOST（设备 IP）")
        self.user = os.environ.get("ACS_USER", "admin")
        self.password = os.environ.get("ACS_PASSWORD", "")
        self.door = int(os.environ.get("ACS_DOOR", "1"))
        self.http_port = int(os.environ.get("ACS_HTTP_PORT", "8080"))
        self.db_path = os.environ.get("ACS_DB", "data/gateway.db")
        self.go2rtc_base = os.environ.get("GO2RTC_BASE", "http://127.0.0.1:1984")
        self.go2rtc_stream = os.environ.get("GO2RTC_STREAM", "door")
        # 浏览器侧要访问的 go2rtc 地址；留空则按请求的 Host 自动推导（换端口到 1984）
        self.go2rtc_public = os.environ.get("GO2RTC_PUBLIC", "").rstrip("/")
        # 默认用子码流：门铃预览要的是起流快，不是清晰度
        self.rtsp_channel = os.environ.get("ACS_RTSP_CHANNEL", "102")
        self.doorbell_seconds = int(os.environ.get("DOORBELL_SECONDS", "30"))

        # 事件入口。httphost = 设备主动 POST（推荐）；stream = alertStream 长连接；
        # both = 两条都开（靠 serialNo 去重，但会更快把设备的单会话额度用掉）。
        self.ingest_mode = os.environ.get("ACS_INGEST_MODE", "httphost")
        # 本机对设备可见的地址，设备要靠它回连推送事件
        self.self_ip = os.environ.get("ACS_SELF_IP", "")

        if not self.password:
            raise SystemExit("必须设置环境变量 ACS_PASSWORD")


# ---------------------------------------------------------------------------
# SSE 广播
# ---------------------------------------------------------------------------

class Hub:
    """把事件推给所有已连接的中控屏。"""

    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                log.warning("订阅者队列已满，丢弃一条消息（屏幕可能卡住了）")

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------

class Gateway:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.device = DeviceClient(cfg.host, cfg.user, cfg.password)
        self.store = Store(cfg.db_path, cfg.host)
        self.hub = Hub()
        self.worker = StreamWorker(self.device, self._on_raw)
        self._last_serial_for_picture: int | None = None
        self._stop = threading.Event()
        self.started_at = time.time()
        self.http_listening_ok = False

    # -- 事件入口 ---------------------------------------------------------

    def local_ip_towards_device(self) -> str:
        """
        本机对设备可见的 IP。设备要主动回连过来，不能填 127.0.0.1。
        用一次 UDP connect 让内核选出正确的出口网卡，不会真的发包。
        """
        if self.cfg.self_ip:
            return self.cfg.self_ip
        import socket as _s
        try:
            with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as s:
                s.connect((self.cfg.host, 80))
                return s.getsockname()[0]
        except OSError:
            return ""

    def configure_http_listening(self) -> bool:
        """让设备把事件 POST 到本网关。"""
        ip = self.local_ip_towards_device()
        if not ip:
            log.error("无法确定本机对设备可见的 IP，跳过 HTTP 监听配置（可显式设置 ACS_SELF_IP）")
            return False
        code, text = self.device.set_http_host(1, ip, self.cfg.http_port, "/notify")
        if code != 200:
            log.error("配置 HTTP 监听失败 HTTP %s：%s", code, text[:200])
            return False
        code2, body = self.device.get_http_hosts()
        if code2 == 200 and f"<ipAddress>{ip}</ipAddress>" in body and "/notify" in body:
            log.info("HTTP 监听已配置：设备将把事件 POST 到 http://%s:%d/notify",
                     ip, self.cfg.http_port)
            return True
        log.error("HTTP 监听回读校验不通过")
        return False

    def fire_device_self_test(self) -> bool:
        """让设备立刻推一条测试报文，验证端到端链路。"""
        code, text = self.device.test_http_host(1)
        ok = code == 200
        log.log(logging.INFO if ok else logging.WARNING,
                "触发设备自检推送 -> HTTP %s", code)
        return ok

    def ingest_http(self, body: bytes, content_type: str) -> bool:
        """
        处理设备 HTTP 监听推来的一条报文。

        报文形态不确定（JSON / XML / 带抓拍图的 multipart），三种都处理：
        设备回读时 parameterFormatType 是空的，不能假设格式。
        """
        m = re.search(r"boundary=([^;]+)", content_type or "", re.I)
        if m:
            boundary = m.group(1).strip().strip('"').encode()
            parts, _ = split_parts(body, boundary)
            event_obj = None
            picture = None
            for part in parts:
                head, pbody, obj = parse_part(part)
                if obj is not None and event_obj is None:
                    event_obj = obj
                elif pbody[:2] == b"\xff\xd8" or "image" in head.lower():
                    picture = pbody
            if event_obj is None:
                log.warning("multipart 报文里没找到事件，%d 字节已忽略", len(body))
                return False
            rec = self.ingest(event_obj)
            target = self._last_serial_for_picture
            if picture and target is not None:
                ptype = "image/jpeg" if picture[:2] == b"\xff\xd8" else "application/octet-stream"
                if self.store.attach_picture(target, picture, ptype):
                    log.info("抓拍图已挂到事件 serialNo=%s（%d 字节）", target, len(picture))
                    self.hub.publish({"kind": "picture", "serialNo": target})
            return rec is not None

        obj = eventcodes.parse_notification(body)
        if obj is None:
            log.warning("无法解析设备推送的报文（%d 字节，Content-Type=%s）：%r",
                        len(body), content_type, body[:200])
            return False
        return self.ingest(obj) is not None

    def _on_raw(self, obj: dict, picture: bytes | None) -> None:
        """alertStream 回调。图片 part 紧跟在它所属的事件之后到达。"""
        if picture:
            target = self._last_serial_for_picture
            if target is None:
                log.warning("收到抓拍图但还没有对应事件，丢弃 %d 字节", len(picture))
                return
            ptype = "image/jpeg" if picture[:2] == b"\xff\xd8" else "application/octet-stream"
            if self.store.attach_picture(target, picture, ptype):
                log.info("抓拍图已挂到事件 serialNo=%s（%d 字节）", target, len(picture))
                self.hub.publish({"kind": "picture", "serialNo": target})
            return

        if not obj:
            return
        self.ingest(obj)

    def ingest(self, obj: dict) -> dict | None:
        """把一条原始报文落库并广播。已存在则返回 None。"""
        rec = eventcodes.normalize(obj)
        if rec is None:
            return None

        if not self.store.insert_event(rec, obj):
            return None       # 重复（对账与实时流会重叠）

        self._last_serial_for_picture = rec["serialNo"]

        if rec["eventName"] == "门铃响":
            log.info("🔔 门铃！serialNo=%s", rec["serialNo"])
            self.hub.publish({
                "kind": "doorbell",
                "stream": self.cfg.go2rtc_stream,
                "seconds": self.cfg.doorbell_seconds,
                "event": rec,
            })
            return rec

        suffix = ""
        if rec["method"]:
            suffix = f" 方式={rec['method']}"
        if rec["person"]:
            suffix += f" 人={rec['person']}"
        if rec["remoteHost"]:
            suffix += f" 来自={rec['remoteHost']}"
        if not rec["codeVerified"]:
            suffix += "  [事件码未确认]"
        log.info("事件 serialNo=%s %s(%s,%s)%s",
                 rec["serialNo"], rec["eventName"], rec["major"], rec["minor"], suffix)

        self.hub.publish({"kind": "event", "event": rec})
        return rec

    # -- 对账：补齐断连期间漏掉的事件 -------------------------------------

    def reconcile(self) -> int:
        """
        按时间窗回查设备事件表，用 serialNo 去重后补入。

        实时流可能因断线丢事件；设备的 serialNo 是全局递增流水号，
        回查再按主键去重即可安全补齐，重复插入会被忽略。
        """
        end = datetime.now(CST)
        start = end - timedelta(minutes=int(os.environ.get("ACS_RECONCILE_MINUTES", "10")))
        added = 0
        try:
            for major in (5, 3, 1, 2):
                data = self.device.query_events(
                    start.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    end.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                    major=major, max_results=50,
                )
                infos = (data.get("AcsEvent") or {}).get("InfoList") or []
                for info in infos:
                    rec = eventcodes.normalize({
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
                    if rec and self.store.insert_event(rec, info):
                        added += 1
        except Exception as e:                          # noqa: BLE001
            log.warning("对账失败：%s", e)
            return 0
        if added:
            log.info("对账补入 %d 条历史事件", added)
            self.hub.publish({"kind": "reconciled", "count": added})
        return added

    def _reconcile_loop(self) -> None:
        interval = int(os.environ.get("ACS_RECONCILE_SECONDS", "120"))
        while not self._stop.wait(interval):
            self.reconcile()

    # -- go2rtc -----------------------------------------------------------

    def register_go2rtc_stream(self) -> bool:
        """
        把设备 RTSP 注册到 go2rtc。

        走 API 而不是写 go2rtc 配置文件，这样设备密码只存在于本进程的环境变量里，
        不会落到磁盘上的第二个地方。
        """
        from urllib.parse import quote
        src = (f"rtsp://{self.cfg.user}:{self.cfg.password}@{self.cfg.host}:554"
               f"/Streaming/Channels/{self.cfg.rtsp_channel}")
        url = (f"{self.cfg.go2rtc_base}/api/streams"
               f"?name={quote(self.cfg.go2rtc_stream, safe='')}&src={quote(src, safe='')}")
        try:
            req = urllib.request.Request(url, method="PUT")
            try:
                urllib.request.urlopen(req, timeout=10).read()
            except urllib.error.HTTPError as e:
                # 这个版本的 go2rtc 对 PUT 会返回非 200，但流其实已经注册了，
                # 所以不能只看状态码，必须回读确认。
                log.debug("go2rtc PUT 返回 HTTP %s，回读确认", e.code)
            with urllib.request.urlopen(
                    f"{self.cfg.go2rtc_base}/api/streams", timeout=10) as r:
                streams = json.loads(r.read().decode())
            if self.cfg.go2rtc_stream in streams:
                log.info("go2rtc 已注册流 '%s' -> %s 通道 %s",
                         self.cfg.go2rtc_stream, self.cfg.host, self.cfg.rtsp_channel)
                return True
            log.error("go2rtc 注册后回读不到流 '%s'", self.cfg.go2rtc_stream)
            return False
        except Exception as e:                          # noqa: BLE001
            log.error("注册 go2rtc 流失败：%s（门铃推流将不可用）", e)
            return False

    def go2rtc_public_base(self, host_header: str | None) -> str:
        """中控屏浏览器该用哪个地址访问 go2rtc。"""
        if self.cfg.go2rtc_public:
            return self.cfg.go2rtc_public
        host = (host_header or "").split(":")[0]
        if not host:
            return self.cfg.go2rtc_base
        return f"http://{host}:1984"

    # -- 开门 -------------------------------------------------------------

    def open_door(self, source: str) -> dict:
        ok, detail = self.device.open_door(self.cfg.door)
        self.store.log_door_action(self.cfg.door, ok, detail, source)
        if ok:
            log.info("远程开门成功（来源：%s）", source)
        else:
            log.error("远程开门失败（来源：%s）：%s", source, detail)
        return {"ok": ok, "detail": detail, "door": self.cfg.door}

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        info = {}
        try:
            info = self.device.device_info()
            log.info("设备 %s 型号=%s 固件=%s",
                     self.cfg.host, info.get("model"), info.get("firmwareVersion"))
        except Exception as e:                          # noqa: BLE001
            log.error("读取设备信息失败：%s", e)

        if not tcp_reachable(self.cfg.host, 80):
            log.error("设备 %s:80 不可达，请检查网络", self.cfg.host)

        self.register_go2rtc_stream()

        if "httphost" in self.cfg.ingest_mode:
            self.http_listening_ok = self.configure_http_listening()
        if "stream" in self.cfg.ingest_mode:
            self.worker.start()
        else:
            log.info("事件入口：HTTP 监听（alertStream 长连接未启用）")

        threading.Thread(target=self._reconcile_loop, name="reconcile", daemon=True).start()
        log.info("网关已启动，中控屏地址 http://%s:%d/",
                 self.local_ip_towards_device() or "127.0.0.1", self.cfg.http_port)

    def stop(self) -> None:
        self._stop.set()
        self.worker.stop()

    def status(self) -> dict:
        return {
            "device": self.cfg.host,
            "ingestMode": self.cfg.ingest_mode,
            "httpListeningOk": self.http_listening_ok,
            "streamConnected": self.worker.connected,
            "streamReconnects": self.worker.reconnects,
            "lastStreamError": self.worker.last_error,
            "screens": self.hub.subscribers,
            "uptimeSec": int(time.time() - self.started_at),
            "stats": self.store.stats(),
        }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

def make_handler(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "acs-gateway"
        protocol_version = "HTTP/1.1"

        # -- 工具 ---------------------------------------------------------

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def log_message(self, fmt, *args):              # 降噪
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- 路由 ---------------------------------------------------------

        def do_GET(self):                               # noqa: N802
            path = self.path.split("?")[0]

            if path in ("/", "/index.html"):
                return self._static("index.html")

            if path == "/api/status":
                return self._json(gw.status())

            if path == "/api/config":
                return self._json({
                    "go2rtc": gw.go2rtc_public_base(self.headers.get("Host")),
                    "stream": gw.cfg.go2rtc_stream,
                    "doorbellSeconds": gw.cfg.doorbell_seconds,
                    "door": gw.cfg.door,
                })

            if path == "/api/events/recent":
                return self._json({"events": gw.store.recent_events(60)})

            if path.startswith("/api/picture/"):
                try:
                    sn = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad serialNo"}, 400)
                data, ptype = gw.store.get_picture(sn)
                if data is None:
                    return self._json({"error": "not found"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", ptype or "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return

            if path in ("/api/events/stream", "/api/stream"):
                return self._sse()

            return self._json({"error": "not found"}, 404)

        def do_POST(self):                              # noqa: N802
            path = self.path.split("?")[0]
            body = self._body()

            if path == "/api/door/open":
                src = self.headers.get("X-Source") or self.address_string()
                return self._json(gw.open_door(source=src))

            if path == "/notify":
                # HTTP 监听模式入口：设备主动 POST 事件到这里。
                # 这里必须兜住所有异常并回 200：回 5xx 会让设备不断重试同一批
                # 报文，把一条坏数据放大成重试风暴；解析失败只记录，不反馈给设备。
                try:
                    ok = gw.ingest_http(body, self.headers.get("Content-Type", ""))
                except Exception as e:                  # noqa: BLE001
                    log.exception("处理设备推送失败：%s", e)
                    ok = False
                return self._json({"accepted": ok})

            return self._json({"error": "not found"}, 404)

        # -- 静态文件 -----------------------------------------------------

        def _static(self, name: str) -> None:
            f = STATIC_DIR / name
            if not f.is_file():
                return self._json({"error": "missing static file"}, 404)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- SSE ----------------------------------------------------------

        def _sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            q = gw.hub.subscribe()
            log.info("中控屏已连接（当前 %d 个）", gw.hub.subscribers)

            def write_chunk(payload: bytes) -> None:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(payload), payload))
                self.wfile.flush()

            try:
                write_chunk(b": connected\n\n")
                while True:
                    try:
                        msg = q.get(timeout=15)
                    except queue.Empty:
                        write_chunk(b": keepalive\n\n")   # 心跳，防中间设备掐连接
                        continue
                    data = json.dumps(msg, ensure_ascii=False)
                    write_chunk(f"data: {data}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                gw.hub.unsubscribe(q)
                log.info("中控屏断开（剩 %d 个）", gw.hub.subscribers)

    return Handler


def main() -> None:
    cfg = Config()
    gw = Gateway(cfg)
    gw.start()
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.http_port), make_handler(gw))
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到中断，退出")
    finally:
        gw.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
