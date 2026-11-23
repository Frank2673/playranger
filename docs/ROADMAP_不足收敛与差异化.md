# PlayRanger 不足收敛路线图与差异化构建策略

> 配套文档：`SUBMISSION_功能说明与亮点不足.md`（现状）、`EXPERIMENT_真实项目对照实验方案.md`（验证）
> 目标：把已列出的 14 项不足按「差异化价值 ÷ 实现成本」排序收敛，而不是平均用力。

---

## 一、先做判断：哪些不足值得修，哪些不该现在修

不是所有不足都该补。补错方向会把项目做成「功能更全但没人记得住」的通用测试平台。

| 不足 | 是否影响差异化 | 是否影响可用性 | 结论 |
|---|---|---|---|
| VLM 误报 / 成本 | 中 | **高** | 用「反馈闭环 + 阈值校准」解，不换模型 |
| Canvas/WebGL 盲测精度 | 中 | 中 | 用「状态桥接约定」缓解，不做通用视觉理解 |
| 记忆非语义向量 | 中 | 低 | 第二阶段做，保留签名兜底 |
| 修复非代码级 | **高（核心差异化）** | 中 | 第三阶段做，优先级最高的护城河 |
| 回放复现率 | 中 | **高** | 第一阶段做，成本可控 |
| 仅 Web 游戏 | 低（当前用户群匹配） | 中 | 靠协议桥接，不自研引擎插件 |
| 单机串行 | 低 | 中 | 第三阶段做 |
| 无趋势对比 | **高（付费感知强）** | 中 | 第一阶段做，成本最低 |
| 无 CI 插件 | 中 | **高（接入门槛）** | 第二阶段做 |
| 无鉴权/多租户 | 低 | 中 | 只做 API Key + 项目隔离，不做 SaaS |
| 阈值不可配 | 中 | **高** | 第二/三阶段做配置化 |
| 专项测试缺失 | 中 | 中 | 只做性能采样，其余按需 |
| CI 环境不稳定 | 低 | **高** | 第一阶段做稳定性加固 |
| 缺人工对照基线 | **高（说服力）** | **高** | 立即执行实验，不占开发工时 |

---

## 二、差异化主轴：三条竞品结构性缺失

竞品能补的（性能、并行、多平台、云规模）不要当卖点；竞品补不了的才是壁垒。

### 主轴 ① 证据可审计（Evidence You Can Audit）

- 现状：哈希链 + 字段级状态差异 + 检测通道 + 触发步。
- 竞品状态：GameDriver/AltTester 给日志，Applitools 给图差，modl.ai 给录像，人工给文字。
- 深化方向：**证据包标准化**（可导出为通用格式，供第三方复验）+ **证据强度分级**（单通道 / 双通道一致 / 三通道一致）。

### 主轴 ② 结论到修法（From Bug to Fix）

- 现状：19 类规则 + 状态差异特化 + 验证清单 + 置信度。
- 竞品状态：没有一家做到「缺陷 → 根因 → 修法 → 验证」。
- 深化方向：**从「规则建议」升级为「代码定位建议」**——用 AST 把状态字段名映射到赋值点，给出 `文件:行` 与 patch 草案。这是从「助手」变成「队友」的分界线。

### 主轴 ③ 零接入私有化（Zero-Onboarding & Private）

- 现状：盲测零接入；单文件 HTML 报告；离线可跑。
- 竞品状态：SDK 接入是硬门槛；SaaS 要上传素材。
- 深化方向：**「5 分钟出第一份报告」**体验指标 + 端到端断网可运行证明。

---

## 三、竞品结构对照（决定我们不打哪一仗）

| 竞品类型 | 强在哪 | 弱在哪 | 我们的策略 |
|---|---|---|---|
| GameDriver / AltTester（脚本式引擎自动化） | 引擎内真值、稳定、可进 CI | 必须写测试代码、必须接 SDK、无缺陷分析 | 不打「稳定性」正面战，打「零脚本 + 分析链条」 |
| Applitools / Percy（视觉回归） | 视觉 diff 成熟、规模化 | 不理解玩法、不判语义对错 | 不打「像素级对比」，把视觉当一条证据通道 |
| modl.ai（AI 玩家 SaaS） | 规模化 AI 玩家、数据平台 | 云依赖、接入成本、无修复建议 | 打「私有化 + 修复建议 + 独游价格带」 |
| 人工外包 QA | 懂玩法、能主观判断 | 贵、慢、不可复现、结论不可审计 | 打「可复现 + 可审计 + 按次成本」，定位为「外包的前置筛子」 |

> 结论：不打「覆盖广」，打「每条缺陷更深」。宁可少报 20% 的缺陷，也要让报出来的每条都能直接派活。

---

## 四、分阶段构建计划

### P0 — 已完成基线（不计入新排期）

双引擎试玩、ReAct 三层自修正、动作校验、状态签名记忆、5 档分级 + 3 档结论、SHA-256 证据链、一键回放、Bug 溯源、修复方案、离线报告、CLI/API/SDK。
验收基线：`test_day5_trace_advisor_report.py` 52 项 + 全量回归 302 项通过（含浏览器级用例）。

---

### P1 — 差异化加固（建议第 1 阶段，成本低、感知强）

目标：把「已有能力」变成「用户看得见的差异」，并解决最影响信任的三项不足（复现率、误报、环境稳定性）。

#### P1-1 跨版本趋势对比（解决不足 8）✅ 已完成

- **做什么**
  1. 每次跑测后落盘 `output/versions/<version>/summary.json`（复用 `report.build_metrics` 输出）；
  2. `report.py` 新增 `render_trend(current, history)` 区块：缺陷总数收敛曲线、按严重度堆叠、新增/已修复/遗留三分类（按 `bug_id` + `anomaly_type` 比对）；
  3. CLI `--baseline <version>` 参数指定对比基线；无基线时区块自动隐藏。
- **验收**：连续跑两次 demo（第二次注入 1 个新缺陷、移除 1 个），报告出现「新增 1 / 修复 1 / 遗留 3」且曲线正确。
- **成本**：约 1 人日。改动文件：`core/agent/report.py`、`tools/build_report.py`。
- **依赖**：无（`history` 参数已预留）。
- **落地**：新增 `core/agent/trend.py`（快照 / 差分 / 指纹匹配）；`build_report.py` 增加 `--version`、`--baseline`、`--versions-dir`；报告新增趋势面板与收敛曲线。实测「v1 4 条 → v2 移除 1 条 + 改标题 1 条」输出「新增 1 / 修复 2 / 遗留 2」。

#### P1-2 误报反馈闭环（解决不足 1、11）✅ 已完成

- **做什么**
  1. 新增 `feedback` 能力：`playranger feedback --bug BUG-001 --label false_positive|true_positive --severity minor --note "..."`，落盘 `bug_dir/feedback.json`；
  2. `triage()` 读取项目级 `triage_overrides.json`（按 `anomaly_type` 的阈值/权重覆盖），命中 FP 记录的异常模式在后续批次降权（`auto_confirmed → auto_warning`）；
  3. 报告 KPI 卡新增「人工复核一致率」。
- **验收**：对同一 `anomaly_type` 连标 2 次 FP 后，下一次同类缺陷结论自动降级；报告一致率随之变化。
- **成本**：约 1.5 人日。改动：`core/agent/triage.py`、`core/agent/intel.py`、`api/main.py`（`POST /api/v1/bugs/{id}/feedback`）。
- **依赖**：P1-1 的版本目录（共用落盘结构）。
- **落地**：新增 `core/agent/feedback.py`；`triage()` 增加 `override` 参数（显式严重度/结论覆盖 + `fp_count≥2` 自动降级）；`BugRecorder.capture` 自动读取覆盖规则；CLI `feedback` 子命令与 API `POST /api/v1/bugs/{id}/feedback` 已联通；报告新增复核一致率 KPI。

#### P1-3 回放确定性增强（解决不足 5）✅ 已完成

- **做什么**
  1. 动作前统一等待策略：网络空闲 + 连续两帧 DOM 稳定 + 动画结束（`requestAnimationFrame` 计数），替代固定 `sleep`；
  2. 缺陷包记录 `seed`、`started_at`、每步耗时；回放时通过 `window.__PLAYRANGER_SEED__` 注入（游戏支持时生效，不支持则标注）；
  3. 回放结果三态：`reproduced / partial / not_reproduced`，并写入 `defect.json` 的 `reproduction` 字段（已有该字段）。
- **验收**：演示游戏连续回放 3 次，结果三态一致；注入随机数后可复现。
- **成本**：约 1.5 人日。改动：`core/engines/play_engine.py`、`core/agent/replayer.py`。
- **依赖**：无。
- **落地**：新增 `core/engines/settle.py`；`PlayEngine` 增 `seed/settle/settle_timeout_ms` 与 `seed_mode`，`StepRecord` 增 `duration_ms/settle`；`Replayer` 增三态、`update_defect_reproduction` 原子写回；CLI `replay` 输出三态/种子模式/平均耗时，退出码 0/2/1；`capture(started_at=...)` 落盘。
- **实测**：Edge + `PLAYWRIGHT_CHROMIUM_PATH` 回放演示游戏 BUG-001，3/3 成功，`state=reproduced`、`seed_mode=sdk`、平均 6241ms，`defect.json.reproduction` 已落盘。

#### P1-4 CI 环境稳定性加固（解决不足 13）✅ 已完成

- **做什么**：启动参数固化（禁用 GPU、固定视口、`--no-sandbox`）、失败自动重试 1 次并记录 `retry_reason`、超时分级（导航/动作/整体）、`--headless` 默认开启。
- **验收**：无头模式下连续 10 次跑 demo，成功率 ≥ 9/10。
- **成本**：约 1 人日。改动：`core/engines/play_engine.py`。
- **依赖**：无。
- **落地**：`PlayEngine` 新增 `ci_mode / retry / viewport / navigation_timeout_ms / action_timeout_ms`；`_CI_LAUNCH_ARGS` 固化 8 项参数；`start()` 失败自动重试并记录 `retry_reasons`；`runtime_stats()` 输出环境与重试信息；CLI `test --ci --retry N`。
- **验收状态（诚实标注）**：单次无头回归已通过；「连续 10 次成功率」尚未按实验方案采数，标记 **pending**，不编造 9/10。

#### P1-5 性能专项采样（解决不足 12 的一部分）✅ 已完成

- **做什么**：页面注入 `PerformanceObserver`，采集 FPS、长任务、内存峰值；超阈值自动生成 `performance` 类缺陷（`advisor._RULES` 已有该类型），证据附采样序列。
- **验收**：构造一个卡顿页面，能产出 `performance / major` 缺陷且带 FPS 曲线数据。
- **成本**：约 1 人日。改动：`core/engines/play_engine.py`（采集）、`core/agent/provenance.py`（证据通道）。
- **依赖**：无。
- **落地**：新增 `core/engines/perf.py`（rAF 帧间隔 + longtask + `performance.memory`）；`PlayEngine(perf=True)` 自动注入；`perf_sample()` 返回 `avg_fps / min_fps / p95_frame_ms / long_task_count / max_long_task_ms / heap_peak_mb / violations`；`ReActLoop` 跑完按阈值自动记 `performance` 缺陷（通道 `perf_sample`）；CLI `test --perf`。
- **验收状态（诚实标注）**：单元测试覆盖低 FPS、长任务、样本不足三态；真实卡顿页面的 `performance / major` 端到端尚未采数。阈值面向 Web 小游戏，**不是** 3A 标准。另实测：无头模式（Edge headless）下 rAF 被节流，4 步冒烟仅采集到 2 帧，`perf_sample()` 正确返回 `insufficient_data` 而不是假 FPS；卡顿判定目前需要 headful/可见页面或后续接入 CDP 指标兜底。

> **P1 合计约 6 人日**。完成后对外可讲的三句话：能看版本收敛、能纠错、能复现。

---

### P2 — 工程化接入（商业化门槛）

目标：让工具能进真实研发流程，而不只是「本地跑一次看看」。

#### P2-1 CI/CD 插件（解决不足 9）✅ 已完成（PR 评论除外）

- **做什么**
  1. GitHub Action（`action.yml` + Dockerfile）：入参 `game-url / goal / max-steps / fail-on`；
  2. Jenkins Shared Library（`vars/playranger.groovy`）；
  3. 门禁规则：`blocker/critical` 数量 > 阈值 → 退出码 1；输出 JUnit XML（进测试报告）与 SARIF（进代码扫描面板）；
  4. PR 评论摘要（缺陷数、最高严重度、报告链接）。
- **验收**：在示例仓库开 PR，Action 自动跑测并因 critical 缺陷标红。
- **成本**：约 3 人日。新增 `.github/actions/playranger/`、`ci/jenkins/`。
- **依赖**：P1-1（趋势）、P1-4（稳定性）。
- **落地**：新增 `tools/ci_report.py`（JUnit / SARIF 2.1.0 / Markdown 摘要 / `--fail-on` + `--allow` 门禁）；`.github/actions/playranger/action.yml`（composite，含 Playwright 安装与产物上传）；`ci/jenkins/playranger.groovy`（Stage + junit + archiveArtifacts）。
- **实现偏离（诚实标注）**：PR 评论摘要**未实现**；GitHub Action 的 Dockerfile 形态改为 composite action（Python 项目无需容器化）。真实仓库 PR 验证尚未执行。

#### P2-2 鉴权与项目隔离（解决不足 10 的一部分）✅ 已完成

- **做什么**：`X-API-Key` 鉴权（密钥来自环境变量）、`project_id` 隔离目录与查询、报告按项目过滤。**明确不做**多租户计费与团队权限体系。
- **验收**：无 key 返回 401；两个 project 的缺陷列表互不可见。
- **成本**：约 1.5 人日。改动：`api/main.py`。
- **依赖**：无。
- **落地**：设置 `PLAYRANGER_API_KEY` 后 `/api/v1/*` 强制校验 `X-API-Key`（`/health` 放行）；`project_id` 经字符白名单清洗（防路径穿越），非 default 项目缺陷写入 `output/projects/<id>/`；`GET /api/v1/bugs?project_id=` 隔离查询；测试覆盖 401、两项目互不可见、路径穿越。

#### P2-3 分级配置化（解决不足 11）✅ 已完成

- **做什么**：`playranger.triage.yaml` 支持按 `anomaly_type` 配置基础分、权重、结论门槛、忽略规则；加载优先级：项目配置 > 内置规则。
- **验收**：把 `text_overflow` 基础分调低后，同一缺陷分级从 `major` 变为 `minor`。
- **成本**：约 1 人日。改动：`core/agent/triage.py`。
- **依赖**：P1-2（共用覆盖机制）。
- **落地**：新增 `core/agent/triage_config.py`；配置文件支持 `base_severity / evidence_weights / thresholds / ignore`；查找顺序 `PLAYRANGER_TRIAGE_CONFIG` → `playranger.triage.json` → `.yaml`；`BugRecorder` 自动加载并传入 `run_triage(config=...)`。
- **实现偏离（诚实标注）**：默认推荐 **JSON**（零依赖）；YAML 仅在环境已安装 PyYAML 时可用，缺失时明确报错而不是静默忽略。

> **P2 合计约 5.5 人日**。完成后可对外讲：能进流水线、能按项目隔离、阈值可按项目调。

---

### P3 — 技术护城河（决定长期壁垒）

目标：把「AI 助手」升级为「能读代码的队友」。这一段成本最高，但只有这一段能形成竞品短期抄不走的能力。

#### P3-1 代码级修复定位（解决不足 4）★ 最高优先 ✅ 已完成

- **做什么**
  1. 建立「状态字段 → 源码符号」映射：从 SDK 状态 schema 或 DOM 数据属性提取字段名（`hp`、`score`、`scene`）；
  2. AST 解析（Python `ast` / JS `@babel/parser` / TS 编译 API），检索字段读写点，输出 `文件:行:列`；
  3. 结合异常类型给出定位策略：`hp_not_reset` → 找场景切换/重置函数中的赋值缺失；`score_mismatch` → 找计分累加与展示两处的一致性；`ui_display_error` → 找渲染绑定表达式；
  4. 输出 patch 草案（unified diff 片段）+ 置信度降级说明（定位不到时明确说「未在源码中定位到」）。
- **验收**：在 2 个开源 Web 游戏上，`hp_not_reset` 与 `score_mismatch` 两类缺陷能定位到正确文件与函数（人工判定 ≥ 70%）。
- **成本**：约 6 人日。新增 `core/agent/code_locator.py`；`advisor.py` 接入。
- **依赖**：P1-3（复现稳定后定位才有意义）。
- **风险**：需要用户提供源码路径；闭源游戏自动降级为现有规则模板。
- **落地**：新增 `core/agent/code_locator.py`（`CodeHit/CodeLocation/extract_fields/scan_js/scan_html/scan_python/locate/build_patch/locate_bug`）；`FixPlan.code_location` + markdown「代码定位」区块；`advise/fix_plan_from_file/analyze_bug/save_intel` 透传 `source_root`；CLI `locate --source --write --json`。
- **实测**：演示游戏 BUG-001 → 命中 `index.html:181`（`level2-restart`），patch 追加 `state.hp = 100;`；BUG-002 → 命中 `index.html:149`，patch 去掉 `- 200`；两者置信度 90%、退出码 0。
- **第二个真实第三方游戏验证（已补）**：夹具 `tests/fixtures/thirdparty_2048` 引入 2048（MIT，上游 commit `478b6e…`，SHA-256 校验保证源码未改）。16 项测试覆盖：原生源码定位（`setup` / `move` / `updateScore`）、`self.` 前缀识别、注入副本上的三条 patch 策略（补 `this.score = 0;` / 去 `this.score - 100` / 绑定 `this.score`）、CLI `locate --json --write` 端到端、无源码降级。验证中发现并修复三处第三方代码适配问题：原型式函数归属、`self.` 状态前缀、patch 硬编码 `state.` 前缀；全量回归 **236 项通过**。
- **实现偏离**：JS 侧未引入 `@babel/parser`，改为零依赖轻量扫描（正则 + 函数归属回溯 + 注释屏蔽），以保持「离线、零依赖」约束。

#### P3-2 语义记忆（解决不足 3）✅ 已完成（近似语义，非神经向量）

- **做什么**：在状态签名之外增加语义层——本地 TF-IDF / 可选 `sentence-transformers` embedding；签名相似度作为兜底；引入容量上限与时间衰减（LRU + 半衰期）。
- **验收**：跨 2 个同类型游戏（如两款回合制）复用动作经验，第二步探索步数下降 ≥ 20%。
- **成本**：约 3 人日。改动：`core/agent/memory.py`。
- **依赖**：无。
- **落地**：`memory.py` 新增中英混合分词（英文词 + 中文字符/二元组）、TF-IDF 余弦、`recall(semantic=True)` 混合打分（签名兜底 + 语义越阈）；`capacity=500` 按「命中 × 时间衰减」淘汰；`half_life_days=30`；`stats()` 暴露 `semantic_mode=tfidf_ngram_approximation`。
- **实现偏离（诚实标注）**：**未**引入 `sentence-transformers`，零依赖近似语义不等于神经 embedding；「跨游戏步数下降 ≥20%」尚未实测，标记 **pending**。

#### P3-3 并行调度（解决不足 7）✅ 已完成（进程内线程池）

- **做什么**：`asyncio` 任务池，按 test 分片并发（默认 2 worker，可配）；每 worker 独立浏览器上下文与缺陷目录；汇总时按 `bug_id` 去重。
- **验收**：3 个测试并发跑，总耗时 < 串行 60%，缺陷数一致。
- **成本**：约 3 人日。改动：`api/main.py`、`core/agent/react_loop.py`（隔离状态）。
- **依赖**：P2-2（项目隔离）。
- **落地**：新增 `core/agent/batch.py`（`load_manifest / run_batch`，ThreadPoolExecutor，worker 上限 8，单任务异常隔离，缺陷按「类型 + 标题」去重）；CLI `playranger batch <manifest> --workers N --json`；每任务独立输出目录。
- **实现偏离（诚实标注）**：未使用 `asyncio`，改为线程池（Playwright sync API 与线程池更匹配）；**合成任务实测** 4 个各 0.3s 的任务，串行 1210ms → 4 worker 304ms（3.98×）；真实浏览器并发未做性能基线。

#### P3-4 多平台适配（解决不足 6）⚠️ 部分完成（协议 + Web 实现，Unity 仅占位）

- **做什么**：定义 `EngineAdapter` 协议（`get_state / get_actions / execute`），实现 Web 适配器（现有）+ AltTester 协议桥接（Unity）+ 原生壳 WebView 适配；**不自研引擎插件**。
- **验收**：用 AltTester 示例工程跑通一次「感知-动作-断言」闭环。
- **成本**：约 4 人日。新增 `core/engines/adapters/`。
- **依赖**：P3-1（建议先完成代码定位，否则多平台只是搬运问题）。
- **落地**：新增 `core/engines/adapters/`（`EngineAdapter` Protocol、`WebAdapter`、`AltTesterAdapter` 诚实 stub、`get_adapter` 注册表）；原生手游明确抛 `AdapterUnavailable`，不静默降级。
- **未完成（诚实标注）**：AltTester 闭环**未验证**（缺 Unity 工程与 AltServer），`capabilities().available=False` 并写明原因；原生壳 WebView 适配未实现。

> **P3 合计约 16 人日**。建议按 P3-1 → P3-2 → P3-3 → P3-4 顺序，P3-1 单独成里程碑。

---

### P4 — 验证闭环（与开发并行，不占排期）⚠️ 部分执行

- 执行 `EXPERIMENT_真实项目对照实验方案.md`：3 个游戏 × 3 组 × 3 轮，产出漏报率、误报率、单缺陷定位耗时、节省工时、API 成本。
- 产出物：实验报告 + 原始数据表 + 仲裁记录。
- 这是把「我们的缺陷是 AI 找的」变成「我们的缺陷发现效率是人工的 N 倍」的唯一路径。
- **已执行（2026-11-23）**：2048 D 组静态定位 10/10，Top-3 4/10、patch 可用 2/10、可操作定位 6/10，与预注册一致；Top-1 0/10（setup 写点抢占），中位 30ms。执行器 `tools/run_2048_experiment.py`，产物 `output/experiments/2048/`。
- **已执行（夹具级）**：2048 浏览器确定性回放 2/2（同种子状态一致），D03 渲染不一致可被「探针 vs DOM」检出；`tests/test_2048_browser_determinism.py`。
- **未执行**：A 人工 / B 盲测 / C 状态桥接 / E 人工定位；发现率、误报率、节省工时仍无数据，不得给出结论。

---

## 五、平台化（参赛版本，2026-11）

### P5-1 多用户控制台与调度 —— 已完成

- SQLite：users / sessions / tasks / events；首用户 admin；PBKDF2 密码 + Token 摘要存储；
- 内存公式：`floor((available_gb - 2.0) * 1024 / 1536)`，clamp 到 `1..min(8, cpu_count)`；
- 队列 + Worker 池 + 目标并发扩缩 + 暂停/恢复/停止/重跑；条件更新避免重复领取；
- 真实 Runner：PlayEngine + ReActLoop 步进回调 → `steps.jsonl` / `progress.json` / 缺陷包；
- 控制台 `/app/`：登录注册、内存仪表、并发滑块、任务看板、步骤时间线、Bug 审查、事件流；
- 修复沙箱：`git apply --check`（非 git 临时仓库等价校验）+ 副本应用 + 可选测试命令；
- CDP 兜底：无头 rAF 不足时用 Performance 指标，标记 `fps_unavailable`，不伪造 FPS。

验收：平台新增 25 项测试通过；真实 demo_game 8 步 E2E 通过；全量 327 项回归通过。

仍未完成：A/B/C/E 对照实验（C 组仅 2 个夹具用例）、修复后自动回归、多机调度、Unity/手游真机。

## 六、优先级矩阵（更新）

| 项 | 差异化贡献 | 实现成本 | 优先级 |
|---|---|---|---|
| 跨版本趋势对比 | 高 | 低 | **P1 首位** |
| 误报反馈闭环 | 高 | 低 | **P1** |
| 回放确定性 | 中 | 低 | **P1** |
| CI 稳定性 | 低 | 低 | **P1** |
| 性能采样 | 中 | 低 | **P1** |
| CI 插件 | 中 | 中 | P2 |
| 鉴权/隔离 | 低 | 中 | P2 |
| 分级配置化 | 中 | 低 | P2 |
| **代码级修复定位** | **极高** | **高** | **P3 首位** |
| 语义记忆 | 中 | 中 | P3 |
| 并行调度 | 低 | 中 | P3 |
| 多平台适配 | 中 | 高 | P3 末位 |

---

## 六、明确不做什么（防范围膨胀）

1. **不做通用 Web 测试平台**——只管游戏，不抢 Testim/Mabl 的市场。
2. **不做像素级视觉回归**——不与 Applitools 正面竞争，视觉只作为一条证据通道。
3. **不自研 VLM / 不训练模型**——模型只做决策与描述，事实判定交给规则。
4. **不做云 SaaS 多租户与计费**——只做私有化部署 + 单租户多项目。
5. **不做引擎内插件（Unity/Unreal 原生 SDK）**——通过 AltTester/GameDriver 协议桥接。
6. **不追求缺陷「数量覆盖」**——宁可少报，不少报错报；误报率是核心 KPI。

---

## 七、里程碑与比赛交付对齐

| 里程碑 | 内容 | 可对外展示的成果 |
|---|---|---|
| M0（已完成） | P0 基线全部能力 | 52 项专项测试 + 演示报告 |
| M1（P1 全部完成） | 趋势对比 / 反馈闭环 / 回放确定性 / CI 稳定性 / 性能采样 | 两次版本对比报告 + 误报降级演示 + 3 次一致回放 + `--ci --perf` |
| M2（P2 全部完成） | CI 插件 / 鉴权 / 配置化 | JUnit+SARIF 门禁产物 + 双项目隔离测试 + `playranger.triage.json` |
| M3（P3-1 完成；P3-2/3 完成；P3-4 部分） | 代码级修复定位 / 语义记忆 / 并行调度 / 适配协议 | 「缺陷 → 文件:行 → patch 草案」+ batch 3.98× 合成加速 + AltTester 占位说明 |
| M4（P4 待执行） | 对照实验 | 量化效率报告（漏报率/误报率/节省工时） |

> 若时间只够做一件事：**先做 P1-1 + P1-2**（成本 2.5 人日，直接解决「报告没对比」和「误报没人管」两个最容易被评委追问的点）；若只做一件技术攻坚：**P3-1 代码级修复定位**，这是本项目唯一竞品短期无法复制的能力。

> 诚实边界：M1–M3 的「完成」指代码与单元/集成测试通过；凡涉及真实项目采数的验收（10 次成功率、跨游戏步数下降、AltTester 闭环、PR 标红），均标注 pending，未用演示数据替代。

---

## 八、P6 多层次差异化深化（赛后 / 决赛冲刺，2026-11 起，按难易排序）

P0–P5 已把「能跑、能看、能证」补齐。下一阶段不再堆功能，而是围绕**三大自我攻诘**（没真实数据 / 目标市场模糊 / LLM 波动）和三条差异化主轴，做竞品短期抄不走的纵深。按「价值 ÷ 成本」从易到难：

### 第 1 层 · 把数据补到无可反驳（最易，先做）

| 序 | 事项 | 攻诘对应 | 形态 | 成本 |
|---|---|---|---|---|
| 6.1 ✅ | **B1 DOM-only 消融实验**：C 组 5 缺陷，证据通道从「状态探针+DOM」换成「纯 DOM + 期望值断言」，量化零接入捕获边界 | #1 没真实数据 | `tools/run_2048_bgroup_dom.py` | 0.3 人日 |
| 6.2 | **B2 真盲测发现率**（无期望值，纯 DOM+截图，LLM/VLM 自主探索）× 3 轮，报发现率/误报率/平均步数/Token 成本 | #1、#3 | 需 LLM Key，多轮采数 | 2 人日 |
| 6.3 | **A 人工基线 + E 人工定位基线**：同一批缺陷让人盲玩、人工定位，计时，给出「Agent vs 人」节省工时倍数 | #1 | 实验脚本 + 仲裁表 | 1 人日 |
| 6.4 | **第二个真实游戏**（非自制 demo、非 2048）复跑 B1/B2，证明不是「为 2048 调参」 | #1 | 新增 fixture | 1.5 人日 |

> 这一层只产数据和报告，不占核心开发，但对说服力是质变：从「demo」变「可复现的测量」。

### 第 2 层 · 把 LLM 波动关进笼子（中，直击测试可复现本质）

| 序 | 事项 | 做法 | 成本 |
|---|---|---|---|
| 6.5 | **轨迹确定性 diff** | 同一目标跑 N 次，逐步比对 action/state，输出动作一致率、首个分叉步、状态一致率（`tools/trace_diff.py`） | 1 人日 |
| 6.6 | **确定性档位** | `temperature=0` + seed 透传 + Top-1 动作硬校验 + 失败动作不进记忆；MockBrain/规则策略作为「确定性回归基线」 | 1 人日 |
| 6.7 | **智能分级回归**：高确定性缺陷（断言/状态）走规则确定性通道，开放探索才走 LLM，报告里分开标注「确定性检出 / AI 检出」 | 架构 | 2 人日 |

> 对外话术从「AI 找 Bug」升级为「**事实判定确定性 + 探索智能可替换**」——这是测试行业愿意买单的表述。

### 第 3 层 · 把「结论到修法」做深（中高，最高护城河）

| 序 | 事项 | 做法 | 成本 |
|---|---|---|---|
| 6.8 | **修复后自动回归闭环** | patch 沙箱应用 → 自动重放触发序列 → 缺陷消失且无新增 → 标 `verified_fixed`；未消失回退 | 2 人日 |
| 6.9 | **跨文件/跨过程定位** | 现在只定位单文件写点；加「字段数据流图」（赋值点→渲染点→重置点），覆盖跨文件状态缺陷 | 4 人日 |
| 6.10 | **定位置信度校准** | 用 A 组人工定位结果做混淆矩阵，把 Top-3 命中率校准成可对外承诺的区间 | 1 人日 |

### 第 4 层 · 把市场从「夹在中间」救出来（中，解决 #2 目标市场模糊）

| 序 | 事项 | 做法 |
|---|---|---|
| 6.11 | **收窄 ICP**：不做泛「独游开发者」，聚焦「**Web 小游戏 / 微信小游戏 / itch.io HTML5**」单人开发者——他们恰好只有 Web、无 QA、无预算、迭代快，和我们能力边界完全重合 | 定位重写 |
| 6.12 | **5 分钟首报告体验**：官网放一个「输入 URL 在线试跑」免登 demo（本地容器，跑完即焚），把 onboarding 变成一个动作 | 工程 3 人日 |
| 6.13 | **缺陷工单一键导出**（GitHub Issue / Linear / 飞书卡片），把证据包贴进他们已有的协作工具，降低「看完然后呢」 | 1.5 人日 |

### 第 5 层 · 平台纵深（高，决赛/商业化阶段）

| 序 | 事项 | 做法 | 成本 |
|---|---|---|---|
| 6.14 | **定时回归 + PR 门禁真实闭环** | 现在 CI 产物就绪但未在真实 PR 跑通；补一个样例仓库的绿/红 PR 录像 | 2 人日 |
| 6.15 | **Unity AltTester 真实闭环** | 把 P3-4 的 stub 用真实 Unity 示例工程跑通「感知-动作-断言」 | 5 人日 |
| 6.16 | **多机分布式 worker** | 现在单机线程池；任务序列化 + 远端 worker（SSH/容器），面向小团队 CI 机房 | 5 人日 |

### 建议执行顺序

1. **立刻**：6.1（DOM 消融，0.3 人日，本轮已做）→ 6.2（真盲测，最关键的数据缺口）→ 6.3（人工基线）。
2. **决赛前**：6.5/6.6（可复现）+ 6.8（自动回归闭环）+ 6.11（收窄 ICP）。这五项让叙事从「AI demo」变成「**可测量、可复现、能闭环修、知道卖给谁**」。
3. **明确继续不做**：通用 Web 测试、像素视觉、自研模型、SaaS 多租户、3A 性能标准（同第七节）。
