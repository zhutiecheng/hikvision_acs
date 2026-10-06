# 更新记录

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.1] - 2026-10-06

### 修复

- **在 HA 里添加集成时报「无法加载配置向导 / Invalid handler specified」**
  ——这是致命问题，集成完全无法使用。根因是 `isapi.py` 写了
  `from aiohttp import DigestAuth`，而 **aiohttp 不导出 DigestAuth**，
  导致整个集成包导入失败。HA 报的错误完全指不到根因，只有真装上 HA 才会暴露。
  已改用 `httpx`（HA 核心依赖，自带 DigestAuth）——Home Assistant 自己的源码里
  就写着 "aiohttp don't support DigestAuth so we use httpx"。
- `tools/correlate.py` 导入已删除的 `EVENT_CODES`，一执行就 ImportError。
- `tools/acs_probe.py` 的 `sniff` 在流空闲时崩溃：requests 在流式读取时把
  urllib3 的读超时包成 ConnectionError 而不是 ReadTimeout。
  因为事件持续到来时不触发，它表现得像随机崩溃。

### 新增

- CI 工作流「集成导入测试」：真的安装 Home Assistant 并逐个导入集成模块。
  hassfest 和 HACS **都不会加载集成**，所以它们拦不住 import 层面的错误——
  上面那个致命 bug 就是三绿状态下溜过去的。
- 本地校验新增「第三方导入」检查，把 aiohttp DigestAuth 这个坑钉死。
- 本地校验新增「工具脚本可导入」检查。

### 已知限制

- `(3, 1024)` 远程开门的报文里 `remoteHostAddr` 与 `netUser` 均为空，
  **无法判断是哪台室内机发起的**。多室内机 / 管理处场景下这一项需另行解决。
- 刷卡 / 指纹类事件码来自公开文档，测试机上无卡未现场验证。

## [0.1.0] - 2026-10-06

首个版本。已在 **DS-K1T341BM（固件 V3.7.80）** 上端到端实测通过。

### 新增

- **远程开门**：`button` 实体 + `hikvision_acs.open_door` 服务
- **门禁事件**：`event` 实体，支持门铃 / 认证通过 / 认证失败 / 门锁打开 /
  门锁关闭 / 开门按钮 / 远程开门，可直接用作自动化触发器
- **状态传感器**：最后开门人、最后开门方式、最后事件、今日开门次数
- **二元传感器**：门锁（继电器状态）、在线
- **摄像头**：RTSP 流 + 抓拍图
- **配置流程**：全部在 UI 完成，无需编辑 YAML；含重认证与选项流程
- **中文 / 英文翻译**
- **门铃推屏蓝图**：`blueprints/automation/hikvision_acs/doorbell_popup.yaml`

### 技术要点

- **零第三方依赖**：使用 HA 自带的 `aiohttp`，不与客户已有依赖冲突
- **事件入口双通道**：主通道为 HA webhook（集成自动把设备的 HTTP 监听指向
  `/api/webhook/<id>`），兜底为 `AcsEvent` 轮询，按设备侧全局递增的 `serialNo` 去重
- **多设备**：同一集成可添加多台门禁，实体按设备归组

### 真机实测得出的约束（写进代码，不是抄文档）

- 事件码必须用 `(major, minor)` 二元组查表：设备通过
  `GET /ISAPI/Event/notification/httpHosts/capabilities` 自述可上报的事件码，
  同一个 minor 会出现在多个大类下（例如 `0x400` 同时在「异常」和「操作」里）
- 判断「用什么方式开门」只能看 `minor`，**不能看 `currentVerifyMode`**：
  实测人脸认证通过时 `minor=75`，而该字段显示 `cardOrfaceOrPw`（设备配置的认证模式）
- `currentEvent` 字段区分实时事件（`true`）与连接建立时补给的历史事件（`false`）
- 抓拍图以二进制随事件推送，无需另行调用抓拍接口
- 该固件不认 `parameterFormatType` 字段，报文可能是 JSON 也可能是 XML，两种都要解析
- 海康接口常返回 HTTP 200 但 XML 里 `statusCode != 1`，必须解析 `ResponseStatus`
- **不使用 alertStream 长连接**：实测该设备只允许一条会话，且因「有事件才写数据」，
  客户端异常断开后设备长时间不释放，后续连接被拒（404 或无响应）

### 未确认

以下事件码由设备声明可上报，但语义尚未确认，**不触发任何自动化**，仅作传感器状态：

| (major, minor) | 十六进制 | 观测到的规律 |
|---|---|---|
| (3, 112) / (3, 113) | 0x70 / 0x71 | 成对出现；报文带 `remoteHostAddr`，实测来源为一台**室内机** |
| (3, 240) | 0xf0 | 单独出现 |
| (3, 80) | 0x50 | 单独出现 |
| (2, 1024) | 0x400 | 历史数据中紧跟「门锁关闭」 |
| (1, 1028) | 0x404 | 历史数据中紧跟「门锁关闭」后 1 秒 |

### 已知限制：远程开门事件没有来源

`(3, 1024)` 远程开门的报文里 **`remoteHostAddr` 和 `netUser` 都是空的**，而
`(3, 112)/(3, 113)` 却带 `remoteHostAddr`。这意味着：

- 目前**无法从事件本身判断是哪台室内机（或多台中的哪一台）开的门**
- 若客户有多台室内机或管理处，这一项需要靠时间相关性间接推断，
  或在室内机侧另行取日志

这一点已作为已知限制记录，未在集成里做任何猜测性归因。

刷卡 / 指纹类事件码来自公开文档，测试机上无卡无法现场验证，接入真实卡后需复核。

[0.1.1]: https://github.com/zhutiecheng/hikvision_acs/releases/tag/v0.1.1
[0.1.0]: https://github.com/zhutiecheng/hikvision_acs/releases/tag/v0.1.0
