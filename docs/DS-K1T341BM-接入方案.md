# DS-K1T341BM 接入方案（开门控制 / 事件上报 / 门铃推流）

> 目标：①远程控制开门 ②上报"谁开的门 + 用什么方式开的" ③有人按门铃时自动把视频推流到中控屏
>
> 结论摘要：**这三个需求走网络（ISAPI），不要走 485。** 该机的 485 是"经销私有"的设备间总线，
> 只有两个用途，不是二次开发接口。但 485 可以当一条**应急开门**的物理通道。

---

## 一、先纠正一个前提：这台机器的 485 能干什么

官方规格书《DS-K1T341BM 系列 人脸识别门禁一体机》对 485 只写了两条用途（原文）：

> - **外接安全模块**：支持通过 RS485（**经销私有**）接入门控安全模块，防止主机被恶意破坏的情况下，门锁不被打开；
> - **读卡器模式**：支持通过 RS485（**经销私有**）或标准韦根（W26/W34）接入门禁控制器，作为读卡器模式使用；

物理接口清单（原文）：

> LAN\*1、**经销私有 RS485\*1**、标准 Wiegand \*1(单向输出)、MicroUSB 接口\*1、电锁\*1、门磁\*1、开门按钮\*1、防拆\*1

设备端 RS-485 菜单里，"外设类型"只有四种可选：接入控制器 / 门控安全模块 / 读卡器 / 二维码扫描器
（见 [Set RS-485 Parameters](https://enpinfo.hikvision.com/hkwsen/unzip/20251224164304_81809_doc/GUID-7A66D143-F830-4CED-B01D-62625BAD8B15.html)）。

同族老机型手册把这件事说得更直白：

> **门控模块**：开启此功能时，一体机支持通过 RS-485 接入门控模块，并通过门控模块接收门磁信号、
> 开门按钮信号和输入输出报警。关闭此功能时，直接通过一体机控制门磁信号、开门信号以及输入输出报警。

**含义**：485 上跑的是海康自有的设备间握手（一体机 ↔ 门禁控制器 / 安全模块），作用是
"把认证结果送出去"或"把 I/O 搬到一个防拆的安全模块上"。它：

- 不是开放 API，海康不发布协议文档；
- 方向上主要是**设备往外送**（读卡器模式），没有"下发开门"这种语义；
- **拿不到"谁开门、什么方式"这些业务字段**——这些数据根本不在 485 上。

所以：想靠 485 实现你的三个需求，**此路不通**。逆向私有协议不仅成本高，固件一升级就可能失效。

---

## 二、但 485 有一个真正好用的用法（应急开门通道）

这台机器自带干接点 I/O，端子定义（同系列官方手册端子表）：

| 组 | 端子 | 名称 | 说明 |
|---|---|---|---|
| D1/D2/D3 | 白紫 / 白黄 / 白红 | NC / COM / NO | 门锁继电器 |
| D4 | 黄绿 | SENSOR | 门磁 |
| D5 | 黑 | GND | 地 |
| **D6** | **黄灰** | **BTN** | **开门按钮输入** |
| C1/C2 | 黄 / 蓝 | 485+ / 485- | 485 总线 |

**做法**：用一个 485 转继电器模块，把它的**干接点**并在 `BTN` 与 `GND` 之间。
短接 = 模拟"按了一下开门按钮" = 开门。

优点：
- 真正意义上的"通过 485 控制开门"，纯硬件，不依赖网络，可作为断网/应急通道；
- 设备会把这个动作当作正常事件上报（开门按钮事件），有迹可循。

局限（必须知道）：
- **上报里没有"谁"**——它只是一次按钮开门，方式固定是"开门按钮"；
- 受门禁计划/时段限制，非授权时段可能不开；
- 只能并联在真实开门按钮上（干接点并联是安全的），不要串进锁回路。

> 定位：**备份/应急通道**。主控通道必须是网络。

---

## 三、需求 ①：远程控制开门

用 ISAPI（HTTP Digest 认证）：

```
PUT http://<设备IP>/ISAPI/AccessControl/RemoteControl/door/1
Content-Type: application/xml

<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <cmd>open</cmd>
</RemoteControlDoor>
```

`cmd` 可取 `open` / `close` / `alwaysOpen` / `alwaysClose`。
认证方式为 **Digest**（不是 Basic）。社区已验证该接口可实际开门
（[Home Assistant 讨论](https://community.home-assistant.io/t/hikvision-doorbell-curl-command-not-working-from-home-assistant-only/519815)、[Homey 讨论](https://community.homey.app/t/app-pro-hikvision/147865?page=9)）。

设备网络协议栈：`海康 SDK / ISAPI / Ehome4.0 / ISUP5.0 / 萤石协议`，网口 10/100/1000M 自适应。

提醒：`/RemoteControl/door/1` 中的 `1` 是门号，按实际门号改。

---

## 四、需求 ②：上报"谁开门 + 什么方式"（核心）

这是整个项目的重点。有两条架构，推荐 A。

### 架构 A：HTTP 监听（设备主动 POST 推送）— 推荐

官方文档 [How to get real-time event in listening mode](https://www.hikvisioneurope.com/eu/portal/portal/Technology%20Partner%20Program/03-How%20to/How%20to%20get%20real-time%20event%20in%20listening%20mode.pdf) 给出的完整流程：

1. `GET /ISAPI/Event/notification/httpHosts` 取当前配置
2. 改 IP / 端口 / URL / 协议后 `PUT /ISAPI/Event/notification/httpHosts`（200 OK 即成功）
3. 自检：`POST /ISAPI/Event/notification/httpHosts/1/test`
4. 你的服务端监听该端口，设备触发事件时主动 POST 过来

关键配置项（`HttpHostNotification` 节点）：

| 字段 | 说明 |
|---|---|
| `url` | `http://你的服务器:端口/路径` |
| `protocolType` | `HTTP` / `HTTPS` |
| `parameterFormatType` | `json` / `XML` / `querystring` |
| `addressingFormatType` | `ipaddress` / `hostname` |
| `portNo` | 监听端口 |
| `httpAuthenticationMethod` | `MD5digest` 或 `none`（不认证） |
| `uploadImagesDataType` | `URL` / `binary`（binary 直接把抓拍图塞进报文） |

优点：不需要常连接、服务端不需要设备账号密码、可带**联动抓拍照片**。

**坑（务必处理）**：刚开启监听时，设备会把**存储的历史事件补推一遍**。业务侧必须按时间窗过滤，
否则中控屏会被历史事件刷屏。（该行为见 [海康 HTTP 监听说明](https://blog.csdn.net/qq_25288617/article/details/146534456)。）

### 架构 B：alertStream 长连接（服务端主动订阅）

```
GET /ISAPI/Event/notification/alertStream     （Digest 认证，长期保持）
```

返回 `multipart/mixed` 分块流，每次事件是一段 `--MIME_boundary` 分隔的 JSON/XML。
需自己实现：断线重连、`Content-Length` 分帧、图片二进制分块跳过。

现有开源实现可参考（**注意：质量参差，勿直接上生产**）：
- [shadowwa1k3r/hikvision-isapi-wrapper](https://github.com/shadowwa1k3r/hikvision-isapi-wrapper) — alertStream 解析
- [MAGNAT12/hikvision-isapi-python](https://github.com/MAGNAT12/hikvision-isapi-python) — 历史事件查询

### 事件报文结构

```json
{
  "ipAddress": "192.168.1.64",
  "dateTime": "2024-11-15T21:03:07+08:00",
  "eventType": "AccessControllerEvent",
  "AccessControllerEvent": {
    "deviceName": "门口机",
    "majorEventType": 5,
    "subEventType": 75,
    "name": "张三",
    "employeeNoString": "1001",
    "cardNo": "",
    "cardReaderNo": 1,
    "doorNo": 1,
    "userType": "normal",
    "currentVerifyMode": "faceOrFpOrCardOrPw",
    "attendanceStatus": "checkIn",
    "mask": "no",
    "pictureURL": "",
    "serialNo": 111736,
    "FaceRect": { "x": 0.168, "y": 0.349, "width": 0.513, "height": 0.288 }
  }
}
```

**取"谁"**：`employeeNoString`（工号）/ `name` / `cardNo`
**取"方式"**：**看 `subEventType`（= minor），不要看 `currentVerifyMode`**

> ⚠️ 重要陷阱：上例中 `subEventType = 75`（人脸认证通过），但 `currentVerifyMode` 却是
> `faceOrFpOrCardOrPw`。后者是**设备配置的认证模式**，不是本次实际使用的方式。
> 如果用它来判断"这次是人脸还是刷卡"，会 100% 判错。

### 事件码对照表

`majorEventType`：门禁事件 = **5**（报警 1 / 异常 2 / 操作 3）。

`subEventType`（十进制 = 十六进制）：

| minor | 十六进制 | 含义 | 来源 |
|---|---|---|---|
| 1 | 0x01 | 合法卡刷卡 | 待实测 |
| 21 | 0x15 | 门锁打开 | 官方事件表 |
| 22 | 0x16 | 门锁关闭 | 官方事件表 |
| **23** | **0x17** | **开门按钮按下** | 官方事件表 |
| 24 | 0x18 | 开门按钮松开 | 官方事件表 |
| 25 | 0x19 | 门开（门磁） | 官方事件表 |
| 26 | 0x1a | 门关（门磁） | 官方事件表 |
| 27 | 0x1b | 门异常打开 | 官方事件表 |
| 28 | 0x1c | 门开超时 | 官方事件表 |
| **37** | **0x25** | **门铃响** | 官方事件表 |
| 38 | 0x26 | 指纹比对通过 | 官方事件表 |
| 39 | 0x27 | 指纹比对失败 | 官方事件表 |
| 51 | 0x33 | 呼叫中心 | 官方事件表 |
| **75** | **0x4B** | **人脸认证通过** | 实测样本 |
| 76 | 0x4C | 人脸认证失败 | 推断，待实测 |
| 151 | 0x97 | 密码错误 | 官方事件表 |
| 133/134/135 | 0x85/0x86/0x87 | 尾随 / 反向通行 / 强行闯入 | 官方事件表 |
| 1024–1027 | 0x400–0x403 | 远程开门 / 关门 / 常开 / 常闭 | 官方事件表 |

> 来源说明：「官方事件表」指海康《Access Control Event Types》附录的公开转载（`MINOR_*` 命名表）；
> 「实测样本」指真实设备返回的 AcsEvent 报文。「待实测」项**必须**用你自己的设备校准。

**上线前必做**：先跑一周"原始报文旁路记录"，把设备真实吐出的 `(major, minor)` 全量落库，
再据此固化映射表。不要照抄任何文档。

设备特性（规格书原文）：*"在线状态下将设备认证结果及联动抓拍照片实时上传给平台，支持断网续传功能，
设备离线状态下产生事件在与平台连接后会重新上传"* —— 所以断网期间的事件不会丢，但接入时要能容忍**乱序/延迟到达**。

补充：也可用 `POST /ISAPI/AccessControl/AcsEvent?format=json` 主动查询历史事件（分页，`maxResults` 上限 30，
大数据量要分页或切时间片）作为对账兜底。

---

## 五、需求 ③：门铃 → 自动推流到中控屏

### 先明确"门铃"在这台机器上是什么

DS-K1T341BM **没有独立门铃输出端子**（端子只有锁/门磁/开门按钮/防拆/485/韦根）。
它的"门铃"是规格书里的可视对讲能力：

> **可视对讲**：支持和海康互联 APP、4200 客户端、室内机、管理机进行可视对讲；支持配置一键呼叫室内机或管理机；
> 支持副门口机或围墙机模式；
> **视频预览**：支持管理中心远程视频预览，支持接入 NVR 设备，视频监控录像，编码格式 H.264；
> 识别界面的"**呼叫**"、"二维码"、"密码"的按键图标可分别配置是否显示。

即：屏幕上按"呼叫" → 产生呼叫/门铃事件 → 同时设备本身有 RTSP 视频流可用。

所以链路是：**订阅事件 → 命中门铃/呼叫码 → 拉 RTSP → 转封装推给中控屏 → 超时自动停流。**

### 取流地址

```
主码流  rtsp://admin:<密码>@<设备IP>:554/Streaming/Channels/101
子码流  rtsp://admin:<密码>@<设备IP>:554/Streaming/Channels/102
```

中控屏预览建议用**子码流**（省带宽、起流快），需要看清细节/录像时再切主码流。

### 建议实现顺序（体验差别很大）

1. **秒出画面**：收到门铃事件后立即取一张抓拍图先铺满屏幕
   （事件里的 `pictureURL`，或 `POST /ISAPI/Streaming/channels/101/picture`）
2. **起视频**：同时拉起 RTSP → 转封装 → 上屏
3. **自动收流**：N 秒（建议 30s）无操作或无人脸则释放会话，避免长期占流

### 传输协议选型

| 方案 | 延迟 | 评价 |
|---|---|---|
| **WebRTC** | <500ms | 首选，浏览器/自研大屏都吃 |
| HTTP-FLV | ~1s | 次选，实现简单，需 flv.js 类播放器 |
| HLS | 3–10s | ❌ 不要用，门铃场景延迟不可接受 |
| 直连 RTSP | ~1s | 仅当中控屏是原生客户端时可行 |

### 备选：走海康原生对讲

设备原生支持"一键呼叫室内机或管理机"。如果中控屏愿意以海康 SIP 门口机/室内机身份注册，
可以用原生对讲（双向语音 + 视频 + 开锁联动），省掉自己转流。
但这是海康私有 SIP，自研中控屏接入成本高、可调试性差，**除非中控屏就是海康管理机，否则不推荐**。

---

## 六、网络侧还需要确认的能力

| 能力 | 接口 | 用途 |
|---|---|---|
| 人员/人脸/卡下发 | `/ISAPI/AccessControl/UserInfo/...` | 权限管理 |
| 历史事件查询 | `POST /ISAPI/AccessControl/AcsEvent?format=json` | 对账兜底 |
| 设备信息 / 状态 | `/ISAPI/System/deviceInfo` | 健康检查 |
| 抓拍图 | `POST /ISAPI/Streaming/channels/101/picture` | 门铃首帧 |

---

## 七、待确认信息（影响实施方案）

1. **中控屏是什么形态**？浏览器 Web / Windows 客户端 / Android 大屏 / 尚未确定
   → 决定推流协议（WebRTC vs FLV）和整体技术栈
2. **部署网络**？设备与服务器同局域网 / 需跨网段或公网
   → 决定用 HTTP 监听（需设备能访问服务器）还是 alertStream（需服务器能访问设备）
3. **是否要一并做人员/人脸/权限下发与考勤**？
   → 决定要不要引入完整的门禁管理模块，还是只做事件管道
4. 设备当前密码与固件版本（事件码映射可能随固件变化）

---

## 八、参考资料

- [DS-K1T341BM 系列规格书（PDF）](https://atta.szlcsc.com/upload/public/pdf/source/20240919/DA16D96DD4701BB3D219CA1BD3955179.pdf)
- [Set RS-485 Parameters — Hikvision](https://enpinfo.hikvision.com/hkwsen/unzip/20251224164304_81809_doc/GUID-7A66D143-F830-4CED-B01D-62625BAD8B15.html)
- [Terminal Description — Hikvision](https://enpinfo.hikvision.com/hkwsen/unzip/20251224164304_81809_doc/GUID-CF0483BE-8F79-42D2-95A5-B9C569329ED9.html)
- [How to get real-time event in HTTP listening mode（PDF）](https://www.hikvisioneurope.com/eu/portal/portal/Technology%20Partner%20Program/03-How%20to/How%20to%20get%20real-time%20event%20in%20listening%20mode.pdf)
- [DS-K1T Series Connect To External Reader（PDF）](https://www.hikvision.com/content/dam/hikvision/en/support/how-to/how-to-document/access-control/DS-K1T-Series-Connect-To-External-Reader.pdf)
- [海康 HTTP 监听报警事件数据](https://blog.csdn.net/qq_25288617/article/details/146534456)
- [Hikvision ISAPI wrapper (Python)](https://github.com/shadowwa1k3r/hikvision-isapi-wrapper)
- [hikvision-isapi-python](https://github.com/MAGNAT12/hikvision-isapi-python)

---

# 九、真机实测记录（DS-K1T341BM / 固件 V3.7.80）

> **本节结论优先级高于上文任何推断。** 上文四、五节的表格是接入前基于公开文档的推测，
> 以下是拿真机跑出来的结果；冲突处以本节为准。

设备信息：型号 `DS-K1T341BM`，序列号 `...20251218V030780CHGT8213972`，
MAC `88:DE:39:82:62:7E`，固件 `V3.7.80 build 251218`，`deviceType=ACS`，
视频 H.264 1280×720@25，带 G.711ulaw 音频。

## 9.1 事件码必须用 (major, minor) 二元组

`GET /ISAPI/Event/notification/httpHosts/capabilities` 返回了**固件自述的、按大类分列的可上报事件码清单**。
这是最权威的来源——文档和网上的对照表都可能过时或抄错，这份不会。

关键点：**同一个 minor 会出现在多个大类里**。例如 `0x400` 同时出现在「异常」和「操作」下，
本机实测 `(3,1024)` 是远程开门，而 `(2,1024)` 是另一回事。
只按 minor 查表一定会误判——这不是推测，是固件自己声明的。

各测试到的组合均已在声明清单内核对通过。

## 9.2 实测确认语义的事件码

| (major, minor) | 十六进制 | 含义 | 实测依据 |
|---|---|---|---|
| **(5, 75)** | 0x4b | **人脸认证通过** | 带 `employeeNoString`、`FaceRect`、二进制抓拍图 |
| **(5, 37)** | 0x25 | **门铃响** | 现场按「呼叫」键采到，带 `doorNo` |
| (5, 21) | 0x15 | 门锁打开 | 每次开门后必现 |
| (5, 22) | 0x16 | 门锁关闭 | |
| (5, 23) | 0x17 | 开门按钮按下 | |
| (5, 24) | 0x18 | 开门按钮松开 | |
| (3, 1024) | 0x400 | 远程开门 | 实测每次紧跟 `(5,21)` 门锁打开 |

**设备声明了但语义仍未确认**（代码中单独归类，界面会打标记，不参与业务判断）：

| (major, minor) | 十六进制 | 观测到的规律 |
|---|---|---|
| (3, 112) / (3, 113) | 0x70 / 0x71 | 成对出现，多次连续；报文带 `remoteHostAddr`，**实测来源是室内机** |
| (3, 240) | 0xf0 | 单独出现 |
| (3, 80) | 0x50 | 单独出现 |
| (2, 1024) | 0x400 | 历史上紧跟「门锁关闭」 |
| (1, 1028) | 0x404 | 历史上紧跟「门锁关闭」后 1 秒 |

### 9.2.1 远程开门无法归因（已知限制）

`(3, 1024)` 远程开门的报文里 **`remoteHostAddr` 与 `netUser` 均为空**，
而 `(3, 112)/(3, 113)` 带有 `remoteHostAddr`。因此：

- **不能**从远程开门事件判断是哪台室内机 / 管理处发起的；
- 现场实测的一次完整过程（室内机 IP 已确认）：

```
17:25:27  (3,112) remoteHostAddr=室内机      ← 4 对 (112,113) 连续出现
17:25:27  (3,113) remoteHostAddr=室内机
   ...    （共 4 对，跨 17:25:27–29）
17:25:34  (3,1024) 远程开门   remoteHostAddr=空   netUser=空
17:25:34  (5,21)   门锁打开
17:25:39  (5,37)   门铃响          ← 门禁机上按「呼叫」
17:25:53  (3,1024) 远程开门   remoteHostAddr=空
```

`(3,112)/(3,113)` 的语义仍未确认，需做受控对照实验（逐项操作、逐项比对）才能定论。
在搞清之前，集成里**不给这些码做任何归因**。

## 9.3 `currentVerifyMode` 不能用来判断开门方式

实测报文（这是**你设备上真实发生的一次人脸开门**）：

```
2026-10-06T17:05:35  major=5 minor=75  人脸认证通过  人=1
                     currentVerifyMode = "cardOrfaceOrPw"
```

实际走的是**人脸**（`minor=75`），但 `currentVerifyMode` 显示的是"卡或脸或密码"。
后者是设备**配置的**认证模式，不是本次实际使用的方式。用它判断「什么方式开门」必然出错。

**判据只有一个：`minor`。**

## 9.4 `currentEvent` 字段：区分实时事件与历史补给

连接瞬间或刚启用 HTTP 监听时，设备会把它存储的**存量事件**一次性倒出来（实测最早到三个月前）。
这些报文的 `currentEvent` 为 `false`，真正实时产生的为 `true`。

比"按时间窗过滤"可靠得多——直接按这个布尔值分流即可。

## 9.5 主通道：HTTP 监听，不要用 alertStream 长连接

这是实测踩到的最大的坑，**直接决定了架构**：

> 设备**只允许一条 alertStream 会话**。而且因为它是"有事件才写数据"，
> 客户端被强杀后设备**长时间察觉不到对端已死**，那条会话一直被占着，
> 之后所有新连接被拒——有时返回 **404**，有时干脆**不响应**（连接建立但不下发响应头）。
> 此时快速重试只会把设备占得更死。

因此本项目把主通道定为 **HTTP 监听**（`PUT /ISAPI/Event/notification/httpHosts`）：
设备每个事件开一次短连接 POST，不存在会话被占死的问题。
`alertStream` 仍保留为可选，但退避已调至 5s→300s。

**运维注意**：不要同时运行多个 alertStream 客户端（例如一边跑采样工具一边跑网关），会互相顶掉。

## 9.6 HTTP 监听的两个实操要点

**① 能力清单里没有 `parameterFormatType` 字段。**
我们发送该字段后回读为空，说明这个固件不认它。因此**报文格式不能假设**——
实现上 JSON、XML、带图的 multipart 三种都解析。

**② 抓拍图以二进制随事件一起推送。**
实测 6 张均有效（`\xff\xd8` 开头的 JPEG，65–116KB，432×768）。
设备自动补全的 `SubscribeEvent` 节点里写着 `<pictureURLType>binary</pictureURLType>`。
所以**不需要再单独调抓拍接口取图**。

设备自动补全的完整订阅配置（我们只发了前几个字段，其余是设备默认值）：

```xml
<SubscribeEvent>
  <heartbeat>30</heartbeat>
  <eventMode>all</eventMode>
  <EventList><Event>
    <type>AccessControllerEvent</type>
    <pictureURLType>binary</pictureURLType>
  </Event></EventList>
</SubscribeEvent>
```

**③ alertStream 的图片 part 头部**（供对比，HTTP 监听不受此限）：

```
Content-Disposition: form-data; name="Picture"; filename="Picture.jpg"
Content-Type: image/jpeg
Content-ID: pictureImage
```

## 9.7 其他实测确认的接口行为

| 接口 | 实测结果 |
|---|---|
| `POST /ISAPI/AccessControl/AcsEvent?format=json` | **强制要求 `major` 字段**，缺失返回 `HTTP 400 / MessageParametersLack / errorMsg="major"` |
| 同上，用 XML 发 | 拒绝，`badJsonFormat`——该接口只吃 JSON |
| `POST /ISAPI/Event/notification/httpHosts/1/test` | 该固件**不支持**，返回 `methodNotAllowed` |
| `GET /ISAPI/ContentMgmt/logSearch` | 需要 `metaId`，尝试未果，未继续 |
| `POST /ISAPI/Streaming/channels/101/picture` | 未使用（图片随事件推送，不需要） |

## 9.8 视频链路（go2rtc + WebRTC）

- RTSP 主/子码流均可用：`rtsp://admin:***@<IP>:554/Streaming/Channels/101`（主）、`102`（子）
- go2rtc 抓单帧实测 **0.67 秒**返回 1280×720 JPEG，可作门铃"秒出画面"
- **go2rtc 会拒绝跨域 WebSocket**：`Origin` 与自身不一致时返回 **403 Forbidden**。
  这意味着不能从别的站点跨域 import `video-rtc.js` 再连 `/api/ws`。
  中控屏采用 **同源 iframe**（`http://<go2rtc>:1984/webrtc.html?src=door`），实测握手 101 正常。
