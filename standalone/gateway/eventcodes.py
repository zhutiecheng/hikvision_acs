"""
事件码表与归一化。

设计要点（全部由真机 DS-K1T341BM / 固件 V3.7.80 实测得出，不要凭文档改动）：

1. 查表必须用 (major, minor) 二元组。
   实测同一个 minor 在不同 major 下含义不同（本机 1024 在 major=2 和 major=3 下
   是两类事件），只按 minor 查表会误判。

2. 判断"通过什么方式开门"只能看 minor，**不能看 currentVerifyMode**。
   实测：人脸认证通过时 minor=75，而 currentVerifyMode 显示 "cardOrfaceOrPw"
   ——那是设备配置的认证模式，不是本次实际使用的方式。

3. 身份字段的优先级：employeeNoString（工号）> name（姓名）> cardNo（卡号）。
   门禁 I/O 类事件（门锁开关、开门按钮、门铃）没有身份，属正常。
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

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


class Code:
    """一条事件码的语义。"""

    __slots__ = ("name", "method", "actor_kind", "verified")

    def __init__(self, name: str, method: str | None = None,
                 actor_kind: str = "unknown", verified: bool = False):
        self.name = name
        # method: 归一化后的开门方式，用于"通过什么方式开门"
        self.method = method
        # actor_kind: person=有身份的人 / anonymous=无身份动作 / system=设备自身
        self.actor_kind = actor_kind
        # verified: 是否已在本机实测确认
        self.verified = verified


# ---------------------------------------------------------------------------
# 设备自己声明的可上报事件码。
#
# 来源：GET /ISAPI/Event/notification/httpHosts/capabilities
# 这是最权威的一份清单——由设备固件给出，决定了设备究竟可能发出哪些
# (major, minor)。文档和网上的对照表都可能过时或抄错，这份不会。
#
# 它按大类分别列出，且同一个 minor 会出现在多个大类里（例如 0x400 同时在
# "异常"和"操作"下）——这就是必须用 (major, minor) 二元组查表的固件级证据。
# ---------------------------------------------------------------------------
DECLARED: dict[int, set[int]] = {
    MAJOR_ALARM: {
        0x404, 0x405, 0x40a, 0x40b, 0x40c, 0x40d, 0x40e,
        0x406, 0x407, 0x40f, 0x410,
    },
    MAJOR_EXCEPTION: {
        0x26, 0x27, 0x400, 0x407, 0x408, 0x423, 0x424, 0x428, 0x429,
        0x409, 0x40a, 0x40f, 0x410,
    },
    MAJOR_OPERATION: {
        0x50, 0x5a, 0x5d, 0x5e, 0x70, 0x71, 0x76, 0x77, 0x79, 0x7a, 0x7b,
        0x7e, 0x86, 0x87, 0xf0, 0xf1, 0x137, 0x138, 0x400, 0x401, 0x402,
        0x403, 0x404, 0x405, 0x406, 0x407, 0x40a, 0x40b, 0x40c, 0x40e,
        0x419, 0x41a, 0x421, 0x422, 0x42f, 0x430, 0x431, 0x432, 0x433,
        0x2601, 0x41f, 0x420, 0x436,
    },
    MAJOR_EVENT: {
        0x1, 0x6, 0x7, 0x8, 0x9, 0xa, 0xb, 0xc, 0xd, 0xe, 0xf,
        0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a,
        0x1b, 0x1c, 0x1f, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,
        0x31, 0x4b, 0x4c, 0x50, 0x68, 0x74, 0x75, 0x82, 0x84, 0x8e, 0x97,
        0x98, 0x9b, 0xa4, 0xc1, 0xd0,
        0x2, 0x3, 0x4, 0x5, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f,
        0x30, 0x36, 0x37, 0x38, 0x39, 0x3a, 0x3b, 0x3c, 0x3d, 0x3e, 0x3f,
        0x40, 0x41, 0x42, 0x43, 0x44, 0x99, 0x9a, 0xa8, 0x25, 0x33, 0x9c,
        0x9d, 0xd9, 0xa2, 0xa9, 0x77, 0xa3, 0xaa, 0xb5,
    },
}


def is_declared(major: int, minor: int) -> bool:
    """该 (major, minor) 是否在设备声明可上报的范围内。"""
    return minor in DECLARED.get(major, set())


# ---------------------------------------------------------------------------
# 已在本机实测确认的事件码
# ---------------------------------------------------------------------------
VERIFIED: dict[tuple[int, int], Code] = {
    # —— 认证类：带身份，是"谁用什么方式开门"的答案 ——
    (5, 75): Code("人脸认证通过", method="人脸", actor_kind="person", verified=True),

    # —— 门禁 I/O 类：无身份 ——
    (5, 21): Code("门锁打开", actor_kind="system", verified=True),
    (5, 22): Code("门锁关闭", actor_kind="system", verified=True),
    (5, 23): Code("开门按钮按下", method="开门按钮", actor_kind="anonymous", verified=True),
    (5, 24): Code("开门按钮松开", actor_kind="system", verified=True),

    # —— 门铃：本机按「呼叫」键实测确认 ——
    (5, 37): Code("门铃响", actor_kind="anonymous", verified=True),

    # —— 远程开门：实测每次远程开门后紧跟 (5,21) 门锁打开 ——
    (3, 1024): Code("远程开门", method="远程开门", actor_kind="anonymous", verified=True),
}

# ---------------------------------------------------------------------------
# 文档给出、但本机尚未实测到的码。
# 本机没有卡，刷卡类事件无法在现场验证，接入真实卡后必须回来核对。
# ---------------------------------------------------------------------------
UNVERIFIED: dict[tuple[int, int], Code] = {
    (5, 1): Code("合法卡刷卡", method="刷卡", actor_kind="person"),
    (5, 2): Code("卡+密码", method="刷卡+密码", actor_kind="person"),
    (5, 3): Code("卡+指纹", method="刷卡+指纹", actor_kind="person"),
    (5, 4): Code("指纹", method="指纹", actor_kind="person"),
    (5, 38): Code("指纹比对通过", method="指纹", actor_kind="person"),
    (5, 39): Code("指纹比对失败", actor_kind="person"),
    (5, 76): Code("人脸认证失败", actor_kind="person"),
    (5, 10): Code("多重认证通过", actor_kind="person"),
    (5, 151): Code("密码错误", actor_kind="person"),
    (5, 1025): Code("远程关门", method="远程关门", actor_kind="anonymous"),
}

# ---------------------------------------------------------------------------
# 本机实测采到、但用途尚未确认的码。
# 故意不给 method，也不写进任何业务判断——先老老实实记录下来，等有证据再定义。
#
# 已知线索：
#   (3,112)/(3,113) 成对出现，带 remoteHostAddr 字段（实测为一台内网主机）；
#   (2,1024)/(1,1028) 在历史事件里紧跟在"门锁关闭"之后；
#   (3,240) 单独出现。
# ---------------------------------------------------------------------------
UNIDENTIFIED: dict[tuple[int, int], str] = {
    (3, 112): "未确认（成对出现，带 remoteHostAddr，疑似远程客户端操作）",
    (3, 113): "未确认（成对出现，带 remoteHostAddr，疑似远程客户端操作）",
    (3, 240): "未确认（操作类）",
    (2, 1024): "未确认（异常类，历史上紧跟门锁关闭出现）",
    (1, 1028): "未确认（报警类，历史上紧跟门锁关闭出现）",
}

ALL_CODES: dict[tuple[int, int], Code] = {**VERIFIED, **UNVERIFIED}


def lookup(major: int, minor: int) -> Code | None:
    return ALL_CODES.get((major, minor))


def describe(major: int, minor: int) -> tuple[str, bool]:
    """返回 (事件名称, 是否已实测确认)。"""
    code = ALL_CODES.get((major, minor))
    if code is not None:
        return code.name, code.verified
    unknown = UNIDENTIFIED.get((major, minor))
    if unknown is not None:
        return unknown, False
    if is_declared(major, minor):
        # 设备说它会发这个码，只是我们还没搞清它代表什么
        return f"语义未确认 major={major} minor={minor}(0x{minor:x})", False
    return f"设备未声明的码 major={major} minor={minor}(0x{minor:x})", False


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _xml_to_event(text: str) -> dict | None:
    """把设备 HTTP 监听推来的 XML 转成与 alertStream JSON 同构的 dict。"""
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

    这台机器回读 httpHosts 时 parameterFormatType 是空的，能力清单里也没有这个
    字段，所以设备到底推 JSON 还是 XML 不能想当然——两种都解析。
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


def normalize(raw: dict) -> dict | None:
    """
    把设备原始报文归一化成业务记录。

    raw 是 alertStream / HTTP 监听里的整条报文（含 eventType 外壳），
    字段可能是 JSON 的数字，也可能是 XML 解析出来的字符串。
    非门禁事件返回 None。
    """
    if raw.get("eventType") != "AccessControllerEvent":
        return None

    ace = raw.get("AccessControllerEvent") or {}
    major = _int(ace.get("majorEventType"))
    minor = _int(ace.get("subEventType"))
    if major is None or minor is None:
        return None

    code = ALL_CODES.get((major, minor))
    name, verified = describe(major, minor)

    # 身份三选一，工号优先
    person = (ace.get("employeeNoString") or "").strip()
    person_name = (ace.get("name") or "").strip()
    card_no = (ace.get("cardNo") or "").strip()

    return {
        "serialNo": _int(ace.get("serialNo")),
        "frontSerialNo": _int(ace.get("frontSerialNo")),
        "time": raw.get("dateTime"),
        "live": bool(ace.get("currentEvent")),
        "major": major,
        "minor": minor,
        "majorName": MAJOR_NAMES.get(major, str(major)),
        "eventName": name,
        "codeVerified": verified,
        "declared": is_declared(major, minor),
        "method": code.method if code else None,
        "actorKind": code.actor_kind if code else "unknown",
        "person": person or person_name or None,
        "personName": person_name or None,
        "cardNo": card_no or None,
        "doorNo": _int(ace.get("doorNo")),
        "remoteHost": ace.get("remoteHostAddr") or None,
        "netUser": ace.get("netUser") or None,
        # 只作留档：这是设备"配置的"认证模式，不能用来判断本次开门方式
        "configuredVerifyMode": ace.get("currentVerifyMode"),
        "hasPicture": False,   # 由调用方在收到图片 part 后置位
    }
