# PlayRanger「试玩队」— 功能说明 · 亮点 · 不足

> 赛道 C：自动试玩与测试 Agent　|　TRAE 好应用大赏赛参赛版
> 交付形态：本地 Web 应用（FastAPI + 浏览器 Agent + 实时控制台）+ CLI + HTTP API + Python SDK

---

## 一、作品定位

一句话：**给独立游戏开发者一支「不需要写测试脚本」的 AI 试玩队——它会自己玩、自己判断哪里不对、自己留下可审计的证据，并直接告诉你怎么修。**

传统自动化测试（GameDriver / AltTester / Playwright 脚本）要求开发者先写好「点哪里、断言什么」；PlayRanger 反过来：你只给一个游戏地址和一句目标（例如「通关第一关」），Agent 自己探索、自己发现异常、自己生成缺陷单与修复建议。

---

## 二、核心功能说明

### 1. 零脚本自动试玩（双引擎）

| 项 | 说明 |
|---|---|
| 做什么 | 输入游戏 URL + 目标，Agent 自主感知页面、规划下一步动作、执行、验证结果，循环推进直到达成目标或判定卡死 |
| 帮用户完成什么 | 替代「人工反复点、写脚本录制」的重复劳动，把探索式试玩变成一条命令 |
| 相比原有方式 | 原方式需先写测试代码或手动录屏，脚本与游戏版本强耦合、改版即失效；本方案零脚本启动，动作由 LLM 现场决策，不依赖固定选择器序列 |

两种引擎可切换：

- **SDK 精确模式**：游戏侧上报结构化状态（血量、分数、场景、可用动作），Agent 直接对真值断言，定位精度到字段；
- **盲测模式**：不接入任何 SDK，用 Playwright 抓 DOM + 截图，交由 VLM 做结构化异常识别，适用于第三方/已上线游戏。

### 2. 三层自修正（ReAct 循环）

| 层 | 触发条件 | 修正内容 |
|---|---|---|
| L1 动作层 | 单步动作执行失败/被校验拦截 | 换动作参数、换目标元素重试 |
| L2 路径层 | 连续多步未推进目标 | 回退到上一个稳定状态，换探索路径 |
| L3 策略反思 | L2 连续失败达到阈值 | 反思目标是否可达，聚合为「策略卡死」缺陷并终止，避免无限空转 |

帮用户完成什么：把「Agent 跑飞了没人管」变成可预期的失败——失败也会沉淀成一条有结论的缺陷，而不是静默超时。

### 3. 动作合法性硬校验（ActionValidator）

执行前对每个动作做四类校验：动作白名单、前置条件（元素可见/可点击）、参数范围、枚举允许值。

相比原有方式：LLM 幻觉出的非法动作（点不存在的按钮、传越界参数）在落盘前就被拦截，不会污染执行轨迹，也不会把「Agent 自己点错」误报成「游戏 Bug」。

### 4. 经验记忆（状态签名相似度召回）

| 项 | 说明 |
|---|---|
| 做什么 | 把每一步的状态（场景、血量、分数等）压成签名，按加权相似度检索历史相似局面，复用当时有效的动作 |
| 帮用户完成什么 | 减少重复探索，第二次跑同一游戏收敛更快；跨局积累「这个局面该按哪个键」的经验 |
| 相比原有方式 | 传统脚本无记忆，每次都从零；本方案零依赖（无向量库）、离线可跑，命中即复用 |

### 5. 缺陷自动分级（规则驱动，可解释）

- **严重度 5 档**：`info / minor / major / critical / blocker`，依据异常类型 + 是否阻断主流程 + 状态偏差幅度打分；
- **结论 3 档**：`auto_confirmed`（多通道证据一致，可直接进工单）/ `auto_warning`（单通道，建议复核）/ `human_required`（证据不足或涉及主观判断）。

相比原有方式：人工看几十条录像逐条判断，分级口径因人而异；本方案规则固定、可复算，同一批缺陷每次分级结果一致。

### 6. SHA-256 哈希链证据链

每个缺陷包内的截图与产物按顺序登记：`prev_hash + file_sha256 + meta → chain_hash`，形成不可静默篡改的链。

`EvidenceChain.verify()` 可校验：① 文件是否被替换 ② 是否被删除 ③ 链是否断裂。

相比原有方式：截图文件夹谁都能改，出了争议无法自证；本方案可自证「这条结论所依据的截图，就是当时那张」，对甲方验收、外包对账、多人协作场景是刚需。

### 7. 一键复现

把缺陷发生前的动作序列抽出来，生成可回放脚本（CLI `replay` 子命令）；超过阈值的长轨迹保留尾部并标记 `truncated`。

帮用户完成什么：把「我这边复现不了」的扯皮成本降到一次点击。

### 8. Bug 溯源（Provenance）

每条缺陷输出结构化溯源信息：

- **触发步 / 触发动作**：第几步、什么动作（如 `#restart`）；
- **触发链**：状态从什么值变到什么值、期望是什么（例如「状态 `hp` 在触发前后均为 40，期望 100（该变的没变）」）；
- **字段级状态差异**：只保留真正变化的字段，自动忽略 `timestamp/fps/history` 等噪声字段；
- **检测通道**：SDK 状态比对 / 视觉检测 / 断言校验 / 运行时异常 / 策略卡死 / 动作轨迹，可多通道并存；
- **截图证据**：每张截图标注对应步骤与哈希；
- **复现动作序列**：可直接重放的步骤清单。

相比原有方式：人工测试报告通常只有「第 N 步点了 X，出现 Y」一句自然语言；本方案把「哪一步、哪个字段、由什么变什么、被哪条通道发现」全部结构化，研发不需要二次追问。

### 9. 修复深化方案（Fix Advisor）

对 19 类异常（`hp_not_reset / score_mismatch / ui_display_error / interaction_blocked / strategy_stuck / crash_page / frozen_screen / text_overflow / performance …`）分别给出：

- **可能根因**（按概率排序，含实测状态差异特化，如「重置逻辑漏了 `hp` 字段」）；
- **修复步骤**（含文件/代码提示）；
- **验证清单**（`- [ ]` 勾选式，含「从第 N 步重放」与截图对照）；
- **影响模块 / 工作量档位 / 回归风险 / 置信度**。

置信度可复算，不拍脑袋：`0.55 基线 + 溯源命中 +0.1 + 状态差异 +0.1 + 断言失败 +0.1 + 多通道证据 +0.1 + 有截图 +0.05`，上限 1.0。

相比原有方式：缺陷报告止于「有问题」，修复仍靠研发自己摸；本方案把「发现问题 → 定位问题 → 提出修法 → 给出验证方式」串成一条链，且置信度可被追问。

### 10. 可视化报告与工单导出

- **单文件离线 HTML 报告**：KPI 卡（缺陷总数、自动确认率、阻断/紧急、平均置信度、证据链通过率、平均复现率、人工复核一致率）、严重度/异常类型条形图、结论分段条、执行时间线（绿=成功 / 黄=修正 / 红=失败，超长自动等距采样）、跨版本趋势面板、同类缺陷聚类表、逐 Bug 卡片（触发链 + 状态差异 + 证据 chips + 截图画廊 + 修复方案）；
- **Markdown 工单**：每个 Bug 一份，可直接贴进 GitHub Issue / Jira / 飞书。

零 CDN、零 JS 依赖，断网可看、可邮件发送、可归档。

### 11. CLI / HTTP API / Python SDK 三种接入

- CLI：`test`（跑试玩）、`replay`（复现）、`locate`（代码级定位）、`bugs`（查缺陷）、`feedback`（复核反馈）、`server`（起服务）；
- API：`/api/v1/tests/start`、`/api/v1/bugs`、`/api/v1/bugs/{id}/provenance`、`/fix-plan`、`/api/v1/bugs/{id}/feedback`、`/api/v1/report`、`/api/v1/replay/{bug_id}` 等 12 个端点；
- SDK：`BugSummary` / `BugDetail` 直接带 `provenance` 与 `fix_plan`。

### 12. 跨版本趋势对比（新增）

| 项 | 说明 |
|---|---|
| 做什么 | 每轮跑测用 `--version` 落盘 `output/versions/<version>/summary.json`；`--baseline` 与历史版本做差分，报告输出「新增 / 修复 / 遗留 / 严重度变化」与缺陷收敛曲线 |
| 帮用户完成什么 | 回答「这个版本比上个版本好转了吗」——回归测试最关心的问题，不用人工对比两份报告 |
| 相比原有方式 | 原报告是一次性快照，只能看当前总数；本方案把每轮结果变成可比的时间序列，修没修掉、有没有引入新问题一目了然 |

缺陷匹配用「异常类型 + 归一化标题」做指纹，不依赖每轮重排的 `bug_id`。

### 13. 误报反馈闭环（新增）

| 项 | 说明 |
|---|---|
| 做什么 | `playranger feedback --bug BUG-001 --label false_positive` 记录人工复核结论，写入缺陷包 `feedback.json` 与项目级 `triage_overrides.json` |
| 帮用户完成什么 | 同一个误报不必每轮重复复核：某异常类型连续 2 次被标误报后，同类新缺陷的结论自动从 `auto_confirmed` 降为 `auto_warning`，也可显式校正该类型的严重度 |
| 相比原有方式 | 原分级规则是静态阈值，错了只能忍着；本方案让规则随项目校准，且不改历史缺陷包，证据链仍可追溯 |

报告同步输出「人工复核一致率」，用于判断自动分级是否可信。

### 14. 回放确定性增强（新增）

| 项 | 说明 |
|---|---|
| 做什么 | 动作前后统一等待「网络空闲 + 连续 2 帧 DOM 稳定 + 动画结束」（`MutationObserver` + `requestAnimationFrame` + `getAnimations`，超时自动降级）；缺陷包记录 `seed`、`started_at`、每步 `duration_ms`；回放通过 `window.__PLAYRANGER_SEED__` 注入种子，游戏支持 SDK 时再调 `setSeed` 双重保险 |
| 帮用户完成什么 | 把「我这边复现不了」变成可判定结论：回放结果分 `reproduced / partial / not_reproduced` 三态并写回 `defect.json`，CLI 退出码 0/2/1 可直接进 CI |
| 相比原有方式 | 原实现动作后直接读状态，动画未结束就点击会导致复现率偏低；现在用可观察的稳定信号替代固定 `sleep`，并在缺陷包里明确标注 `seed_mode`（`sdk` / `global` / `none`），不假装种子生效 |

### 15. 代码级修复定位（新增）

| 项 | 说明 |
|---|---|
| 做什么 | 从缺陷包提取状态字段，静态扫描 JS/HTML/Python 源码，输出 `文件:行:列`、所在函数、读写类型；按异常类型套策略（`hp_not_reset` 找重置函数遗漏、`score_mismatch` 找渲染偏移、`ui_display_error` 找未绑定字段），生成 unified diff patch 草案与置信度 |
| 帮用户完成什么 | 把「修复方案」从「该改重置逻辑」推进到「改 `index.html:181`，补一行 `state.hp = 100;`」，研发不必再从零搜代码 |
| 相比原有方式 | 原方案是规则模板 + 证据定制，不知道源码在哪一行；本方案纯标准库（JS 轻量扫描 + Python `ast`），零依赖、离线可跑，定位不到时明确降级为通用模板并说明原因 |
| 验证（自研） | 单文件内联脚本：`index.html:181` → 补 `state.hp = 100;`（置信度 90%）；`index.html:149` → 去掉 `- 200`（置信度 90%） |
| 验证（第三方） | 真实开源游戏 2048（MIT，多文件原型式代码）：3 个字段共 **4 个写点 4/4** 命中并归属到正确函数（`score`→`setup`、`over`→`move`、`won`→`move`、渲染写点→`updateScore`）；注入 3 类缺陷副本上 **3/3** 生成带 `this.` 前缀的正确 patch；单次定位中位耗时 **139 ms**（20 次采样，min 122 / max 175 ms） |

为适配第三种代码结构（原型式、`self.` 惯用法、渲染与状态分离），定位器做了三处真实修正——均由 2048 验证暴露，而不是事后补文档：

- **原型式函数归属**：`GameManager.prototype.setup = function` 原先被归到构造函数 `GameManager`，修正后归到 `setup`；
- **回调排除**：`grid.forEach(function () {})` 不再被误判成函数名 `forEach`；
- **状态前缀推断**：`self.won = true` 被识别为状态写点；patch 前缀按源码出现频次推断为 `this.`，不再硬编码 `state.`。

```bash
playranger locate output/demo/bugs/BUG-001 --source examples/demo_game
# → 策略：疑似遗漏重置　patch：@@ -181,1 +181,2 @@  + state.hp = 100;　置信度 90%

playranger --no-env locate output/2048/BUG-2048-001 --source tests/fixtures/thirdparty_2048 --json
# → fields=["score","over","won"]　hits 指向 js/game_manager.js:48/186/170 与 js/html_actuator.js:110
```

### 16. CI 稳定性与性能采样（新增）

| 项 | 说明 |
|---|---|
| CI 稳定性 | `--ci` 固化禁用 GPU / `--no-sandbox` / 固定视口 1280×720 / 降级动画；启动失败自动重试并记录 `retry_reasons`；导航与动作超时分离；`runtime_stats()` 输出 retry/viewport/seed_mode |
| 性能采样 | `--perf` 注入 rAF + `PerformanceObserver('longtask')` + `performance.memory`，产出 `avg_fps / min_fps / p95_frame_ms / long_task_count / heap_peak_mb`；超阈值自动记 `performance` 缺陷；样本不足明确返回 `insufficient_data`，不伪造 FPS。实测无头模式下 rAF 会被节流（4 步冒烟仅 2 帧），此时只报样本不足；卡顿判定需 headful/可见页面 |
| 可调阈值 | `PlayEngine(perf_thresholds={...})` 可覆盖 `min_avg_fps / max_p95_frame_ms / max_long_task_ms / max_heap_mb` |

### 17. CI/CD 插件形态（新增）

| 项 | 说明 |
|---|---|
| 产物 | `tools/ci_report.py` 把缺陷包转成 JUnit XML（进测试报告）、SARIF 2.1.0（进代码扫描面板）、Markdown 摘要 |
| 门禁 | `--fail-on blocker,critical --allow 0`：命中数超过允许值退出码 1，可直接卡 PR |
| GitHub Action | `.github/actions/playranger/action.yml`（composite）：安装依赖 → `--ci --perf` 跑测 → 门禁 → 上传产物 |
| Jenkins | `ci/jenkins/playranger.groovy`：Stage + `junit` + `archiveArtifacts` |

### 18. API Key 与项目隔离（新增）

| 项 | 说明 |
|---|---|
| 鉴权 | 设置 `PLAYRANGER_API_KEY` 后，`/api/v1/*` 必须携带 `X-API-Key`，否则 401；`/health` 放行 |
| 隔离 | `project_id` 白名单清洗防路径穿越；非 default 项目缺陷写入 `output/projects/<id>/`；`GET /api/v1/bugs?project_id=` 按项目过滤 |
| 边界 | 不做多租户计费与团队权限——只解决「同一部署下多项目不串数据」 |

### 19. 分级配置化（新增）

项目根放 `playranger.triage.json` 即可覆盖内置规则：`base_severity`（按异常类型改基线）、`evidence_weights`（证据加权）、`thresholds`（复现率→结论门槛）、`ignore`（忽略类型）。查找顺序：环境变量 → JSON → YAML（需 PyYAML）。配置非法值时丢弃而不是崩溃。

### 20. 近似语义记忆与容量衰减（新增）

在状态签名之外增加 TF-IDF + 中文字符/二元组相似度；`recall(semantic=True)` 采用「签名兜底 + 语义越阈」混合打分；容量默认 500，按「命中次数 × 半衰期衰减（30 天）」淘汰。**明确不是神经 embedding**，`stats()` 标注 `semantic_mode=tfidf_ngram_approximation`。

### 21. 批量并行调度（新增）

`playranger batch manifest.json --workers 4`：每个任务独立输出目录，线程池并发（上限 8），单任务异常隔离，汇总按「异常类型 + 标题」去重。合成任务实测：4 个各 0.3s 任务，串行 1210ms → 4 worker 304ms（3.98×）。

### 22. 引擎适配协议（新增）

`EngineAdapter` 协议（`capabilities / get_state / get_actions / execute / reset`）+ `WebAdapter` 正式封装 + `AltTesterAdapter` 诚实占位（`available=False` 并写明缺失条件）。原生手游明确抛 `AdapterUnavailable`，不静默降级成假状态。

### 23. 多用户控制台（新增，参赛主形态）

`/app/` 是真实可操作的控制台，不是静态展示页：注册 / 登录 / 退出，首位注册用户自动成为管理员；SQLite 持久化账号、会话、任务与事件；Token 采用 Bearer 鉴权，密码 PBKDF2-HMAC-SHA256（20 万次迭代 + 随机盐）。管理员可查看全部任务与用户，成员只看自己的任务。

### 24. 内存自适应并行 + 主控板（新增）

启动后读取本机可用内存与 CPU，按公式自动给出建议并发：

`floor((available_gb - 2.0) * 1024 / 1536)`，再 clamp 到 `1..min(8, cpu_count)`。

控制台提供 1-8 滑块一键扩缩、暂停 / 恢复调度、单任务停止、任务重跑、全部停止。缩容只影响后续领取，不打断运行中任务；每个任务独立浏览器实例与输出目录。

### 25. 真实执行与流程数据可视化（新增）

任务由 `PlayEngine + ReActLoop` 在真实浏览器中执行，每步回调写入 `steps.jsonl` 与 `progress.json`，控制台实时展示：任务状态、进度条、逐步动作、执行结果、修正层级、状态快照、Bug 卡片与全局事件流。任务结束后回读 `result.json` 与缺陷包，不需要二次整理。

### 26. 缺陷修复沙箱（新增）

缺陷包里的修复 patch 可以在控制台直接校验与应用：原始源码只做 `git apply --check`（非 git 目录自动用临时仓库等价校验），真正应用只发生在 `output/fix_sandbox/<id>/` 副本；可选执行测试命令，超时 120s。原目录不被修改，验证结果与沙箱路径回显。

### 27. 无头性能 CDP 兜底（新增）

无头模式 rAF 被节流、帧样本不足时，自动切换 CDP `Performance.getMetrics` 的窗口增量：`TaskDuration / ScriptDuration / LayoutDuration / JSHeapUsedSize`，输出 `mode=cdp_fallback`、`busy_ratio`、`heap_used_mb`，并明确 `fps_unavailable=true`，不伪造 FPS。

---

## 三、亮点

### 亮点 1：双引擎可切换，一个工具覆盖「自家游戏」与「别人的游戏」

同类工具二选一：GameDriver / AltTester 要求先接 SDK 写代码，Applitools 只能做纯视觉对比。PlayRanger 用同一套 Agent 内核切换两种感知通道——自家游戏走 SDK 拿真值（高精度），外部/竞品游戏走盲测（零接入）。

### 亮点 2：缺陷结论「可审计」，不是「AI 说有就有」

每条结论背后有：哈希链证据 → 字段级状态差异 → 检测通道 → 触发步 → 复现序列。任何一环都可被研发复核或推翻。这是把 AI 测试结果真正接进生产流程的前提——团队敢信，才敢用。

### 亮点 3：从「报告」到「修复方案」，链条比竞品长一段

多数工具止于「发现 + 截图 + 录像」。PlayRanger 继续输出根因排序、修复步骤、验证清单、工作量与回归风险。对独立开发者（往往没有专职 QA、也没有时间二次分析）这一段的价值远大于「多发现两条缺陷」。

### 亮点 4：LLM 只做决策，不做事实判定

状态对错由规则比对（SDK 真值 / 断言 / 视觉结构化输出）决定，LLM 只负责「下一步做什么」和「这段界面描述是否异常」。好处：结论可复算、成本可控、模型波动不会直接变成误报。

### 亮点 5：离线、零依赖、可私有化

报告是单文件 HTML，无需服务器；记忆无向量库；证据链无外部服务。可完全断网运行（配 mock 决策器），源码不外流——对怕上传未上线游戏素材的独立团队是关键门槛。

### 亮点 6：可验证，不是 PPT

- 新增 `tests/test_day5_trace_advisor_report.py` **52 项全部通过**；P1 能力（趋势对比 + 误报闭环）**15 项**、P1-3 回放确定性 **18 项**、P3-1 代码级定位 **25 项**、第三方真实游戏（2048）验证 **16 项**、P1–P3 深度构建 **34 项**新增测试全部通过；
- **平台化新增 25 项测试**：账号/首用户管理员/Token/持久化、内存并发公式、CDP 兜底、patch 校验与沙箱、并发上限/暂停/取消/重跑/权限/事件；真实浏览器 E2E 跑通 demo_game 8 步并产出轨迹与结果文件；
- 全量回归 **327 项通过**（既有 302 + 平台 25；含浏览器级用例，需注入本机 Edge：`PLAYWRIGHT_CHROMIUM_PATH`）；
- 浏览器级验收：用本机 Edge 回放演示游戏 BUG-001 **3/3 复现**（`reproduced` + `seed_mode=sdk`），并完成 `locate` 对 BUG-001/BUG-002 的真实定位；第三方 2048 上 3 个字段共 **4 个写点 4/4** 归属正确（`score`→`setup`、`over`→`move`、`won`→`move`、渲染写点→`updateScore`），注入 3 类缺陷副本 **3/3** 生成正确 `this.` 前缀 patch，单次定位中位 **139 ms**；
- `tools/smoke_new_features.py` 7 段冒烟全通过，`tools/demo_report.py` 可一键生成 4 个演示缺陷包 + 报告 + 4 份工单。

### 亮点 8：能进流水线，而不是「本地跑一次看看」

CI 场景被当成一等公民：固定启动参数、失败重试、视口与超时分级；产物直接是 JUnit XML、SARIF 与 Markdown 摘要；`--fail-on blocker,critical` 可按严重度卡住 PR。也就是说，PlayRanger 的输出不需要二次转换就能进 GitHub Actions / Jenkins 的现有面板。

> 诚实边界：PR 评论摘要尚未实现；10 次连续跑测成功率、真实仓库 PR 标红未采数。

### 亮点 7：跨代码结构的第三方源码定位，而不是「只对自己的演示游戏有效」

多数「缺陷定位」能力只在自研 Demo 上演示，因为 Demo 是自己的代码风格。PlayRanger 用两个结构完全不同的真实游戏验证了泛化：

| 维度 | 自研演示游戏 | 第三方 2048（上游 commit `478b6ec…`，MIT） |
|---|---|---|
| 代码组织 | 单文件内联 `<script>` | 10 个 JS 文件，按状态 / 渲染 / 输入 / 存储分层 |
| 函数写法 | 普通函数声明 | 原型式 `GameManager.prototype.setup = function` |
| 状态引用 | `state.hp` | `this.score` / `self.won` |
| 定位结果 | `index.html:181`、`:149` | `js/game_manager.js:48`、`:186`、`:170`、`js/html_actuator.js:110` |
| patch 前缀 | `state.` | `this.`（按源码推断，非硬编码） |

差异化的关键不在「能扫 JS」，而在**定位器会随代码结构自适应**：函数归属、`self` 别名、回调误判、patch 接收者前缀都由源码上下文推断。2048 的源码用 SHA-256 清单锁定、未做任何修改（`PROVENANCE.json`），验证不是靠改游戏迎合工具。

### 亮点 9：从「单机命令行」到「团队可审查的控制台」

多数自动试玩工具止于一条命令或一份报告，团队无法在过程中干预。PlayRanger 把执行面（浏览器 Agent）与控制面（账号 / 队列 / 事件 / 修复沙箱）分开：开发者能在任务运行时看到每一步动作与验证结果，随时暂停、扩缩、停止；管理员能看到全局并发与所有任务。这让「自动试玩」从个人脚本变成可演示、可协作的产品形态。

> 诚实边界：当前是单机单进程调度 + SQLite，适合本地/内网团队；多机分布式与云端多租户未实现。

---

## 四、不足（如实）

### 技术层

1. **视觉检测依赖 VLM**：有延迟与调用成本；对动态特效、3D 场景的误报率仍需真实项目校准。
2. **盲测模式对 Canvas/WebGL 游戏可读性有限**：DOM 拿不到状态时只能靠截图，定位精度下降。第三方游戏若把状态放在 `localStorage` 或 Canvas 内部，还需要为每个游戏写只读探针才能拿到真值，通用探针协议尚未抽象（2048 的状态在 `localStorage.gameState`，本次仅静态定位，未做运行时状态断言）。
3. **记忆是状态签名相似度，不是语义向量** —— 部分收敛：已加 TF-IDF + 中文字符/二元组近似语义与容量/半衰期衰减，跨场景名的语义召回可用；但仍是**词频近似**，不是神经 embedding，跨游戏迁移「步数下降 ≥20%」尚未实测。
4. **修复方案是「规则模板 + 证据定制」，不是代码级自动修复** —— 部分收敛：新增静态扫描定位（JS/HTML 轻量扫描 + Python `ast`），可输出 `文件:行:列` 与 patch 草案；已在演示游戏与第三方 2048 上验证：2048 的 4 个写点归属 4/4、三类注入缺陷 patch 3/3 正确。但仍是「定位 + 建议」而非自动改写，未接入完整调用链 / 跨文件数据流分析；目前仅覆盖「遗漏重置 / 渲染偏移 / 渲染未绑定」三类策略，像重复事件绑定、输入节流、存档字段写错这类逻辑缺陷，预期只能定位到函数、给不出可直接应用的 patch（已作为对照实验的预注册失败样本）。静态复核还暴露两个边界：①「遗漏重置」策略要求重置函数内**完全查不到**该字段写点，若只破坏单条重置分支（如 `this.score = this.score || 0;`），策略会拒绝出 patch；②多写点文件里写点权重最高，Top-1 可能落在同函数更早的赋值（如 `setup:42` 而非注入行 `:48`），目前靠 Top-3 + patch 兜底，不保证第一条即命中。
5. **回放依赖动作序列** —— 部分收敛：已用「网络空闲 + 连续 2 帧 DOM 稳定 + 动画结束」替代固定 sleep，并注入 `seed` / 记录 `seed_mode`；游戏自身不支持种子、或用时间戳/真随机时，复现率仍会受影响。当前全局种子只写入 `window.__PLAYRANGER_SEED__`，不会自动覆写 `Math.random()`；2048 这类直接调用 `Math.random` 的游戏，需要实验侧的种子垫片才能确定性回放。
6. **目前以 Web 游戏为主** —— 部分收敛：已定义 `EngineAdapter` 协议与 `WebAdapter`，并提供 `AltTesterAdapter` 占位；但 AltTester 闭环未验证（缺 Unity 工程与 AltServer），原生手游仍明确不支持。
7. **单机串行执行** —— 已收敛：新增 `playranger batch --workers N` 线程池并行，以及控制台调度器（内存建议并发 + 1-8 手动扩缩 + 暂停/恢复/取消）。合成任务 3.98×；真实浏览器多任务并行的内存/稳定性基线仍需按机器采集，控制台会显示本机建议值但不保证压满。

### 产品层

8. **报告是单次快照** —— 已收敛：新增跨版本趋势对比，每次跑测用 `--version` 落盘快照，`--baseline` 与历史版本差分，报告输出「新增 / 修复 / 遗留 / 严重度变化」及缺陷收敛曲线。
9. **没有 CI/CD 插件形态** —— 已收敛：`tools/ci_report.py` 产出 JUnit XML / SARIF / Markdown，`--fail-on` 门禁；提供 GitHub Action（composite）与 Jenkins Shared Library。PR 评论摘要未实现。
10. **API 无鉴权、无多项目/多租户** —— 已收敛（平台侧）：`/api/console` 增加账号注册登录、Bearer Token、首用户管理员与任务归属校验；`/api/v1` 仍保留 `PLAYRANGER_API_KEY` + `project_id` 隔离。尚未实现团队邀请审批、云端多租户计费与 SSO。
11. **分级阈值基于演示游戏调校** —— 已收敛：新增 `playranger.triage.json`，可按异常类型覆盖基线严重度、证据权重、结论门槛与忽略规则；人工反馈闭环继续生效。真实项目仍需按配置校准。
12. **专项测试未覆盖** —— 部分收敛：已做性能采样（FPS/长任务/内存）；弱网、存档兼容性、多语言文本、无障碍仍未覆盖。

### 验证层

13. **浏览器相关测试在无头/CI 环境不稳定** —— 部分收敛：已固化启动参数、固定视口、失败重试、导航/动作超时分离，并提供 `--ci`；「连续 10 次成功率 ≥9/10」尚未采数。2048 原有静态级验证 + 注入副本 patch 验证（16 项测试）；**新增**浏览器级夹具验证（2 项）：同种子两次独立加载状态一致（score 104），D03 渲染不一致可由「探针 104 vs DOM 4」检出。**C 组（浏览器级 + 状态桥接 + 代码定位）已实测 5/5 全部发现**：状态类 4 个（D01/D02/D05/D06）均能由状态探针检出，UI 类 1 个（D03）由 DOM vs 状态差异检出；定位可操作率 5/5（Top-3 命中 4/5 + patch 指向 1/5）。详细数据见 `output/experiments/2048/c_group_report.json`。
14. **缺少与人工测试的对照基线**：漏报率、误报率、节省工时均无量化数据——这是后续最需要补的实验。通用方案见 `EXPERIMENT_真实项目对照实验方案.md`；针对 2048 的专项方案已细化到「10 个注入缺陷 × 三组 × 3 轮 + 人工定位基线」，见 `EXPERIMENT_2048_真实项目对照实验方案.md`。**D 组静态定位已实测**（10/10，Top-3 4/10、patch 2/10、可操作 6/10）；**C 组状态桥接已实测 5 个缺陷**（发现率 100%、定位可操作率 100%、Top-3 召回 80%）；A/B/E 仍 pending；盲测模式（B 组）发现率仍无量化数据，不能据此给真实项目的节省工时结论。
15. **修复验证到 patch 为止**：已实现 `git apply --check`、沙箱副本应用与可选测试命令，但尚未自动重跑原任务确认「缺陷消失」；跨文件调用链修复仍不在覆盖范围。
16. **平台是单机形态**：SQLite + 进程内调度适合本地/内网 Demo；多机分布式、任务持久化恢复、细粒度权限（邀请/审批/审计导出）未实现。
17. **无 LLM Key 时体验受限**：客户端的 MockBrain 只会产生保守动作，真实探索质量取决于配置的模型；Demo 需提前配置 Key。

---

## 五、真实第三方游戏验证：2048

### 5.1 为什么是 2048

- **真实第三方**：来自 `github.com/gabrielecirulli/2048`，commit `478b6ec346e3787f589e4af751378d06ded4cbbc`（2024-10-24），MIT 许可；
- **源码未改**：夹具内 14 个文件全部登记 SHA-256，`PROVENANCE.json` 与逐文件哈希一致；测试还断言目录里没有夹带任何自研代码；
- **结构不同**：10 个 JS 文件分层（状态 / 渲染 / 输入 / 存储），原型式写法 + `var self = this` + 渲染与状态分离；
- **无 SDK**：不能靠游戏上报状态，正好检验「第三方 / 已上线游戏」这条差异化路径。

### 5.2 已完成的验证与实测数据

| 验证项 | 方法 | 结果 |
|---|---|---|
| 上游完整性 | SHA-256 逐文件比对 + 目录白名单 | 14/14 一致，无夹带文件 |
| 原生源码定位 | `locate("score"/"over"/"won")` | 4/4 命中并归属到 `setup` / `move` / `move` / `updateScore` |
| 多字段缺陷定位 | `locate_bug()` 取 `initial/final/assertion` 字段 | `fields=["score","over","won"]`，置信度 0.6，不伪造 patch |
| 注入缺陷 patch | 在 tmp 副本注入 3 类缺陷（遗漏重置样本删除 `setup` 内两处 `score` 写点） | 3/3 策略正确，前缀均为 `this.`；若只破坏单条重置分支、字段写点仍在，策略会拒绝出 patch（实验方案 D01 即此情形） |
| 无源码降级 | 不传 `--source` | `has_hits=False`、`confidence=0.0`、说明「未提供源码目录」 |
| CLI 端到端 | `locate --json --write` | 退出码 0、JSON 正确、`code_location.json` 落盘；无源码退出码 1 |
| 定位耗时 | `locate_bug()` 连续 20 次 | 中位 139 ms（min 122 / max 175） |
| 自动化测试 | `tests/test_p3_1_thirdparty_game.py` | 16/16 通过 |
| 回归 | 全量（排除浏览器依赖） | 236/236 通过 |

### 5.3 这组验证能证明什么、不能证明什么

**能证明**：定位器不是「只对自研 Demo 有效」——它能识别原型式函数、`self.` 状态别名、排除 `forEach` 回调误判，并按源码推断 patch 前缀；第三方源码未被修改，结果可复核。

**不能证明**：它还没有证明「能在 2048 上自动发现缺陷」。上述验证是**已知真值的静态定位**和**注入副本上的 patch 生成**，不是运行时发现率、误报率、复现率。后者需要浏览器级对照实验，方案见 [EXPERIMENT_2048_真实项目对照实验方案.md](EXPERIMENT_2048_真实项目对照实验方案.md)：10 个注入缺陷 × 三组（人工 / 盲测 / 状态桥接）× 3 轮，并单独测量「定位到源码行」的人工基线。

另外，patch 能力有明确边界：10 个实验缺陷中预注册仅 **2/10** 能给出可应用 patch（渲染偏移 D03、渲染未绑定 D04），4/10 可定位到 Top-3 但无 patch，4/10 预期定位失败（交互/节流/对象字面量等）。这组数字写在实验方案里作为失败样本，是为了让「定位能力」可被证伪，而不是把 3/3 当成通用结论。

---

## 六、与同类方案的差异化定位

| 维度 | PlayRanger | 脚本式游戏自动化（GameDriver / AltTester） | 视觉回归（Applitools 类） | AI 玩家 SaaS（modl.ai 类） | 人工外包 QA |
|---|---|---|---|---|---|
| 是否需写测试脚本 | 否 | **是**，需接 SDK + 编码 | 部分需编写 | 需接 SDK | 否 |
| 是否理解游戏状态 | 是（SDK 真值 / 结构化视觉） | 是 | **否**，只比像素 | 是 | 是 |
| 缺陷是否可审计 | **哈希链 + 字段级溯源** | 只有日志 | 只有图差 | 报告 + 录像 | 只有文字描述 |
| 是否给修复方案 | **是**（根因/步骤/验证/风险） | 否 | 否 | 否 | 否 |
| 能否定位到第三方源码行 | **是**（JS/HTML 轻量扫描，原型式/内联均可） | 否（只对自己接入的工程） | 否（只比像素） | 否 | 否（仅文字描述） |
| 是否零接入跑第三方游戏 | **是**（盲测模式） | 否 | 是（但不懂玩法） | 否 | 是 |
| 部署形态 | **本地私有化、可离线** | 本地 | SaaS 为主 | 云 SaaS | 人力 |
| 成本结构 | 一次性 + 模型调用费 | 接入人力 | 订阅 | 订阅 + 规模费 | 按人天 |

差异化主轴（详见 `ROADMAP_不足收敛与差异化.md`）：
**① 证据可审计 → ② 结论到修法（含第三方源码行） → ③ 零接入私有化（跑别人的游戏 + 定位别人的源码）**。这三条是竞品结构性缺失、且短期难以补齐的部分；性能、并行、多平台等属于工程追赶项，不作为主打。

---

## 附：本地验证方式

```powershell
# 全量回归（排除浏览器依赖）
D:\Python313\python.exe -m pytest tests --ignore=tests/test_day3_play_engine.py -q

# 新增能力专项
D:\Python313\python.exe -m pytest tests/test_day5_trace_advisor_report.py -q

# 第三方 2048 验证（16 项：完整性 / 定位 / patch / CLI）
D:\Python313\python.exe -m pytest tests/test_p3_1_thirdparty_game.py -q -p no:cacheprovider

# 第三方源码定位（输出 JSON，可加 --write 落盘 code_location.json）
D:\Python313\python.exe -m playranger.cli --no-env locate output/2048/BUG-2048-001 --source tests/fixtures/thirdparty_2048 --json

# 一键演示：生成 4 个缺陷包 + 报告 + 工单
D:\Python313\python.exe -m tools.demo_report

# 生成报告与工单（从既有缺陷目录）
D:\Python313\python.exe -m tools.build_report --bugs output/bugs --out output/report.html --tickets output/tickets

# 跨版本趋势：v1 落盘快照，v2 对比基线
D:\Python313\python.exe -m tools.build_report --bugs output/bugs --out output/report_v1.html --version v1 --versions-dir output/versions
D:\Python313\python.exe -m tools.build_report --bugs output/bugs --out output/report_v2.html --version v2 --baseline v1 --versions-dir output/versions

# 误报反馈闭环（同一异常类型连续 2 次误报后自动降级）
D:\Python313\python.exe -m playranger.cli feedback --bug BUG-001 --label false_positive --note "按钮截断属于设计预期"
```

产物：`output/demo/report.html`（离线报告）、`output/demo/tickets/*.md`（缺陷工单）。
