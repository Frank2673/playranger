# PlayRanger

> 给游戏测试员装三层脑子，越错越聪明。

**PlayRanger** 是一个 AI 原生游戏自动化测试 Agent，采用「**双引擎 + 三层自修正**」架构，核心推理部署在单台 NVIDIA DGX Spark 上。

它不是按脚本跑的录屏回放，而是像真人玩家一样：**读状态 → 想一步 → 走一步 → 验证对不对 → 不对就改 → 改不通就反思 → 再试一轮。**

## 架构

```
                    ┌─────────────────────────────┐
                    │  大脑（DGX Spark 推理层）     │
                    │  LLM Planner  +  VLM Inspector│
                    └──────────┬───────────┬───────┘
                               │           │
              ┌────────────────▼──┐      ┌─▼──────────────────┐
              │ 引擎A·试玩引擎       │      │ 引擎B·视觉引擎       │
              │ 读状态→规划→执行      │      │ 截图→VLM判异常       │
              └──────────┬─────────┘      └─┬──────────────────┘
                         │                    │
            ┌────────────▼────────────────────▼───────────┐
            │          三层自修正（Agent 核心）             │
            │  L1 动作合法性修正 → 点不到/换定位/重试       │
            │  L2 路径重规划修正 → 死路/卡关→换走法         │
            │  L3 策略反思修正 → 整轮失败→总结原因→再跑     │
            └──────────────────────┬───────────────────────┘
                                   │
      ┌──────────────────┬─────────┼─────────────┐
      │                  │         │             │
  自动测试          自动调参    关卡生成验证    UI爆框检测
```

## 快速开始

### 方式一：试玩测试控制台（推荐，参赛演示）

```bash
# 1. 安装
pip install -r requirements.txt

# 2. 指定本机浏览器（没有执行 playwright install 时）
#    Windows PowerShell:
$env:PLAYWRIGHT_CHROMIUM_PATH="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 3. 启动
playranger server --port 8000
# 或 python -m playranger.api.main

# 4. 打开 http://127.0.0.1:8000/app/
#    - 第一个注册的账号自动成为管理员
#    - 新建任务填入游戏 URL 或本地 HTML 路径
#    - 控制台会显示内存、建议并发、任务队列、逐步轨迹与缺陷
```

控制台能力：

- 账号注册 / 登录 / 退出，首位用户管理员，SQLite 持久化；
- 按可用内存自动计算建议并发，可在 1-8 之间随时扩缩；
- 暂停 / 恢复调度、单任务停止、任务重跑、全部停止；
- 任务看板实时显示状态、步数、缺陷数；详情页展示逐步动作与验证结果；
- 缺陷审查：证据包、源码定位、修复 patch、`git apply --check` 与沙箱验证；
- 实时事件流，团队可按 `since_id` 增量审查；
- 无头模式 rAF 被节流时自动切 CDP Performance 指标兜底（不伪造 FPS）。

安全边界：修复只在 `output/fix_sandbox/` 副本中应用，原始源码只做只读校验；
源码目录需在 `PLAYRANGER_WORKSPACE_ROOTS` 白名单内。

### 方式二：命令行

```bash
# 安装
pip install -e .
playwright install chromium

# 配置
cp .env.example .env
# 编辑 .env，填入你的 API Key 或 DGX Spark 地址

# 跑冒烟测试
pytest tests/ -v
```

## 多端支持（WebGL / Android / Windows / Linux / iOS）

> 先说实话：**不是「什么游戏都能测」**。完整矩阵与限制见
> [docs/COMPATIBILITY_多端支持矩阵.md](docs/COMPATIBILITY_多端支持矩阵.md)。

| 目标 | 适配器 | 宿主 | 状态 |
|------|--------|------|------|
| HTML5 / DOM 网页游戏 | `web` | Win/macOS/Linux | ✅ 生产可用 |
| Canvas / WebGL 网页游戏 | `web` + `webgl.py` | Win/macOS/Linux | ✅ 可用（视觉断言） |
| Android（真机/模拟器） | `android` (ADB) | Win/macOS/Linux | ✅ 可用（需 adb + 设备） |
| Windows 桌面游戏 | `windows` (Win32) | 仅 Windows | ✅ 可用（需 Pillow） |
| Linux 桌面游戏 | `linux` (X11) | 仅 Linux | ✅ 可用（需 xdotool） |
| iOS | `ios` (WebDriverAgent) | 仅 macOS | ⚠️ 需 Mac + WDA |
| Unity（AltTester） | `unity-altester` | 任意 | 🚧 协议占位 |

主机平台（PS/Xbox/Switch）、云游戏串流、强反作弊线上游戏：**明确不支持**。

```bash
# 一秒钟看清本机能测哪些端、缺什么、怎么补
playranger doctor

# 原生平台冒烟观察（默认只截图，不点击）
playranger native --platform android --package com.example.game --steps 10
playranger native --platform windows --window-title "MyGame" --steps 10
playranger native --platform linux   --window-title "MyGame" --steps 10
playranger native --platform ios     --bundle-id com.company.game --steps 10

# 允许点击客户区中心（谨慎）
playranger native --platform windows --window-title "MyGame" --auto-tap
```

`native` 输出 `summary.json` / `summary.md` / `trace.jsonl` / 逐帧截图。
连续帧指纹相同只会标记为「疑似冻结」，**不会**冒充已确认缺陷。

WebGL 探测（程序内）：

```python
from playranger.core.engines.adapters.webgl import detect_web_render_mode, is_vision_required

info = detect_web_render_mode(page)      # {"mode": "webgl2", "renderer": "ANGLE (NVIDIA...)"}
if is_vision_required(info):             # Canvas/WebGL 没有 DOM 可断言
    ...                                  # 走截图 + VLM
```

## 快速使用

```bash
# 测试一个网页游戏
playranger test --url https://your-game-url.com --goal "complete the game"

# 回放一个 bug
playranger replay bugs/bug-001/info.json
```

## 项目结构

```
playranger/
├── playranger/
│   ├── core/
│   │   ├── brain/         # 大脑：LLM 规划 + VLM 视觉
│   │   ├── engines/       # 引擎：试玩 + 视觉
│   │   └── agent/         # Agent：ReAct + 记录 + 回放 + 验证
│   ├── sdk/               # PlayRanger SDK
│   ├── api/               # FastAPI 后端
│   └── web/               # Web 控制台
├── examples/demo_game/    # 示例游戏（带预置 bug）
├── tests/                 # 测试
└── scripts/               # 部署脚本
```

## 接入你的游戏

### SDK 模式（推荐，测试更精准）

在游戏里实现两个函数：

```javascript
window.__playranger__ = {
  get_state() { return { scene: "menu", hp: 100, interactables: [...] }; },
  do_action(action) { /* 执行动作，返回 { ok: true } */ },
};
```

### 盲测模式（零代码接入）

什么都不用改，直接给 URL，PlayRanger 靠 DOM + 视觉推断。

## 奖项定位

- 🎯 **主攻**：最佳 AI 原生游戏创作工具（DGX Spark 实体机）
- 🎯 **次攻**：最佳 Agent 工程实践（¥4,000）
- 🎯 **叠buff**：人气奖（点赞数）

## 开发路线

- **Day 1-7**：核心骨架 + 自动测试 + 一键回放 + Web 控制台
- **Day 8-14**：自动调参 + UI 爆框检测 + 健康容错 + SDK 双版本
- **Day 15-21**：P1/P2 收尾 + 真实游戏实测
- **Day 22-25**：文档 + 创作手记 + 提交材料

## 开源与下载

- **仓库**：<https://github.com/Frank2673/playranger>（公开，可下载、可提 Issue）
- **协议**：[MIT](LICENSE) —— 可商用、可修改、可再分发，保留版权声明即可
- **下载试用**（三选一）：

```bash
# 1) 直接克隆
git clone https://github.com/Frank2673/playranger.git
cd playranger && pip install -e .

# 2) 从源码包安装
pip install git+https://github.com/Frank2673/playranger.git

# 3) 下载 Release 里的 wheel
pip install playranger-*.whl
```

- **30 秒自检**：`playranger doctor` —— 不装浏览器也能跑，会告诉你缺什么
- **贡献**：Fork → 新建分支 → 提交 PR；CI 会在 Ubuntu/Windows/macOS × Python 3.10/3.12 上跑单测
- **安全边界**：修复只在 `output/fix_sandbox/` 副本中进行；原始源码只读校验

## 免责声明

本项目仅用于**自研游戏**的自动化测试与研究。
请勿用于绕过反作弊、破解、未授权访问或任何违反服务条款的场景。
在带反作弊的线上游戏中使用合成输入可能导致封号，风险自负。

## License

MIT
