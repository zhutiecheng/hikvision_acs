#!/usr/bin/env python3
"""
HA 集成的离线校验。

为什么需要它：HA 跑在另一台主机上（本项目开发时是 192.0.2.20），
开发机上装不了 Home Assistant Core，所以无法真跑集成。这里用能拿到的最强
证据替代：

  1. 纯逻辑单测 —— eventcodes.py 不依赖 HA，事件码映射、currentVerifyMode
     陷阱、JSON/XML 解析这些最容易错的地方全部实测
  2. 结构校验 —— manifest / hacs.json / 翻译 / 平台齐备性
  3. 静态分析 —— 未定义名字、翻译键覆盖

任何一项失败都以非 0 退出，可以直接当 CI 门禁用。
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import pathlib
import re
import sys
from xml.etree import ElementTree

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "hikvision_acs"

failures: list[str] = []
checks = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}" + (f"\n       {detail}" if detail else ""))
        failures.append(label)


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ---------------------------------------------------------------------------
# 1. 加载纯逻辑模块（无 HA 依赖）
# ---------------------------------------------------------------------------

def load_eventcodes():
    spec = importlib.util.spec_from_file_location(
        "acs_eventcodes", COMPONENT / "eventcodes.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["acs_eventcodes"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 2. 纯逻辑单测
# ---------------------------------------------------------------------------

def test_eventcodes(ec) -> None:
    section("1. 事件码逻辑（纯函数，实测）")

    # —— 真机实测确认的语义 ——
    cases = [
        ((5, 75), "access_granted", "人脸", "person"),
        ((5, 37), "doorbell", None, "anonymous"),
        ((5, 21), "door_opened", None, "system"),
        ((5, 22), "door_closed", None, "system"),
        ((5, 23), "exit_button", "开门按钮", "anonymous"),
        ((3, 1024), "remote_open", "远程开门", "anonymous"),
    ]
    for (major, minor), ha_event, method, actor in cases:
        code = ec.ALL_CODES.get((major, minor))
        check(code is not None, f"({major},{minor}) 在码表里")
        if code is None:
            continue
        check(code.ha_event == ha_event,
              f"({major},{minor}) HA 事件 = {ha_event}", f"实际 {code.ha_event}")
        check(code.method == method,
              f"({major},{minor}) 方式 = {method}", f"实际 {code.method}")
        check(code.actor_kind == actor,
              f"({major},{minor}) 角色 = {actor}", f"实际 {code.actor_kind}")
        check(code.verified is True, f"({major},{minor}) 标记为已实测确认")

    # —— 同一个 minor 在不同 major 下必须区分开（本项目最核心的设计约束）——
    check(ec.ALL_CODES[(5, 21)].name == "门锁打开", "(5,21) 与其它 major 的 21 不混淆")
    check((5, 1024) not in ec.ALL_CODES, "minor=1024 只登记在 major=3 下")
    check(ec.is_declared(3, 1024) and ec.is_declared(2, 1024),
          "0x400 在「操作」和「异常」两个大类下都被声明（固件自述）")
    check(not ec.is_declared(5, 9999), "未声明的码被判为未声明")

    # —— 归一化：人脸事件 ——
    raw = {
        "dateTime": "2026-10-06T17:05:35+08:00",
        "eventType": "AccessControllerEvent",
        "AccessControllerEvent": {
            "majorEventType": 5, "subEventType": 75,
            "employeeNoString": "1", "doorNo": 1,
            "currentVerifyMode": "cardOrfaceOrPw",   # ← 配置值，不是本次方式
            "currentEvent": True, "serialNo": 20, "frontSerialNo": 19,
        },
    }
    ev = ec.normalize(raw)
    check(ev is not None, "人脸事件可归一化")
    if ev:
        check(ev.ha_event == "access_granted", "人脸事件 → access_granted")
        check(ev.method == "人脸",
              "方式取自 minor（人脸），而不是 currentVerifyMode",
              f"实际 {ev.method}")
        check(ev.configured_verify_mode == "cardOrfaceOrPw",
              "currentVerifyMode 只作留档")
        check(ev.person == "1", "身份取自 employeeNoString")
        check(ev.serial_no == 20 and isinstance(ev.serial_no, int), "serialNo 转成 int")
        check(ev.door_no == 1 and isinstance(ev.door_no, int), "doorNo 转成 int")
        check(ev.live is True, "currentEvent=true → live")
        check(ev.declared is True, "declared 标记正确")
        check(ev.dedup_key == "20", "去重键用 serialNo")

    # —— 未确认语义的码：绝不能触发 HA 事件，也绝不能带 method ——
    for major, minor in [(3, 112), (3, 113), (3, 240), (3, 80), (2, 1024), (1, 1028)]:
        unknown = ec.normalize({
            "dateTime": "2026-10-06T17:25:27+08:00",
            "eventType": "AccessControllerEvent",
            "AccessControllerEvent": {
                "majorEventType": major, "subEventType": minor, "serialNo": 1},
        })
        check(unknown is not None and unknown.ha_event is None
              and unknown.method is None,
              f"({major},{minor}) 未确认语义：不触发 HA 事件、不带方式")

    # —— 非门禁事件与坏数据 ——
    check(ec.normalize({"eventType": "ANPR"}) is None, "非门禁事件返回 None")
    check(ec.normalize({"eventType": "AccessControllerEvent",
                        "AccessControllerEvent": {}}) is None, "缺 major/minor 返回 None")

    # —— XML 解析（固件不认 parameterFormatType，报文格式不能假设）——
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0">
<ipAddress>192.0.2.10</ipAddress>
<dateTime>2026-10-06T17:40:00+08:00</dateTime>
<eventType>AccessControllerEvent</eventType>
<AccessControllerEvent>
<majorEventType>5</majorEventType><subEventType>37</subEventType>
<doorNo>1</doorNo><serialNo>12345</serialNo><frontSerialNo>12344</frontSerialNo>
<currentEvent>true</currentEvent>
</AccessControllerEvent></EventNotificationAlert>"""
    parsed = ec.parse_notification(xml)
    check(parsed is not None and parsed.get("eventType") == "AccessControllerEvent",
          "XML 报文可解析")
    if parsed:
        ev_xml = ec.normalize(parsed)
        check(ev_xml is not None and ev_xml.ha_event == "doorbell",
              "XML 里的门铃事件正确归一化")
        check(ev_xml is not None and ev_xml.serial_no == 12345
              and isinstance(ev_xml.serial_no, int),
              "XML 的字符串数字被转成 int")
        check(ev_xml is not None and ev_xml.door_no == 1,
              "XML 的 doorNo 被转成 int")

    check(ec.parse_notification(b'{"eventType":"AccessControllerEvent"}') is not None,
          "JSON 报文可解析")
    check(ec.parse_notification(b"garbage") is None, "垃圾数据返回 None")
    check(ec.parse_notification(b"") is None, "空数据返回 None")

    # —— 码表自洽性：登记的码必须都在设备声明范围内 ——
    for key in list(ec.VERIFIED) + list(ec.UNVERIFIED):
        major, minor = key
        check(ec.is_declared(major, minor),
              f"码表中的 ({major},{minor}) 在设备声明范围内")


# ---------------------------------------------------------------------------
# 3. 结构校验
# ---------------------------------------------------------------------------

def test_structure() -> None:
    section("2. 集成结构")

    manifest_path = COMPONENT / "manifest.json"
    check(manifest_path.is_file(), "manifest.json 存在")
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("domain", "name", "version", "documentation", "issue_tracker",
                    "codeowners", "config_flow", "iot_class"):
            check(key in manifest, f"manifest 含 {key}")
        check(manifest.get("domain") == "hikvision_acs", "domain 正确")
        check(manifest.get("config_flow") is True, "config_flow 已开启")
        check(manifest.get("requirements") == [],
              "零第三方依赖（requirements 为空）")
        check("webhook" in manifest.get("dependencies", []),
              "声明依赖 webhook（推送入口）")
        check(re.fullmatch(r"\d+\.\d+\.\d+", manifest.get("version", "")),
              "version 是语义化版本")

        # HA 要求 manifest 的键顺序：domain、name，其余按字母序。
        # 这条是 hassfest 在 CI 上抓出来的——加在本地就不会再等一轮 CI 才发现。
        keys = list(manifest)
        if keys[:2] == ["domain", "name"]:
            rest = keys[2:]
            check(rest == sorted(rest),
                  "manifest 键序符合 HA 要求（domain、name，其余字母序）",
                  f"当前 {rest}，应为 {sorted(rest)}")
        else:
            check(False, "manifest 前两个键应为 domain、name", f"实际 {keys[:2]}")

    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    check("name" in hacs, "hacs.json 含 name（HACS 唯一必填项）")

    # hacs.json 的合法键。
    #
    # 来源是 HACS 的真实 schema（custom_components/hacs/utils/validate.py 里的
    # HACS_MANIFEST_JSON_SCHEMA，extra=vol.PREVENT_EXTRA 即多余键会被拒），
    # **不是**文档页的表格——文档漏了 render_readme，我照文档做了一版过严的
    # 护栏，反而会把合法键判成非法。
    allowed = {"name", "content_in_root", "country", "filename", "hacs",
               "hide_default_branch", "homeassistant", "persistent_directory",
               "render_readme", "zip_release"}
    unknown = sorted(set(hacs) - allowed)
    check(not unknown, "hacs.json 只含 HACS 支持的键",
          f"不支持的键：{unknown}（支持：{sorted(allowed)}）")

    check((COMPONENT / "brand" / "icon.png").is_file(),
          "提供品牌图标 custom_components/<domain>/brand/icon.png（HACS 必需）")

    # 必需的模块
    for name in ("__init__.py", "config_flow.py", "const.py", "coordinator.py",
                 "entity.py", "isapi.py", "eventcodes.py", "services.yaml",
                 "strings.json"):
        check((COMPONENT / name).is_file(), f"存在 {name}")

    # 平台齐备：PLATFORMS 里每个平台都要有对应模块和 async_setup_entry
    init_src = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    platforms = re.findall(r"Platform\.([A-Z_]+)", init_src)
    check(len(platforms) >= 5, f"声明了 {len(platforms)} 个平台")
    for plat in platforms:
        module = COMPONENT / f"{plat.lower()}.py"
        check(module.is_file(), f"平台 {plat.lower()} 有对应模块")
        if module.is_file():
            src = module.read_text(encoding="utf-8")
            check("async def async_setup_entry" in src,
                  f"{plat.lower()}.py 实现了 async_setup_entry")


# ---------------------------------------------------------------------------
# 4. 翻译完整性
# ---------------------------------------------------------------------------

def test_translations() -> None:
    section("3. 翻译与实体命名")

    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    for lang in ("en", "zh-Hans"):
        path = COMPONENT / "translations" / f"{lang}.json"
        check(path.is_file(), f"存在 translations/{lang}.json")
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            check(data.keys() == strings.keys(),
                  f"{lang}.json 顶层键与 strings.json 一致")

    # 代码里用到的每个 _attr_translation_key 都要有翻译
    entity_t = strings.get("entity", {})
    for plat in ("event", "binary_sensor", "button", "sensor", "camera"):
        module = COMPONENT / f"{plat}.py"
        if not module.is_file():
            continue
        src = module.read_text(encoding="utf-8")
        keys = set(re.findall(r'_attr_translation_key\s*=\s*"([^"]+)"', src))
        declared = set(entity_t.get(plat, {}))
        for key in keys:
            check(key in declared,
                  f"{plat}.py 的 translation_key 「{key}」在 strings.json 里有条目")
        for key in declared:
            check(key in keys,
                  f"strings.json 里的 {plat}.{key} 在代码中被使用（无死翻译）")

    # 事件类型的中文翻译要和 const 对齐
    zh = json.loads((COMPONENT / "translations" / "zh-Hans.json").read_text(encoding="utf-8"))
    state_map = zh.get("entity", {}).get("event", {}).get("access_event", {}).get("state", {})
    const_src = (COMPONENT / "const.py").read_text(encoding="utf-8")
    event_types = re.findall(r'EVENT_TYPE_[A-Z_]+\s*:\s*Final\s*=\s*"([^"]+)"', const_src)
    check(len(event_types) >= 7, f"const 里定义了 {len(event_types)} 种事件类型")
    for et in event_types:
        check(et in state_map, f"事件类型 {et} 有中文翻译")


# ---------------------------------------------------------------------------
# 5. 静态分析：未定义名字
# ---------------------------------------------------------------------------

def test_undefined_names() -> None:
    section("4. 静态分析")

    for path in sorted(COMPONENT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Global):
                defined.update(node.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        missing = sorted(used - defined)
        check(not missing, f"{path.name} 无未定义名字", f"疑似缺少：{missing}")


# ---------------------------------------------------------------------------
# 6. YAML 合法性
# ---------------------------------------------------------------------------

def test_yaml() -> None:
    section("5. 蓝图与服务定义")
    try:
        import yaml
    except ImportError:
        print("  ⏭  未安装 pyyaml，跳过 YAML 校验")
        return

    class BlueprintLoader(yaml.SafeLoader):
        """HA 蓝图用 !input 这类自定义标签，SafeLoader 需要认识它们。"""

    def _unknown(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    BlueprintLoader.add_multi_constructor("!", _unknown)

    for path in [COMPONENT / "services.yaml",
                 ROOT / "blueprints" / "automation" / "hikvision_acs" / "doorbell_popup.yaml"]:
        try:
            yaml.load(path.read_text(encoding="utf-8"), Loader=BlueprintLoader)
            check(True, f"{path.name} YAML 合法")
        except Exception as err:                          # noqa: BLE001
            check(False, f"{path.name} YAML 合法", str(err)[:200])


def test_single_source_of_truth(ec) -> None:
    """
    事件码表在仓库里只允许有一份。

    这条检查是因为真的踩过：表原先是三份拷贝（调试探针、独立网关、HA 集成），
    更新集成时漏了另外两份，于是 (3,1024) 在探针里被标成"未确认"——客户现场
    会看到错误的事件名。
    """
    section("6. 事件码表单一来源")

    consumers = {
        "调试探针": ROOT / "tools" / "acs_probe.py",
        "独立网关": ROOT / "standalone" / "gateway" / "eventcodes.py",
    }
    for label, path in consumers.items():
        check(path.is_file(), f"{label} 存在")
        if not path.is_file():
            continue
        src = path.read_text(encoding="utf-8")
        check("custom_components" in src and "eventcodes.py" in src,
              f"{label} 从集成加载事件码表（不自己维护一份）")
        literal_table = re.search(r'\(\s*\d+\s*,\s*\d+\s*\)\s*:\s*"', src)
        check(literal_table is None,
              f"{label} 没有内嵌事件码字面表",
              f"在偏移 {literal_table.start()} 处发现疑似内嵌表"
              if literal_table else "")

    # 行为层面再确认一次：三者对关键码的判断必须一致
    def load(path, name, label):
        """
        加载失败要变成一条检查失败，绝不能让脚本崩掉。

        踩过：CI 上没装 requests，探针在模块级 sys.exit，SystemExit 直接穿透
        校验器把整个脚本带崩，日志里只剩一句 "exit code 1"，后面所有检查结果
        全被吞掉，排查时完全看不出发生了什么。
        """
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            return module
        except SystemExit as err:
            check(False, f"{label} 可被加载", f"模块在加载时退出：{err}")
        except BaseException as err:                       # noqa: BLE001
            check(False, f"{label} 可被加载", f"{type(err).__name__}: {err}")
        return None

    probe = load(ROOT / "tools" / "acs_probe.py", "acs_probe_check", "调试探针")
    gateway = load(ROOT / "standalone" / "gateway" / "eventcodes.py",
                   "acs_gw_check", "独立网关")

    for major, minor in [(3, 1024), (5, 37), (5, 75), (5, 21), (3, 112)]:
        expected = ec.describe(major, minor)

        if probe is not None:
            probe_got = probe.EC.describe(major, minor)
            check(probe_got == expected,
                  f"探针与集成对 ({major},{minor}) 判断一致",
                  f"探针={probe_got} 集成={expected}")

        if gateway is not None:
            gw_got = gateway.describe(major, minor)
            check(gw_got == expected,
                  f"独立网关与集成对 ({major},{minor}) 判断一致",
                  f"网关={gw_got} 集成={expected}")

    # 适配层必须给出 store/server 约定的键，否则独立网关运行时会 KeyError
    need = {"serialNo", "frontSerialNo", "time", "live", "major", "minor", "majorName",
            "eventName", "codeVerified", "declared", "method", "actorKind", "person",
            "personName", "cardNo", "doorNo", "remoteHost", "netUser",
            "configuredVerifyMode", "hasPicture"}
    if gateway is not None:
        rec = gateway.normalize({
            "dateTime": "2026-10-06T17:23:06+08:00",
            "eventType": "AccessControllerEvent",
            "AccessControllerEvent": {
                "majorEventType": 5, "subEventType": 75,
                "employeeNoString": "1", "doorNo": 1,
                "currentEvent": True, "serialNo": 20,
            },
        })
        missing = need - set(rec or {})
        check(rec is not None and not missing,
              "独立网关的 normalize 输出键与 store/server 约定一致",
              f"缺少 {sorted(missing)}" if missing else "")


def main() -> int:
    print("海康门禁 HA 集成 —— 离线校验")
    print(f"目标目录：{COMPONENT}")
    print("（开发机上装不了 HA Core，故用纯逻辑单测 + 结构/静态校验替代）")

    ec = load_eventcodes()
    test_eventcodes(ec)
    test_structure()
    test_translations()
    test_undefined_names()
    test_yaml()
    test_single_source_of_truth(ec)

    section("结果")
    if failures:
        print(f"❌ {len(failures)} / {checks} 项失败：")
        for item in failures:
            print(f"   - {item}")
        return 1
    print(f"✅ 全部 {checks} 项校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
