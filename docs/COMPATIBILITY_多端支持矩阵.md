# 多端兼容性矩阵（诚实版）

> 最后实测：2026-11-23 · Windows 11 · Python 3.13 / 3.12
>
> 结论先行：**不是"什么游戏都能测"**。
> 网页游戏（HTML5/Canvas/WebGL）是完整支持；Android/Windows/Linux/iOS 通过原生适配器支持
> "截图 + 输入注入 + 可选控件树"，但对**原生渲染引擎（Unity/Unreal/Godot）只能做视觉级判定**，
> 且不触碰反作弊、不做注入、不测主机平台。

---

## 1. 一眼看清：哪些能测，测到什么程度

| 目标 | 适配器 | 宿主系统 | 状态读取 | 动作执行 | 成熟度 | 实测 |
|------|--------|----------|----------|----------|--------|------|
| HTML5 / DOM 网页游戏 | `web` | Win / macOS / Linux | DOM 节点 + SDK | 鼠标 / 键盘 / 导航 | ✅ 生产可用 | 已有 C/B1/B2 实验数据 |
| Canvas / WebGL 网页游戏 | `web` + `webgl.py` | 同上 | Canvas 存在性、WebGL 版本、GPU renderer；DOM 通常为空 | 鼠标 / 键盘 | ✅ 可用（断言须走视觉） | 2048 真实项目对照实验 |
| Android 应用/游戏 | `android` | Win / macOS / Linux + 真机 | `uiautomator` XML（原生渲染时为空） | `adb shell input` | ✅ 可用（需真机） | 本机无设备，逻辑已单测 |
| Windows 桌面游戏 | `windows` | 仅 Windows | UIA（可选）+ 截图 | Win32 `SetCursorPos/mouse_event/keybd_event` | ✅ 可用 | 1600×952 截图实测通过 |
| Linux 桌面游戏 | `linux` | 仅 Linux (X11) | 截图 | `xdotool` | ✅ 可用（Wayland 受限） | 无 Linux 宿主，逻辑已单测 |
| iOS 应用/游戏 | `ios` | 仅 macOS | XCUITest 节点树 | WDA HTTP | ⚠️ 需 Mac + WDA | 无 Mac 宿主，逻辑已单测 |
| Unity（AltTester 协议） | `unity-altester` | 任意 | ❌ 未实现 | ❌ 未实现 | 🚧 协议占位 | 明确返回不可用 |
| 主机（PS/Xbox/Switch） | — | — | — | — | ❌ 不支持 | — |
| 云游戏串流 | — | — | — | — | ❌ 不支持 | — |

成熟度定义：
- ✅ 生产可用：有真实运行证据，失败路径有明确报错
- ⚠️ 有条件可用：依赖外部硬件/工具链，缺一不可
- 🚧 占位：只定义协议与失败原因，不假装能用
- ❌ 不支持：明确不做，避免误导

---

## 2. "什么游戏都能测吗？" —— 按游戏类型回答

### 2.1 能测（有真实链路）

| 类型 | 例子 | 为什么能测 | 断言精度 |
|------|------|-----------|----------|
| 纯 DOM 网页游戏 | 卡片、答题、H5 小游戏 | DOM 可读，SDK 可注入 | 高（状态断言） |
| Canvas 2D 游戏 | 2048、扫雷、贪吃蛇 | 截图 + 帧指纹 + VLM | 中（视觉断言） |
| WebGL 游戏 | Three.js / Phaser 3 / PixiJS | 同上，额外识别 WebGL 版本与 GPU | 中（视觉断言） |
| Android 标准控件界面 | Compose / Flutter 带语义 | `uiautomator` 节点树 | 高 |
| Android 原生渲染游戏 | Unity/Unreal 手游 | 截图 + 坐标点击 | 中 |
| Windows 窗口化游戏 | 独立游戏、模拟器 | Win32 输入 + 截图 | 中 |
| Linux X11 游戏 | 开源游戏、Wine 运行 | xdotool + 截图 | 中 |
| iOS XCUITest 可见界面 | 原生 App / 部分游戏 | XCUITest 节点树 | 高 |

### 2.2 受限（能跑但结论要打折）

| 场景 | 限制 | 后果 |
|------|------|------|
| Unity / Unreal / Godot 原生渲染 | 无控件树 | 只能靠视觉；"按钮点不到"类 bug 需 VLM 复核 |
| 全屏独占 / DirectX 独占 | 截图 API 可能拿到黑屏 | 建议改窗口化或无边框窗口 |
| 高 DPI / 多显示器缩放 | 坐标与截图可能错位 | 已在 Windows 适配器用 `ClientToScreen` 校正客户区，仍需按 DPI 复核 |
| Wayland 原生窗口 | xdotool 无法注入 | 请用 XWayland 会话运行游戏 |
| 视频/过场动画 | 帧一直变，冻结检测无效 | 需要 OCR/字幕比对，当前未实现 |
| 强反作弊（EAC / BattlEye / Vanguard） | 合成输入可能被判定为外挂 | **不要在有反作弊的线上游戏上跑**，仅用于自研包 |

### 2.3 明确不测

- 主机平台（PS5 / Xbox / Switch）：无公开输入注入通道
- 云游戏串流：编码延迟与模糊会破坏截图判定
- 需要内核驱动/内存读写的测试
- 破解、绕过付费墙、绕过 DRM

---

## 3. 各端前置条件（缺什么就报什么）

### 3.1 Web / WebGL

```bash
pip install playwright
playwright install chromium
playranger doctor          # 应显示 Web ✅
playranger test https://your-game.com --goal "通关"
```

无外网时复用本机浏览器：

```powershell
$env:PLAYWRIGHT_CHROMIUM_PATH="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
```

WebGL 探测（程序内）：

```python
from playranger.core.engines.adapters.webgl import detect_web_render_mode, is_vision_required
info = detect_web_render_mode(page)
# {"mode": "webgl2", "renderer": "ANGLE (NVIDIA...)", "dom_nodes": 8}
if is_vision_required(info):
    ...  # 走截图 + VLM，不要用选择器断言
```

### 3.2 Android

```bash
# 1) 安装 platform-tools，使 adb 可用
adb devices            # 必须看到 device 状态
# 2) 观察（默认不点击）
playranger native --platform android --package com.example.game --steps 10
# 3) 允许点击客户区中心
playranger native --platform android --package com.example.game --steps 10 --auto-tap
```

环境变量：`PLAYRANGER_ANDROID_SERIAL`、`PLAYRANGER_ANDROID_PACKAGE`、`PLAYRANGER_ADB_PATH`。

### 3.3 Windows

```powershell
playranger native --platform windows --window-title "MyGame" --steps 10 -o output/native
# 需要重启功能时：
playranger native --platform windows --window-title "MyGame" --exe "C:\Games\MyGame\game.exe"
```

依赖：`pip install pillow`（截图）；可选 `pip install uiautomation`（标准控件树）。

> 注意：截图要求窗口在前台。最小化、被完全遮挡或被独占全屏时会失败/黑屏。

### 3.4 Linux

```bash
sudo apt install xdotool imagemagick    # 或 scrot / grim
echo $DISPLAY                            # 必须是 X11 或 XWayland
playranger native --platform linux --window-title "MyGame" --steps 10
```

Wayland 原生窗口无法注入；请用 `GDK_BACKEND=x11` 或游戏自带的 X11 模式。

### 3.5 iOS

```bash
# 在 macOS 上启动 WebDriverAgent 后：
curl http://127.0.0.1:8100/status        # 必须返回 JSON
playranger native --platform ios --bundle-id com.company.game --steps 10
```

限制：免费证书签名的 WDA 每 7 天需重签；本仓库不打包 Xcode 工程。

---

## 4. 一次体检看清全貌

```bash
playranger doctor            # 人类可读
playranger doctor --json     # 给 CI / 平台调用
playranger doctor --network  # 额外实测 LLM/VLM 连通性
```

输出包含：宿主信息、每个平台可用性与原因、修复步骤、诚实边界。

---

## 5. 架构：适配器协议怎么保证"不撒谎"

所有适配器实现同一协议（`adapters/base.py`）：

```python
class EngineAdapter(Protocol):
    name: str
    def capabilities(self) -> dict: ...   # available / reason / actions
    def get_state(self) -> dict: ...      # scene / frame_sig / ui_tree_summary
    def get_actions(self) -> list: ...
    def execute(self, action_type, target=None, params=None) -> dict: ...
    def reset(self) -> None: ...
```

三条硬规则：

1. **`capabilities()` 必须先回答"可不可用"**，不可用时给 `reason`
2. **不可用就抛 `AdapterUnavailable`**，不返回假状态
3. **每个动作都带 `command` 原始记录**（命令、退出码、耗时、输出摘要），可回溯

未安装依赖时不会导致整个包导入失败：`play_engine.py` 对 Playwright 做了延迟导入，
`doctor` 因此在"裸环境"下也能运行并给出安装建议。

---

## 6. 已知缺口（下一阶段）

| 缺口 | 影响 | 计划 |
|------|------|------|
| 原生适配器未接入 LLM ReAct 回路 | 目前原生侧是"观察 + 有限动作"冒烟，不是自主探索 | 将 `NativeAdapterBase` 包装成 `PlayEngine` 兼容门面 |
| Unity AltTester 未实现 | Unity 真值读取不可用 | 接入 AltTester WebSocket 协议 |
| 多机 worker | 只能单机跑 | 设备农场 + 任务分发 |
| 感知哈希去重 | 只做逐字节帧指纹 | 已内置 `dhash`，待接入报告链 |
| iOS 真机验证 | 无 macOS 环境 | 需要外部 Mac 跑通 CI |

---

## 7. 复现本文档的实测命令

```bash
# 体检
playranger doctor

# Windows 截图链路（本机实测：1600×952 PNG，139 KB）
playranger native --platform windows --window-title "任意窗口" --steps 3

# 单测（无硬件依赖）
pytest tests/test_adapters.py -v
```

实测记录（2026-11-23，Windows 11）：
- `doctor`：Web ✅（miniforge Python 3.13 + playwright）、Windows ✅、Android ❌（无 adb）、Linux ❌（宿主不符）、iOS ❌（宿主不符）
- `native --platform windows`：3/3 帧截图成功，静止窗口被正确标记为 `suspected_freeze`
