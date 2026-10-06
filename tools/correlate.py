#!/usr/bin/env python3
"""
按时间轴对齐设备事件，用于把「某个操作」和「某些事件码」对应起来。

为什么用历史查询而不是实时采样：
    实测该设备只允许一条 alertStream 会话，且异常断开后长时间不释放，
    现场做诊断时很容易把设备搞成"后续连接全被拒"的状态。
    AcsEvent 历史查询是普通短请求，没有这个风险，设备缓存 15 万条事件，
    事后回查完全够用。

用法：
    python3 tools/correlate.py --host <设备IP> --minutes 10
    python3 tools/correlate.py --host <设备IP> --start "2026-10-06T18:00:00" --end "2026-10-06T18:10:00"
    python3 tools/correlate.py --host <设备IP> --minutes 10 --raw   # 附完整报文
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from acs_probe import Device, code_label, is_known, die  # noqa: E402

CST = "+08:00"

# 诊断时最需要看清楚的几个字段
WATCH_FIELDS = ("remoteHostAddr", "netUser", "doorNo", "cardReaderKind",
                "employeeNoString", "name", "cardNo", "currentVerifyMode")


def describe(major: int, minor: int) -> str:
    """事件码名称。语义确认与否由 code_label 统一标注，这里不再自己维护表。"""
    return code_label(major, minor)


def query(dev: Device, payload: dict) -> tuple[int, str]:
    """acs_probe.Device 的 post 返回的是 requests.Response，不是元组。"""
    resp = dev.post("/ISAPI/AccessControl/AcsEvent?format=json",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"})
    return resp.status_code, resp.text


def main() -> int:
    ap = argparse.ArgumentParser(description="事件时间轴对照")
    ap.add_argument("--host", default=os.environ.get("ACS_HOST"))
    ap.add_argument("--user", default=os.environ.get("ACS_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("ACS_PASSWORD"))
    ap.add_argument("--minutes", type=int, default=10, help="回看多少分钟")
    ap.add_argument("--start", help="起始时间（本地时间，不含时区）")
    ap.add_argument("--end", help="结束时间")
    ap.add_argument("--raw", action="store_true", help="附完整报文")
    args = ap.parse_args()

    if not args.host:
        die("缺少设备 IP：--host <设备IP> 或设置 ACS_HOST")
    if not args.password:
        die("缺少密码：--password '<密码>' 或设置 ACS_PASSWORD")

    now = datetime.now()
    if args.start:
        start = args.start
        end = args.end or now.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        start = (now - timedelta(minutes=args.minutes)).strftime("%Y-%m-%dT%H:%M:%S")
        end = now.strftime("%Y-%m-%dT%H:%M:%S")

    dev = Device(args.host, args.user, args.password)
    print(f"设备 {args.host}   时间窗 {start} ~ {end}")
    print("（注意：设备时区需与本机一致，否则时间轴会整体偏移）\n")

    rows: list[dict] = []
    for major in (5, 3, 1, 2):
        position = 0
        while True:
            payload = {
                "AcsEventCond": {
                    "searchID": "corr",
                    "searchResultPosition": position,
                    "maxResults": 30,
                    "major": major,
                    "minor": 0,
                    "startTime": start + CST,
                    "endTime": end + CST,
                }
            }
            code, text = query(dev, payload)
            if code != 200:
                print(f"  [警告] major={major} 查询失败 HTTP {code}: {text[:120]}")
                break
            acs = json.loads(text).get("AcsEvent") or {}
            infos = acs.get("InfoList") or []
            rows.extend(infos)
            if acs.get("responseStatusStrg") != "MORE" or not infos:
                break
            position += len(infos)
            if position > 300:
                break

    if not rows:
        print("该时间窗内没有事件。")
        return 0

    rows.sort(key=lambda r: (r.get("time") or "", r.get("serialNo") or 0))

    print(f"{'时间':<20}{'maj/min':<10}{'事件':<30}{'来源':<16}{'身份'}")
    print("-" * 100)
    for r in rows:
        major, minor = r.get("major"), r.get("minor")
        host = r.get("remoteHostAddr") or "—"
        who = (r.get("name") or r.get("employeeNoString")
               or r.get("cardNo") or "—")
        t = (r.get("time") or "")[11:19]
        print(f"{t:<20}{str(major)+'/'+str(minor):<10}"
              f"{describe(major, minor)[:28]:<30}{host:<16}{who}")

    print()
    print("=== 出现的 (major, minor) 汇总 ===")
    counts: dict[tuple, list] = {}
    for r in rows:
        counts.setdefault((r.get("major"), r.get("minor")), []).append(r)
    for (major, minor), items in sorted(counts.items(),
                                        key=lambda kv: (kv[0][0], kv[0][1])):
        hosts = {i.get("remoteHostAddr") for i in items if i.get("remoteHostAddr")}
        host_txt = f"  来源={','.join(sorted(hosts))}" if hosts else ""
        print(f"  ({major},{minor}) 0x{minor:<5x} x{len(items):<3} "
              f"{describe(major, minor)}{host_txt}")

    if args.raw:
        print("\n=== 各组合的完整报文样本 ===")
        shown = set()
        for r in rows:
            key = (r.get("major"), r.get("minor"))
            if key in shown:
                continue
            shown.add(key)
            fields = {k: r.get(k) for k in WATCH_FIELDS if r.get(k) not in (None, "")}
            print(f"\n--- {key} ---")
            print(json.dumps(fields, ensure_ascii=False, indent=2))
    else:
        print("\n（加 --raw 可看完整报文）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
