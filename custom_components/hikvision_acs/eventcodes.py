"""
事件码表与归一化。

全部语义均由真机 DS-K1T341BM（固件 V3.7.80）实测得出。改动前请先读本文件的说明。

三条硬性规则：

1. 查表必须用 (major, minor) 二元组。
   设备通过 GET /ISAPI/Event/notification/httpHosts/capabilities 自述了它可能上报的
   事件码，按大类分别列出——同一个 minor 会出现在多个大类里（例如 0x400 同时属于
   「异常」和「操作」）。只按 minor 查表一定会误判。

2. 判断「用什么方式开门」只能看 minor，不能看 currentVerifyMode。
   实测：人脸认证通过时 minor=75，而 currentVerifyMode 显示 "cardOrfaceOrPw"——
   那是设备配置的认证模式，不是本次实际使用的方式。

3. 未确认语义的码一律不参与业务判断，只在界面上标记出来。
   宁可显示"未知"，也不要把门铃误判成开门。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# 事件大类
MAJOR_ALARM = 1        # 报警
MAJOR_EXCEPTION = 2    # 异常
MAJOR_OPERATION = 3    # 操作
MAJOR_EVENT = 5        # 事件

MAJOR_NAMES = {
    MAJOR_ALARM: "报警",
    MAJOR_EXCEPTION: "异常",
    MAJOR_OPERATION: "操作",
    MAJOR_EVENT: "事件",
}

# HA 事件类型（与 const.EVENT_TYPES 对应）
#
# 这里刻意用字面量而不是 import const，好让本模块保持零依赖、可单独测试。
EV_DOORBELL = "doorbell"
EV_ACCESS_GRANTED = "access_granted"
EV_ACCESS_DENIED = "access_denied"
EV_DOOR_OPENED = "door_opened"
EV_DOOR_CLOSED = "door_closed"
EV_EXIT_BUTTON = "exit_button"
EV_REMOTE_OPEN = "remote_open"


@dataclass(frozen=True)
class Code:
    """一条事件码的语义。"""

    name: str
    ha_event: str | None = None       # 映射到 HA 事件实体的类型；None = 不触发
    method: str | None = None         # 归一化后的开门方式
    actor_kind: str = "unknown"       # person / anonymous / system / unknown
    opens_door: bool = False          # 是否表示"门被打开了"
    closes_door: bool = False
    verified: bool = False            # 语义是否已在本机实测确认


# ---------------------------------------------------------------------------
# 已在本机实测确认语义的事件码
# ---------------------------------------------------------------------------
VERIFIED: dict[tuple[int, int], Code] = {
    # —— 认证类：带身份，是「谁用什么方式开门」的答案 ——
    (5, 75): Code("人脸认证通过", ha_event=EV_ACCESS_GRANTED, method="人脸",
                  actor_kind="person", opens_door=True, verified=True),

    # —— 门禁 I/O 类：无身份 ——
    (5, 21): Code("门锁打开", ha_event=EV_DOOR_OPENED, actor_kind="system",
                  opens_door=True, verified=True),
    (5, 22): Code("门锁关闭", ha_event=EV_DOOR_CLOSED, actor_kind="system",
                  closes_door=True, verified=True),
    (5, 23): Code("开门按钮按下", ha_event=EV_EXIT_BUTTON, method="开门按钮",
                  actor_kind="anonymous", opens_door=True, verified=True),
    (5, 24): Code("开门按钮松开", actor_kind="system", verified=True),

    # —— 门铃：现场按「呼叫」键实测确认 ——
    (5, 37): Code("门铃响", ha_event=EV_DOORBELL, actor_kind="anonymous", verified=True),

    # —— 远程开门：实测每次远程开门后紧跟 (5,21) 门锁打开 ——
    (3, 1024): Code("远程开门", ha_event=EV_REMOTE_OPEN, method="远程开门",
                    actor_kind="anonymous", opens_door=True, verified=True),
}

# ---------------------------------------------------------------------------
# 公开文档给出、但本机尚未实测到的码。
# 本机没有卡，刷卡类无法现场验证；接入真实卡后必须回来核对，确认无误再把
# verified 改成 True（或者移到 VERIFIED 里）。
# ---------------------------------------------------------------------------
UNVERIFIED: dict[tuple[int, int], Code] = {
    (5, 1): Code("合法卡刷卡", ha_event=EV_ACCESS_GRANTED, method="刷卡",
                 actor_kind="person", opens_door=True),
    (5, 2): Code("卡+密码", ha_event=EV_ACCESS_GRANTED, method="刷卡+密码",
                 actor_kind="person", opens_door=True),
    (5, 3): Code("卡+指纹", ha_event=EV_ACCESS_GRANTED, method="刷卡+指纹",
                 actor_kind="person", opens_door=True),
    (5, 4): Code("指纹", ha_event=EV_ACCESS_GRANTED, method="指纹",
                 actor_kind="person", opens_door=True),
    (5, 38): Code("指纹比对通过", ha_event=EV_ACCESS_GRANTED, method="指纹",
                  actor_kind="person", opens_door=True),
    (5, 39): Code("指纹比对失败", ha_event=EV_ACCESS_DENIED, actor_kind="person"),
    (5, 76): Code("人脸认证失败", ha_event=EV_ACCESS_DENIED, actor_kind="person"),
    (5, 10): Code("多重认证通过", ha_event=EV_ACCESS_GRANTED, actor_kind="person",
                  opens_door=True),
    (5, 151): Code("密码错误", ha_event=EV_ACCESS_DENIED, actor_kind="person"),
    # 远程关门属于「操作」大类，不是「事件」大类——0x401 只出现在设备的
    # minorOperation 清单里，放进 major=5 会被校验脚本判为"设备不会发的码"。
    (3, 1025): Code("远程关门", method="远程关门", actor_kind="anonymous",
                    closes_door=True),
}

# ---------------------------------------------------------------------------
# 设备声明可上报、但语义尚未确认的码。
# 故意不给 ha_event 也不给 method —— 它们不会触发任何 HA 事件，也不会被当成开门，
# 只作为传感器状态呈现，等有确凿证据再定义。
#
# 已知线索（来自真机观测，未定论）：
#   (3,112)/(3,113) 成对出现，报文带 remoteHostAddr，疑似远程客户端操作；
#   (3,240)/(3,80)  单独出现；
#   (2,1024)/(1,1028) 在历史数据里紧跟在「门锁关闭」之后。
# ---------------------------------------------------------------------------
UNIDENTIFIED: dict[tuple[int, int], str] = {
    (3, 112): "未确认（成对出现，带远程地址）",
    (3, 113): "未确认（成对出现，带远程地址）",
    (3, 240): "未确认（操作类）",
    (3, 80): "未确认（操作类）",
    (2, 1024): "未确认（异常类）",
    (1, 1028): "未确认（报警类）",
}

ALL_CODES: dict[tuple[int, int], Code] = {**VERIFIED, **UNVERIFIED}

# ---------------------------------------------------------------------------
# 设备自述的可上报事件码清单。
# 来源：GET /ISAPI/Event/notification/httpHosts/capabilities
# 用于区分「设备会发但语义没搞清」与「设备根本不该发这个码」——后者往往意味着
# 固件行为变了，值得告警而不是静默处理。
# ---------------------------------------------------------------------------
DECLARED: dict[int, frozenset[int]] = {
    MAJOR_ALARM: frozenset({
        0x404, 0x405, 0x40A, 0x40B, 0x40C, 0x40D, 0x40E,
        0x406, 0x407, 0x40F, 0x410,
    }),
    MAJOR_EXCEPTION: frozenset({
        0x26, 0x27, 0x400, 0x407, 0x408, 0x423, 0x424, 0x428, 0x429,
        0x409, 0x40A, 0x40F, 0x410,
    }),
    MAJOR_OPERATION: frozenset({
        0x50, 0x5A, 0x5D, 0x5E, 0x70, 0x71, 0x76, 0x77, 0x79, 0x7A, 0x7B,
        0x7E, 0x86, 0x87, 0xF0, 0xF1, 0x137, 0x138, 0x400, 0x401, 0x402,
        0x403, 0x404, 0x405, 0x406, 0x407, 0x40A, 0x40B, 0x40C, 0x40E,
        0x419, 0x41A, 0x421, 0x422, 0x42F, 0x430, 0x431, 0x432, 0x433,
        0x2601, 0x41F, 0x420, 0x436,
    }),
    MAJOR_EVENT: frozenset({
        0x1, 0x6, 0x7, 0x8, 0x9, 0xA, 0xB, 0xC, 0xD, 0xE, 0xF,
        0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A,
        0x1B, 0x1C, 0x1F, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,
        0x31, 0x4B, 0x4C, 0x50, 0x68, 0x74, 0x75, 0x82, 0x84, 0x8E, 0x97,
        0x98, 0x9B, 0xA4, 0xC1, 0xD0,
        0x2, 0x3, 0x4, 0x5, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E, 0x2F,
        0x30, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
        0x40, 0x41, 0x42, 0x43, 0x44, 0x99, 0x9A, 0xA8, 0x25, 0x33, 0x9C,
        0x9D, 0xD9, 0xA2, 0xA9, 0x77, 0xA3, 0xAA, 0xB5,
    }),
}


def is_declared(major: int, minor: int) -> bool:
    """该 (major, minor) 是否在设备自述的可上报范围内。"""
    return minor in DECLARED.get(major, frozenset())


def describe(major: int, minor: int) -> tuple[str, bool]:
    """返回 (事件名称, 语义是否已实测确认)。"""
    code = ALL_CODES.get((major, minor))
    if code is not None:
        return code.name, code.verified
    known_unknown = UNIDENTIFIED.get((major, minor))
    if known_unknown is not None:
        return known_unknown, False
    if is_declared(major, minor):
        return f"语义未确认 (major={major}, minor={minor}/0x{minor:x})", False
    return f"设备未声明的码 (major={major}, minor={minor}/0x{minor:x})", False


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _xml_to_event(text: str) -> dict | None:
    """把设备推来的 XML 转成与 JSON 同构的 dict。"""
    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    out: dict = {}
    for child in root:
        tag = local(child.tag)
        if tag == "AccessControllerEvent":
            out[tag] = {local(g.tag): g.text for g in child}
        else:
            out[tag] = child.text
    return out or None


def parse_notification(body: bytes) -> dict | None:
    """
    解析设备推来的一条报文。

    该固件不认 parameterFormatType 字段（能力清单里也没有它），所以报文到底是
    JSON 还是 XML 不能假设——两种都解析。
    """
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return None
    if text[0] == "{":
        try:
            return json.loads(text)
        except ValueError:
            return None
    if text[0] == "<":
        return _xml_to_event(text)
    return None


@dataclass
class NormalizedEvent:
    """一条归一化后的门禁事件。"""

    serial_no: int | None = None
    front_serial_no: int | None = None
    time: str | None = None
    live: bool = False
    major: int = 0
    minor: int = 0
    major_name: str = ""
    event_name: str = ""
    code_verified: bool = False
    declared: bool = False
    ha_event: str | None = None
    method: str | None = None
    actor_kind: str = "unknown"
    opens_door: bool = False
    closes_door: bool = False
    person: str | None = None
    person_name: str | None = None
    card_no: str | None = None
    door_no: int | None = None
    remote_host: str | None = None
    # 仅留档：这是设备"配置的"认证模式，不能用来判断本次开门方式
    configured_verify_mode: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def dedup_key(self) -> str | None:
        """去重键。serial_no 是设备侧全局递增的事件流水号。"""
        return str(self.serial_no) if self.serial_no is not None else None

    def as_attributes(self) -> dict:
        """给 HA 实体用的属性字典（去掉 None，避免属性爆炸）。"""
        out = {
            "event_name": self.event_name,
            "major": self.major,
            "minor": self.minor,
            "code_verified": self.code_verified,
        }
        for key, value in (
            ("time", self.time),
            ("method", self.method),
            ("person", self.person),
            ("person_name", self.person_name),
            ("card_no", self.card_no),
            ("door", self.door_no),
            ("remote_host", self.remote_host),
            ("serial_no", self.serial_no),
        ):
            if value not in (None, ""):
                out[key] = value
        return out


def normalize(raw: dict) -> NormalizedEvent | None:
    """
    把设备原始报文归一化成 NormalizedEvent。

    非门禁事件返回 None。字段值可能是 JSON 的数字，也可能是 XML 解析出的字符串。
    """
    if raw.get("eventType") != "AccessControllerEvent":
        return None

    ace = raw.get("AccessControllerEvent") or {}
    major = _to_int(ace.get("majorEventType"))
    minor = _to_int(ace.get("subEventType"))
    if major is None or minor is None:
        return None

    code = ALL_CODES.get((major, minor))
    name, verified = describe(major, minor)

    person = (ace.get("employeeNoString") or "").strip()
    person_name = (ace.get("name") or "").strip()
    card_no = (ace.get("cardNo") or "").strip()

    return NormalizedEvent(
        serial_no=_to_int(ace.get("serialNo")),
        front_serial_no=_to_int(ace.get("frontSerialNo")),
        time=raw.get("dateTime"),
        live=bool(ace.get("currentEvent")),
        major=major,
        minor=minor,
        major_name=MAJOR_NAMES.get(major, str(major)),
        event_name=name,
        code_verified=verified,
        declared=is_declared(major, minor),
        ha_event=code.ha_event if code else None,
        method=code.method if code else None,
        actor_kind=code.actor_kind if code else "unknown",
        opens_door=code.opens_door if code else False,
        closes_door=code.closes_door if code else False,
        person=person or person_name or None,
        person_name=person_name or None,
        card_no=card_no or None,
        door_no=_to_int(ace.get("doorNo")),
        remote_host=(ace.get("remoteHostAddr") or "").strip() or None,
        configured_verify_mode=ace.get("currentVerifyMode"),
    )
