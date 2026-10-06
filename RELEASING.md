# 发布清单

把这个仓库变成客户能装的 HACS 集成，需要按顺序做以下几步。

## 1. 改掉占位信息（**必须**，否则 HACS 校验会失败）

现在仓库里用的是占位坐标 `hikvision-acs/hikvision_acs`。改成你的真实仓库地址：

**`custom_components/hikvision_acs/manifest.json`**

```json
{
  "codeowners": ["@你的GitHub用户名"],
  "documentation": "https://github.com/<你>/<仓库名>",
  "issue_tracker": "https://github.com/<你>/<仓库名>/issues"
}
```

**`hacs.json`** —— 不用改，但确认 `name` 是客户在 HACS 里看到的名称。

**`CHANGELOG.md`** —— 末尾的 `[0.1.0]:` 链接地址。

**`README.md`** —— 里面出现的仓库地址。

## 2. 确认许可证

仓库里放的是 **MIT**（`LICENSE`）。这是 HACS 集成的常见选择，但**这是你的商业决定**：

- 想开源放 MIT / Apache-2.0 → 保持或替换
- 想保留商业权利 → 换成专有许可，并注意 HACS 对专有许可的集成也接受，
  但客户就无法自由二次分发
- 想双授权 → 需要自己补 `LICENSE-COMMERCIAL` 等文件

**确认一遍再推公网。**

## 3. 推到 GitHub

```bash
cd 海康威视门禁
git add -A
git commit -m "feat: 海康威视门禁 HA 集成 v0.1.0"
git branch -M main
git remote add origin git@github.com:<你>/<仓库名>.git
git push -u origin main
```

## 4. 在仓库设置里补齐 HACS 要求

HACS 校验会看仓库本身，不只是代码：

- **Description**：填一句说明，例如「海康威视门禁一体机集成：开门、事件、门铃推屏」
- **Topics**：加上 `hacs`、`home-assistant`、`integration`、`hikvision`、`access-control`

## 5. 打标签发 Release

HACS 靠 Release 判断版本。**`manifest.json` 里的 `version` 必须和标签一致。**

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

然后在 GitHub 上基于该标签建 Release，把 `CHANGELOG.md` 里对应段落贴进去。

## 6. 确认 CI 全绿

推上去后三个工作流会自动跑：

| 工作流 | 作用 |
|---|---|
| `hassfest.yaml` | Home Assistant **官方**集成校验（manifest、翻译、目录结构） |
| `hacs.yaml` | HACS **官方**收录校验 |
| `validate.yaml` | 本仓库的离线校验（155 项） |

**hassfest 和 HACS 这两个是最有价值的**——它们由 HA 和 HACS 官方维护，能查出我
在开发机上装不了 HA Core 因而无法本地验证的东西。**它们报错就照着改，不要跳过。**

## 7. 本地先跑一遍

```bash
pip install pyyaml
python tools/validate_integration.py
```

## 8. 客户安装说明

客户侧的步骤写在 `README.md` 的「安装」一节。要点：

1. HACS → 集成 → 自定义存储库 → 填本仓库地址 → 类别选 Integration
2. 安装后**重启 HA**
3. 设置 → 设备与服务 → 添加集成 → 搜「海康威视门禁」

前置条件是**门禁设备与 HA 在同一局域网**——设备要能访问 HA 才能推送实时事件。
上门前先确认这条，否则客户现场会退化成只有轮询延迟。

## 9. 现场调试工具

`tools/acs_probe.py` 是给上门工程师用的，不用装 HA 就能确认设备状态：

```bash
python3 tools/acs_probe.py --host <设备IP> --password '<密码>' info      # 设备能力
python3 tools/acs_probe.py --host <设备IP> --password '<密码>' hosts     # HTTP 监听配置
python3 tools/acs_probe.py --host <设备IP> --password '<密码>' sniff -t 60  # 采样真实事件
python3 tools/acs_probe.py --host <设备IP> --password '<密码>' rtsp      # 取流验证
```

**注意**：`sniff` 会占用设备唯一的那条 alertStream 会话。同一时间**只能有一个客户端**，
否则会互相顶掉（设备要过很久才释放死会话）。日常运行不需要它——集成走的是 HTTP 监听。
