# 技术复盘：演示录屏中的 LLM Mock 回退事件

> 事件对象：RegexRealm（团结引擎 WebGL）演示任务 `33b49e9c8b7e`
> 发生时间：2026-11-23 15:36:14Z – 15:37:02Z（UTC，时长 44.8 秒）
> 复盘时间：2026-11-23
> 证据原则：本文所有结论均可在开源仓库与本任务产物目录中复核，不做无法复现的推断。

---

## 1. 事件概述（发生了什么）

参赛录屏中的演示任务在 8 个执行步内**没有产生任何真实交互**：逐步动作全部是
`wait`，动作原因字段全部为 `mock_fallback`，最终 `result.json` 记录
`brain_source: "mock"`、`success: false`。

这不是"AI 试玩后未发现缺陷"，而是**推理服务在任务启动时即不可用，系统静默降级到了
本地 Mock 大脑**。录屏展示的是真实产品链路（调度、浏览器接管、逐帧截图、证据链、
报告渲染），但不包含真实 LLM 规划行为。本复盘用于向评审如实交代成因、影响与整改。

## 2. 影响范围与诚实边界

| 维度 | 真实情况 | 不可宣称的内容 |
|------|----------|----------------|
| 任务调度/Worker/事件流 | 真实运行，数据库 events 表 10 条记录可查 | — |
| 浏览器与被测游戏 | Playwright 真实启动，RegexRealm WebGL 真实加载（step 4 起截图由 ~21KB 加载页变为 ~317KB 渲染帧） | 不代表 AI 对游戏做了有效探索 |
| 8 个执行步 | 全部 `wait / mock_fallback`，L1/L2/L3 自修正次数均为 0 | 不代表 ReAct 回路的真实决策质量 |
| BUG-001（性能告警） | 由 `play_engine.perf` 性能采样通道**自动登记**，`conclusion=auto_warning`、triage 结论 `human_required`、复现 0/0 | 不是 LLM 发现的功能缺陷，不计入"AI 发现缺陷数" |
| 证据链 | 截图 sha256 哈希链（root hash `d8cbf78c…3e4ab`）真实生成 | 哈希链只能证明证据未被篡改，不能证明测试有效 |

## 3. 时间线（UTC，取自 platform.db 与任务产物）

| 时间 | 事件 | 来源 |
|------|------|------|
| 15:36:14 | 任务入队，Worker 领取并启动 | events #4–#6 |
| 15:36:14 | `create_brain()` 健康检查失败，降级 MockLLMClient（任务内无独立事件，见第 4 节取证） | brain_factory.py |
| 15:36:41 | SDK 注入超时（5000ms × 2 次），自动降级为无 SDK 模式（原生键鼠 + 视觉） | events #7（warn） |
| 15:36:47–15:36:58 | 8 个步骤依次执行，动作全部 `wait / mock_fallback` | steps.jsonl |
| 15:36:58 | 性能采样通道登记 BUG-001（长任务 689ms > 阈值 200ms） | bugs/BUG-001/defect.json |
| 15:36:59 | 任务结束：8 步、缺陷 1 条（auto_warning） | events #8 |
| 15:37:02 | 停止请求收尾，状态置为 cancelled | events #9–#10 |

注：SDK 降级与 LLM Mock 回退是**两个相互独立的问题**，前者已有 warn 事件并在
`runner.py` 中实现自动恢复；后者当时没有任何运行时告警，是本次复盘的主要对象。

## 4. 根因分析

### 4.1 直接原因：LLM 账户余额不足，健康检查返回 HTTP 402

复盘时使用任务同款环境变量（`.env`：`LLM_API_BASE=https://api.deepseek.com/v1`、
`LLM_MODEL=deepseek-flash`、Key 长度 35、前缀 `sk-`）复放 `LLMClient.health_check()`
的最小请求（`max_tokens=1`），实测返回：

```text
HTTP 402
{"error":{"message":"Insufficient Balance (request_id: c59e2658-859f-41db-b012-b5dcda5fb763)",
          "type":"insufficient_balance_error","code":"invalid_request_error"}}
```

结论：

- **Key 本身有效**（通过鉴权，否则返回 401），模型名 `deepseek-flash` 合法
  （DeepSeek V4.1 Flash 的官方模型名，见 DeepSeek API 文档更新日志）；
- 失败原因是**账户余额不足（402）**。前期排查笔记中"Key 无效"的表述不准确，以此处实测为准。

### 4.2 放大因素：备选后端全部未配置

`brain_factory.create_brain()` 的候选链路为：

1. OpenAI 兼容接口（DeepSeek）—— 402 失败；
2. DGX Spark 本地推理 —— 未设置 `DGX_SPARKS_HOST`，跳过；
3. 魔搭 API —— 未设置 `MODEL_SCOPE_API_KEY`，跳过；
4. MockLLMClient —— 永远可用，成为最终兜底。

即唯一配置的推理通道失败后，系统没有第二可用后端，直接落到底层兜底。

### 4.3 兜底行为：Mock 大脑恒定输出 wait

`MockLLMClient.chat_pydantic(PlanOutput)` 的设计行为是返回
`action_type="wait", reason="mock_fallback", confidence=0.0`
（见 `playranger/core/brain/brain_factory.py`）。因此 8 步全部 wait 是确定性结果，
不是随机波动，也不是游戏画面导致的决策。

### 4.4 工程缺陷（真正需要整改的点）：静默兜底，无预检阻断

`playranger/platform/runner.py` 第 247–248 行创建大脑后仅读取
`brain_source` 并在收尾写入 `result.json`（第 314 行），**没有任何分支在
`source == "mock"` 时阻断任务或发出显著告警**。任务状态仍按正常流程置为
`completed`，控制台与录屏中也没有"演示模式/Mock 模式"标识。这使得"推理不可用"
在界面上看起来像"任务正常跑完"，是本次事件中最需要修复的设计缺口。

### 4.5 体检命令的既有局限

`playranger doctor` 默认只检查环境变量是否存在（Key 非空即判 available），
只有加 `--network` 才发起真实健康检查。本环境 Key 非空，静态体检通过，
无法提前暴露 402。这一行为在 doctor 源码注释中有说明，但对演示场景不够友好。

## 5. 为什么没有演变成"伪造成功"

尽管兜底是静默的，系统的数据层仍保留了完整的诚实信号，使事后复盘可以还原真相：

1. `result.json` 显式记录 `"brain_source": "mock"`；
2. `steps.jsonl` 每一步都带 `"reason": "mock_fallback"` 与 `confidence` 链路；
3. BUG-001 被标记为 `auto_warning` + `human_required`，复现率字段如实为 0/0，
   来源通道写明 `perf_sample / play_engine.perf`，未冒充智能发现；
4. 截图证据链带 sha256 链式哈希，证据不可事后替换；
5. 降级链路本身有单元测试覆盖（`tests/test_day2_brain.py` 断言
   `create_brain({}).source == "mock"`），Mock 行为是被显式测试而非偶然行为。

## 6. 整改项（含状态与验收标准）

### 已具备（本次事件中被验证有效）

| 能力 | 位置 | 证据 |
|------|------|------|
| 大脑来源落盘 | runner.py → result.json `brain_source` | 本任务结果文件 |
| 逐步动作原因留痕 | ReActLoop → steps.jsonl `reason=mock_fallback` | 8 条步骤记录 |
| 自动告警不冒充发现 | triage 结论 `human_required`、复现 0/0 | defect.json |
| SDK 缺失自动降级 + warn | runner.py 第 270–280 行 | events #7 |
| 降级链路单测 | tests/test_day2_brain.py | CI 全绿 |

### 待实现（按优先级）

| # | 整改项 | 验收标准 |
|---|--------|----------|
| 1 | **任务预检阻断/确认**：`create_brain()` 返回 mock 时，API 返回明确警告，控制台需二次确认才允许开跑；默认拒绝静默执行 | 新增单测：无可用后端时 StartTest 返回 409/警告字段；确认后任务带 `mock_confirmed=true` 标记 |
| 2 | **全链路 Mock 水印**：控制台任务卡、HTML 报告、视频对应帧出现"Mock 演示模式，不代表 AI 测试效果"角标 | 报告快照测试断言水印存在；真实大脑运行时水印不出现 |
| 3 | **doctor 默认联网预检**：首次运行引导执行一次 `--network` 实测，并把 401/402/超时分别给出可操作建议（充值/换 Key/换模型/配备用后端） | 对 402 响应的提示文案包含"账户余额不足"与备用后端配置项 |
| 4 | **备用后端配置指引**：在 `.env.example` 与部署文档中写明双后端配置（主后端 + 魔搭/DGX 兜底），避免单通道故障即落 Mock | 文档评审；集成测试模拟主后端 5xx 时自动切到备用通道 |
| 5 | **演示前清单（runbook）**：录屏/演示前强制执行一次 health check 并截图留档 | 本仓库 docs 增加演示前检查清单，复盘后首次演示按清单执行 |

## 7. 复测计划（整改有效性验证）

1. 对 DeepSeek 账户充值或更换可用 Key，重新执行最小 health check，确认返回 200；
2. 用与本次完全相同的输入复跑：`game_url=http://127.0.0.1:8123/index.html`、
   goal 不变、`max_steps=8`；
3. 断言新任务 `result.json` 中 `brain_source == "openai-compatible"`；
4. 断言 `steps.jsonl` 至少出现一个非 `wait` 动作（click/key_press 等），
   且不再出现 `reason=mock_fallback`；
5. 两次任务产物并列归档，作为"问题—修复—复测"完整证据链补充进 `docs/demo/`。

## 8. 对参赛填报材料的口径约束

- 视频与截图可以证明：产品形态、调度链路、WebGL 实机适配、证据链与报告工程化；
- 不得表述为"AI 自动试玩并发现 N 个缺陷"；BUG-001 只能称为"性能采样自动告警"；
- 建议在表单附件/说明中直接引用本文第 2 节的边界表与第 4.1 节的 402 取证，
  把一次基础设施故障转化为对"诚实失败"工程原则的展示。

## 9. 证据索引

| 证据 | 路径（仓库/本机产物） |
|------|-----------------------|
| 逐步动作记录 | output/tasks/33b49e9c8b7e/steps.jsonl |
| 任务结果（含 brain_source） | output/tasks/33b49e9c8b7e/result.json |
| 性能告警缺陷包 | output/tasks/33b49e9c8b7e/bugs/BUG-001/ |
| 截图哈希链 | bugs/BUG-001/evidence_chain.json |
| 调度事件 | output/platform.db（events 表 #1–#10） |
| 降级链路实现 | playranger/core/brain/brain_factory.py |
| 大脑装配与来源记录 | playranger/platform/runner.py 第 240–318 行 |
| 体检命令 | playranger/platform/doctor.py |
| 降级单测 | tests/test_day2_brain.py |
