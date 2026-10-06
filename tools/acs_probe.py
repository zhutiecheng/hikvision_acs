#!/usr/bin/env python3
"""
acs_probe.py — 海康门禁（DS-K1T341BM 等）ISAPI 探测与真实事件采样工具

用途：在写业务代码之前，先把"设备到底支持什么、真实事件长什么样"问清楚。
     事件码映射表必须用本工具从真机采样得出，不要照抄任何文档。

依赖：requests （本机已装）

用法：
    python3 tools/acs_probe.py --host <设备IP> --user admin --password 'xxx' info
    python3 tools/acs_probe.py ... hosts      # 看 HTTP 监听配置是否支持
    python3 tools/acs_probe.py ... sniff -t 120   # 采样真实事件，产出 events.jsonl
    python3 tools/acs_probe.py ... rtsp       # 验证 RTSP 取流
    python3 tools/acs_probe.py ... history    # 查历史门禁事件
    python3 tools/acs_probe.py ... open --yes # 远程开门（会真的开门！）

凭据也可用环境变量：ACS_HOST / ACS_USER / ACS_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    from requests.auth import HTTPDigestAuth
except ImportError:
    sys.exit("缺少依赖：pip3 install requests")

# Hikvision 官方事件码表（Access Control Event Types 附录）中的关键项。
# 标记 assumed 的条目必须用真机采样确认后再用于业务判断。
SUB_EVENT_HINTS: dict[int, str] = {
    1: "合法卡刷卡",
    21: "门锁打开",
    22: "门锁关闭",
    23: "开门按钮按下",
    24: "开门按钮松开",
    25: "门开(门磁)",
    26: "门关(门磁)",
    27: "门异常打开",
    28: "门开超时",
    37: "门铃响",
    38: "指纹比对通过",
    39: "指纹比对失败",
    51: "呼叫中心",
    75: "人脸认证通过",
    76: "人脸认证失败(推断)",
    151: "密码错误",
    133: "尾随",
    134: "反向通行",
    135: "强行闯入",
    1024: "远程开门",
    1025: "远程关门",
    1026: "远程常开",
    1027: "远程常闭",
}

# 在真机 DS-K1T341BM / 固件 V3.7.80 上实测确认的 (major, minor) → 含义。
#
# 为什么必须用 (major, minor) 二元组而不是单看 minor：
#   实测发现 minor 相同、major 不同时含义完全不同（例如 1024 在 major=5 下是"远程开门"，
#   在本机实测的 major=2 下却是另一类事件）。只按 minor 查表会误判。
EVENT_CODES: dict[tuple[int, int], str] = {
    (5, 21): "门锁打开",
    (5, 22): "门锁关闭",
    (5, 23): "开门按钮按下",
    (5, 24): "开门按钮松开",
    (5, 37): "门铃响(待本机确认)",
    (5, 75): "人脸认证通过(带身份+抓拍图)",
    (5, 76): "人脸认证失败(待确认)",
    # 以下三条由本机实测采到，用途尚未确认，不要臆测后写进业务逻辑
    (2, 1024): "【未确认】异常类，实测总在门锁关闭后紧邻出现",
    (1, 1028): "【未确认】报警类，实测总在门锁关闭后 1 秒出现",
    (3, 240): "【未确认】操作类",
}


def die(msg: str) -> None:
    print(f"[错误] {msg}", file=sys.stderr)
    raise SystemExit(1)


class Device:
    def __init__(self, host: str, user: str, password: str, https: bool = False, timeout: int = 10):
        self.host = host
        self.timeout = timeout
        scheme = "https" if https else "http"
        self.base = f"{scheme}://{host}"
        self.auth = HTTPDigestAuth(user, password)
        self.session = requests.Session()
        self.session.auth = self.auth

    def url(self, path: str) -> str:
        return self.base + path

    def get(self, path: str, **kw):
        return self.session.get(self.url(path), timeout=self.timeout, **kw)

    def put(self, path: str, data=None, **kw):
        return self.session.put(self.url(path), data=data, timeout=self.timeout, **kw)

    def post(self, path: str, data=None, **kw):
        return self.session.post(self.url(path), data=data, timeout=self.timeout, **kw)


def pretty_xml(text: str, max_len: int = 4000) -> str:
    text = text.strip()
    if len(text) > max_len:
        text = text[:max_len] + f"\n... (截断，共 {len(text)} 字符)"
    return text


# ---------------------------------------------------------------- info

def cmd_info(dev: Device, args) -> None:
    print("=" * 72)
    print("设备信息")
    print("=" * 72)
    r = dev.get("/ISAPI/System/deviceInfo")
    print(f"GET /ISAPI/System/deviceInfo -> HTTP {r.status_code}")
    print(pretty_xml(r.text))

    print()
    print("=" * 72)
    print("视频通道（RTSP 取流用）")
    print("=" * 72)
    r = dev.get("/ISAPI/Streaming/channels")
    print(f"GET /ISAPI/Streaming/channels -> HTTP {r.status_code}")
    print(pretty_xml(r.text, 3000))

    print()
    print("=" * 72)
    print("门禁事件查询能力")
    print("=" * 72)
    r = dev.get("/ISAPI/AccessControl/AcsEvent/capabilities")
    print(f"GET /ISAPI/AccessControl/AcsEvent/capabilities -> HTTP {r.status_code}")
    print(pretty_xml(r.text, 3000))


# ---------------------------------------------------------------- hosts

def cmd_hosts(dev: Device, args) -> None:
    print("=" * 72)
    print("HTTP 监听（事件推送）配置")
    print("=" * 72)
    r = dev.get("/ISAPI/Event/notification/httpHosts")
    print(f"GET /ISAPI/Event/notification/httpHosts -> HTTP {r.status_code}")
    body = pretty_xml(r.text, 6000)
    print(body)

    print()
    if r.status_code == 200 and "HttpHostNotification" in r.text:
        print(">>> 设备支持 HTTP 监听：可以让设备主动 POST 事件到你的服务")
        print(">>> 若 url 为空，说明尚未配置推送目标")
    else:
        print(">>> 未拿到 HTTP 监听配置。设备可能不支持该特性，或需改用 alertStream 长连接。")
        print(">>> 这种情况请用 `sniff` 子命令走 alertStream 方案。")


# ---------------------------------------------------------------- sniff

def parse_multipart_parts(buffer: bytes, boundary: bytes):
    """
    从缓冲区里切出完整的 part，返回 (完整part列表, 剩余缓冲区)。

    两个必须处理的边界情况：
      - 流以 --boundary 开头，split 会产生一个前导空块，要丢掉；
      - 流以 --boundary-- 收尾（closing delimiter），此时后面不再有 part，
        剩余缓冲应清空而不是当成半个 part 留着。

    判定 closing delimiter 时**不能只看是否以 "--" 开头**：
    网络分片可能正好切在 "--boundary" 中间，此时剩余缓冲区是 "\r\n--M…"，
    只按前缀判断会把它误认成结束符并丢弃，导致分隔符残字混进下一个 part 的头部。
    单一个 "--" 既可能是结束符，也可能是下一个分隔符的前两字节，因此只有
    当 "--" 后面确实跟了行结束符时才能确认结束。
    """
    delim = b"--" + boundary
    chunks = buffer.split(delim)
    remainder = chunks.pop()

    complete = [c for c in chunks if c.strip(b"\r\n")]

    stripped = remainder.lstrip(b"\r\n")
    if stripped == b"" or stripped.startswith(b"--\r") or stripped.startswith(b"--\n"):
        return complete, b""      # closing delimiter，流已结束
    return complete, remainder


def parse_event_part(part: bytes):
    """解析单个 part，返回 (headers文本, 原始body, 解析后的JSON或None)。"""
    part = part.strip(b"\r\n")
    if not part:
        return None
    if b"\r\n\r\n" in part:
        head, body = part.split(b"\r\n\r\n", 1)
    else:
        head, body = b"", part
    head_txt = head.decode("utf-8", "replace")
    body = body.rstrip(b"\r\n")

    obj = None
    if body[:1] in (b"{", b"["):
        try:
            obj = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            obj = None
    elif body[:1] == b"<":
        obj = {"_xml": body.decode("utf-8", "replace")[:2000]}
    return head_txt, body, obj


def describe_event(obj: dict) -> str:
    """把一条事件压成一行人类可读的摘要。"""
    if "_xml" in obj:
        return f"XML 事件: {obj['_xml'][:160]}"

    et = obj.get("eventType", "?")
    ts = obj.get("dateTime", "")

    if et == "AccessControllerEvent":
        ace = obj.get("AccessControllerEvent", {})
        major = ace.get("majorEventType")
        minor = ace.get("subEventType")
        who = ace.get("name") or ace.get("employeeNoString") or ace.get("cardNo") or "(无身份信息)"
        hint = EVENT_CODES.get((major, minor)) or SUB_EVENT_HINTS.get(minor, "未知事件码")
        live = "实时" if ace.get("currentEvent") else "历史"
        return (f"[{live}] {ts} | major={major} minor={minor} ({hint}) "
                f"| 人={who} | door={ace.get('doorNo')} "
                f"| verifyMode={ace.get('currentVerifyMode')}")

    return f"{ts} | {et} | {json.dumps(obj, ensure_ascii=False)[:200]}"


def cmd_sniff(dev: Device, args) -> None:
    out_path = Path(args.out)
    print("=" * 72)
    print(f"alertStream 原始事件采样（{args.time} 秒）")
    print("=" * 72)
    print(">>> 采样期间请到设备上做这些动作，覆盖各类事件：")
    print(">>>   1) 刷一次已注册的卡")
    print(">>>   2) 人脸识别一次")
    print(">>>   3) 按一次开门按钮")
    print(">>>   4) 按一次屏幕上的「呼叫」/门铃")
    print(">>>   5) 从软件远程开一次门")
    print()

    url = dev.url("/ISAPI/Event/notification/alertStream")
    # 读超时设短一点：流空闲时 iter_content 会一直阻塞，只有让它周期性抛
    # ReadTimeout，下面的 deadline 检查才有机会执行（否则 -t 形同虚设）。
    try:
        r = dev.session.get(url, stream=True, timeout=(10, 5))
    except Exception as e:
        die(f"连接 alertStream 失败: {e}")

    if r.status_code != 200:
        die(f"alertStream 返回 HTTP {r.status_code}: {r.text[:500]}")

    ctype = r.headers.get("Content-Type", "")
    m = re.search(r"boundary=([^;]+)", ctype, re.I)
    if not m:
        print(f"[警告] Content-Type 里没有 boundary: {ctype!r}")
        print("       回退到 MIME_boundary")
        boundary = b"MIME_boundary"
    else:
        boundary = m.group(1).strip().strip('"').encode()

    print(f"已连接。Content-Type={ctype}")
    print(f"boundary={boundary.decode(errors='replace')}")
    print(f"原始 part 落盘到: {out_path.resolve()}")
    print("-" * 72)

    out = open(out_path, "w", encoding="utf-8")
    pic_dir = Path(args.pic_dir)
    pic_dir.mkdir(parents=True, exist_ok=True)
    buffer = b""
    n_events = 0
    n_parsed = 0
    n_binary = 0
    n_live = 0
    n_history = 0
    deadline = time.time() + args.time

    def say(msg: str) -> None:
        print(msg, flush=True)

    try:
        while time.time() < deadline:
            try:
                chunk = next(r.iter_content(chunk_size=8192))
            except requests.exceptions.ReadTimeout:
                continue          # 空闲是正常的，回到上面重新检查 deadline
            except StopIteration:
                break
            if not chunk:
                continue
            buffer += chunk
            parts, buffer = parse_multipart_parts(buffer, boundary)
            for part in parts:
                parsed = parse_event_part(part)
                if parsed is None:
                    continue
                n_events += 1
                head_txt, body, obj = parsed

                # 二进制 part 就是联动抓拍图，直接存盘
                if obj is None:
                    n_binary += 1
                    saved = ""
                    if b"image/jpeg" in head_txt.encode() or b"image" in head_txt.encode():
                        name = f"pic-{datetime.now().strftime('%H%M%S')}-{n_binary}.jpg"
                        (pic_dir / name).write_bytes(body)
                        saved = f" -> {pic_dir}/{name}"
                    say(f"  [图片] {len(body)} 字节{saved}")
                    out.write(json.dumps({
                        "recv_at": datetime.now(timezone.utc).isoformat(),
                        "part_headers": head_txt, "body_len": len(body),
                        "body_utf8": None, "json": None,
                    }, ensure_ascii=False) + "\n")
                    out.flush()
                    continue

                ace = obj.get("AccessControllerEvent", {}) if obj.get("eventType") == "AccessControllerEvent" else {}
                is_live = bool(ace.get("currentEvent"))
                if is_live:
                    n_live += 1
                n_parsed += 1

                # 原样落盘，绝不丢信息
                out.write(json.dumps({
                    "recv_at": datetime.now(timezone.utc).isoformat(),
                    "part_headers": head_txt,
                    "body_len": len(body),
                    "body_utf8": body.decode("utf-8", "replace"),
                    "json": obj,
                }, ensure_ascii=False) + "\n")
                out.flush()

                # 历史事件在连接瞬间会一次性倒出来，打一行就够，避免刷屏
                if is_live or args.verbose_history:
                    say(f"  {describe_event(obj)}")
                else:
                    n_history += 1
    except KeyboardInterrupt:
        print("\n[中断] 提前结束采样")
    finally:
        out.close()
        r.close()

    print("-" * 72)
    print(f"采样结束：{n_parsed} 条结构化事件（{n_live} 条实时 / {n_history} 条历史补给），"
          f"{n_binary} 张图片")
    print(f"原始数据：{out_path.resolve()}")
    print(f"抓拍图：  {pic_dir.resolve()}")

    if n_parsed == 0:
        print()
        print("⚠️  没有采到结构化事件。排查顺序：")
        print("   1) 设备是否已激活并设置了正确的系统时间（时间错会导致事件时间戳异常）")
        print("   2) 采样期间是否真的触发了事件")
        print("   3) 该固件是否改用 httpHosts 推送（试 `hosts` 子命令）")
        return

    print()
    print("=" * 72)
    print("本次采到的 (major, minor) 组合 —— 请据此固化你的事件码表")
    print("=" * 72)
    seen: dict[tuple, int] = {}
    for line in out_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        obj = rec.get("json")
        if not isinstance(obj, dict) or obj.get("eventType") != "AccessControllerEvent":
            continue
        ace = obj["AccessControllerEvent"]
        key = (ace.get("majorEventType"), ace.get("subEventType"))
        seen[key] = seen.get(key, 0) + 1
    for (major, minor), cnt in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        hint = EVENT_CODES.get((major, minor)) or SUB_EVENT_HINTS.get(minor, "未知")
        flag = "" if (major, minor) in EVENT_CODES else "   <<< 新码，需确认含义"
        print(f"  major={major:<4} minor={minor:<6} 出现 {cnt:>3} 次   {hint}{flag}")


# ---------------------------------------------------------------- rtsp

def cmd_rtsp(dev: Device, args) -> None:
    print("=" * 72)
    print("RTSP 取流验证")
    print("=" * 72)
    for ch, label in ((101, "主码流"), (102, "子码流")):
        url = f"rtsp://{args.user}:{args.password}@{args.host}:554/Streaming/Channels/{ch}"
        print(f"\n--- {label}: {url.replace(args.password, '***')} ---")
        try:
            p = subprocess.run(
                ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                 "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
                 "-of", "default=noprint_wrappers=1", "-timeout", "10000000", url],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            print("  未找到 ffprobe，跳过")
            return
        except subprocess.TimeoutExpired:
            print("  超时（30s）")
            continue
        if p.returncode == 0:
            print("  ✅ 可用")
            for line in p.stdout.strip().splitlines():
                print(f"     {line}")
        else:
            print(f"  ❌ 失败: {p.stderr.strip()[:300]}")


# ---------------------------------------------------------------- history

def cmd_history(dev: Device, args) -> None:
    print("=" * 72)
    print("历史门禁事件查询（用于对账与校准事件码表）")
    print("=" * 72)
    now = datetime.now()
    start = args.start or now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    end = args.end or now.strftime("%Y-%m-%dT%H:%M:%S+08:00")

    payload = {
        "AcsEventCond": {
            "searchID": "probe-1",
            "searchResultPosition": 0,
            "maxResults": 30,
            # 本机型固件 V3.7.80 强制要求 major，缺失会返回
            #   HTTP 400 / MessageParametersLack / errorMsg="major"
            "major": args.major,
            "minor": 0,
            "startTime": start,
            "endTime": end,
        }
    }
    major_names = {1: "报警", 2: "异常", 3: "操作", 5: "事件"}
    print(f"major={args.major} ({major_names.get(args.major, '?')})")
    print(f"时间范围: {start} ~ {end}")
    r = dev.post("/ISAPI/AccessControl/AcsEvent?format=json",
                 data=json.dumps(payload),
                 headers={"Content-Type": "application/json"})
    print(f"POST /ISAPI/AccessControl/AcsEvent -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:1000])
        return
    try:
        data = r.json()
    except Exception:
        print(r.text[:2000])
        return

    acs = data.get("AcsEvent", {})
    print(f"totalMatches={acs.get('totalMatches')} numOfMatches={acs.get('numOfMatches')} "
          f"status={acs.get('responseStatusStrg')}")
    print()
    for info in acs.get("InfoList", []) or []:
        major = info.get("major")
        minor = info.get("minor")
        hint = SUB_EVENT_HINTS.get(minor, "未知")
        print(f"  {info.get('time')} | major={major} minor={minor} ({hint}) "
              f"| 人={info.get('name') or info.get('employeeNoString') or '-'} "
              f"| card={info.get('cardNo') or '-'} "
              f"| verifyMode={info.get('currentVerifyMode')} "
              f"| pic={'有' if info.get('pictureURL') else '无'}")
    if acs.get("responseStatusStrg") == "MORE":
        print("\n  (还有更多，需翻页或切时间片)")


# ---------------------------------------------------------------- open

def cmd_open(dev: Device, args) -> None:
    if not args.yes:
        die("远程开门会真的把门打开。确认无误请加 --yes")

    door = args.door
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
           f'<cmd>open</cmd></RemoteControlDoor>')
    path = f"/ISAPI/AccessControl/RemoteControl/door/{door}"
    print(f"PUT {path}  cmd=open")
    r = dev.put(path, data=xml.encode("utf-8"),
                headers={"Content-Type": "application/xml"})
    print(f"-> HTTP {r.status_code}")
    print(pretty_xml(r.text, 2000))
    if r.status_code == 200:
        print(">>> 请求已接受。门是否真的开了，请现场确认。")
        print(">>> 同时看 `sniff` 是否收到 minor=1024 (远程开门) 事件。")


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="海康门禁 ISAPI 探测与事件采样")
    ap.add_argument("--host", default=os.environ.get("ACS_HOST"), help="设备 IP")
    ap.add_argument("--user", default=os.environ.get("ACS_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("ACS_PASSWORD"))
    ap.add_argument("--https", action="store_true", help="用 https（默认 http）")
    ap.add_argument("--timeout", type=int, default=10)

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="设备信息与能力").set_defaults(func=cmd_info)
    sub.add_parser("hosts", help="HTTP 监听配置").set_defaults(func=cmd_hosts)

    p = sub.add_parser("sniff", help="采样 alertStream 真实事件")
    p.add_argument("-t", "--time", type=int, default=60, help="采样秒数，默认 60")
    p.add_argument("-o", "--out", default="events.jsonl", help="原始事件落盘路径")
    p.add_argument("--pic-dir", default="data/pictures", help="联动抓拍图保存目录")
    p.add_argument("--verbose-history", action="store_true",
                   help="连历史补给事件也逐条打印（默认只汇总，避免刷屏）")
    p.set_defaults(func=cmd_sniff)

    sub.add_parser("rtsp", help="验证 RTSP 取流").set_defaults(func=cmd_rtsp)

    p = sub.add_parser("history", help="查询历史门禁事件")
    p.add_argument("--start", help="ISO 时间，默认今天 0 点")
    p.add_argument("--end", help="ISO 时间，默认现在")
    p.add_argument("--major", type=int, default=5,
                   help="事件大类：1=报警 2=异常 3=操作 5=事件（默认 5）")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("open", help="远程开门（会真的开门）")
    p.add_argument("--door", type=int, default=1, help="门号，默认 1")
    p.add_argument("--yes", action="store_true", help="确认执行")
    p.set_defaults(func=cmd_open)

    args = ap.parse_args()

    if not args.host:
        die("缺少设备 IP。用 --host <设备IP> 或设置环境变量 ACS_HOST")
    if not args.password:
        die("缺少密码。用 --password 'xxx' 或设置环境变量 ACS_PASSWORD")

    dev = Device(args.host, args.user, args.password, https=args.https, timeout=args.timeout)

    print(f"目标设备: {args.host}  用户: {args.user}")
    print()

    # 先做一次连通性与认证检查，避免后面报错难以定位
    try:
        r = dev.get("/ISAPI/System/deviceInfo")
    except requests.exceptions.ConnectTimeout:
        die(f"连接 {args.host} 超时。检查：设备是否在线 / 与本机是否同网段 / 防火墙")
    except requests.exceptions.ConnectionError as e:
        die(f"无法连接 {args.host}: {e}")
    if r.status_code == 401:
        die("认证失败（HTTP 401）。检查用户名密码；注意设备激活后默认用户是 admin")
    if r.status_code != 200:
        die(f"设备信息接口返回 HTTP {r.status_code}: {r.text[:300]}")
    print("✅ 连通性与 Digest 认证正常")
    print()

    t0 = time.time()
    args.func(dev, args)
    print(f"\n[耗时 {time.time() - t0:.1f}s]")


if __name__ == "__main__":
    main()
