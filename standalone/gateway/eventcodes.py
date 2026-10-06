"""
事件码表 —— 薄适配层。

**真正的表在 `custom_components/hikvision_acs/eventcodes.py`**，本模块只把它
加载进来并适配成这个独立网关需要的 dict 形态。

为什么这么做：这张表原先在仓库里存了三份（调试探针、独立网关、HA 集成），
改一份漏两份。实际发生过——`(3,1024)` 已在真机确认是远程开门，但探针和本模块
仍是旧表，把它标成了"未确认"。

本模块依赖仓库完整布局（`custom_components/` 要在上层目录里）。要单独部署
这个网关，请整仓拷贝，不要只拷 `standalone/`。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_COMPONENT_DIR = (pathlib.Path(__file__).resolve().parent.parent.parent
                  / "custom_components" / "hikvision_acs")


def _load():
    path = _COMPONENT_DIR / "eventcodes.py"
    if not path.is_file():
        raise SystemExit(
            f"找不到事件码表：{path}\n"
            "本模块依赖仓库完整布局，请整仓拷贝而不是只拷 standalone/。")
    spec = importlib.util.spec_from_file_location("acs_eventcodes", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["acs_eventcodes"] = module
    spec.loader.exec_module(module)
    return module


EC = _load()

# 直接转出去，调用方不用关心来源
MAJOR_NAMES = EC.MAJOR_NAMES
ALL_CODES = EC.ALL_CODES
VERIFIED = EC.VERIFIED
UNVERIFIED = EC.UNVERIFIED
UNIDENTIFIED = EC.UNIDENTIFIED
DECLARED = EC.DECLARED


def describe(major: int, minor: int) -> tuple[str, bool]:
    return EC.describe(major, minor)


def is_declared(major: int, minor: int) -> bool:
    return EC.is_declared(major, minor)


def parse_notification(body: bytes) -> dict | None:
    return EC.parse_notification(body)


def normalize(raw: dict) -> dict | None:
    """
    归一化成 dict（本网关的存储层要 dict；HA 集成那边用的是 dataclass）。

    返回的键与 store.py / server.py 的约定一致。
    """
    event = EC.normalize(raw)
    if event is None:
        return None
    return {
        "serialNo": event.serial_no,
        "frontSerialNo": event.front_serial_no,
        "time": event.time,
        "live": event.live,
        "major": event.major,
        "minor": event.minor,
        "majorName": event.major_name,
        "eventName": event.event_name,
        "codeVerified": event.code_verified,
        "declared": event.declared,
        "method": event.method,
        "actorKind": event.actor_kind,
        "person": event.person,
        "personName": event.person_name,
        "cardNo": event.card_no,
        "doorNo": event.door_no,
        "remoteHost": event.remote_host,
        "netUser": None,
        "configuredVerifyMode": event.configured_verify_mode,
        "hasPicture": False,   # 由调用方在收到图片 part 后置位
    }
