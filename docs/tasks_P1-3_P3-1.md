# Tasks：P1-3 回放确定性 + P3-1 代码级修复定位

> 顺序执行。每完成一项即更新状态；P1-3 全部完成后再进入 P3-1。

## P1-3 回放确定性

- [x] **T1.3.1 新增 `core/engines/settle.py`**
  - `SettleResult`（status/frames/animations/elapsed_ms/to_dict）
  - `SETTLE_SCRIPT`：2 帧 DOM 稳定 + 动画结束 + 网络空闲 + 硬超时
  - `settle(page, timeout_ms)`：永不抛异常，异常兜底 80ms
  - `build_seed_script(seed)`：生成 `window.__PLAYRANGER_SEED__`，拒绝 bool/非法类型
  - 验收：模块可导入；脚本字符串包含 `requestAnimationFrame` / `MutationObserver` / `getAnimations`

- [x] **T1.3.2 改造 `PlayEngine`**
  - `StepRecord` 增 `duration_ms`、`settle`
  - `__init__` 增 `seed` / `settle` / `settle_timeout_ms`
  - `start()`：goto 前注入 seed 脚本；SDK 就绪后尝试 `setSeed`；新增 `seed_mode`
  - `execute()`：前后 settle，记录耗时与 settle 结果
  - `sdk_set_seed()` 返回 bool
  - `trace_to_list()` 输出新字段
  - 验收：不启动浏览器即可构造 engine 并检查默认值与 `build_seed_script`

- [x] **T1.3.3 改造 `Replayer`**
  - `ReplayResult` 增 `duration_ms` / `seed_mode`
  - `ReproductionReport` 增 `seed_mode` / `reproduction_state` / `to_dict()`
  - `replay_once`：start 前设置 engine.seed；记录 seed_mode 与耗时
  - `replay_from_file` 增 `initial_state` / `seed` 透传
  - 新增 `update_defect_reproduction(bug_dir, report)`：原子写回 defect.json
  - 验收：三态边界（1.0 / 0.5 / 0.0）正确；defect.json 字段合并正确

- [x] **T1.3.4 记录 `started_at`**
  - `BugRecorder.capture(..., started_at="")` → defect.json
  - `ReActLoop` 运行开始生成 ISO 时间并传入
  - 验收：单元测试断言 defect.json 含 `started_at`

- [x] **T1.3.5 增强 CLI `replay`**
  - 读 initial_state / seed 并传入
  - 回放后 `update_defect_reproduction`
  - 打印三态、seed_mode、平均耗时；退出码 0/2/1

- [x] **T1.3.6 新增 `tests/test_p1_3_determinism.py`**
  - `build_seed_script` 合法/非法
  - `SettleResult.to_dict`
  - `StepRecord` 默认 + `trace_to_list` 含 duration_ms
  - 三态与 `to_dict`
  - `update_defect_reproduction` 写回/缺失文件
  - `capture(started_at=...)`

- [x] **T1.3.7 P1-3 验收**
  - `pytest tests/test_p1_3_determinism.py`
  - 演示游戏回放 3 次（若浏览器可用）
  - 全量回归（排除浏览器测试）
  - 实测：Edge + `PLAYWRIGHT_CHROMIUM_PATH`，3/3 成功，`state=reproduced`、`seed_mode=sdk`、`avg=6241ms`，`defect.json.reproduction` 已落盘

## P3-1 代码级修复定位

- [x] **T3.1.1 新增 `core/agent/code_locator.py` 数据结构与字段提取**
  - `CodeHit` / `CodeLocation`
  - `extract_fields`（initial/final/assertion，过滤元字段）
  - `extract_fields_from_html`（data-field）
  - 验收：元字段被过滤；assertion 字段被纳入

- [x] **T3.1.2 实现 JS/HTML/Python 扫描器**
  - `scan_js`：write/read/declaration + 函数归属 + line/column
  - `scan_html`：`<script>` 块行号换算
  - `scan_python`：`ast` + 函数/类归属
  - `locate(field, roots)`：目录遍历、跳过目录、大小/数量上限、排序去重
  - 验收：三种源码各至少 1 条命中，行列号准确

- [x] **T3.1.3 实现异常类型定位策略与 patch**
  - `hp_not_reset`：重置函数遗漏检测 + 追加赋值 patch
  - `score_mismatch`：渲染偏移检测 + 去偏移 patch
  - `ui_display_error`：渲染未引用字段 + 替换字面量 patch
  - `build_patch` unified diff
  - `locate_bug` 置信度与降级说明
  - 验收：演示游戏两个缺陷均命中正确行
  - 补充：重置值优先取断言期望值（BUG-001 → `state.hp = 100;`），否则退回 `initial_state`

- [x] **T3.1.4 接入 `advisor` / `intel`**
  - `FixPlan.code_location` + markdown 区块
  - `advise(..., source_root=None)` 注入定位步骤与验证项
  - `fix_plan_from_file(bug_dir, source_root=None)`
  - `intel` 透传 source_root
  - 验收：有源码时 fix_plan 含定位与 patch；无源码时行为不变

- [x] **T3.1.5 新增 CLI `locate`**
  - 文本/JSON 输出、`--write` 写 `code_location.json`、退出码
  - 验收：`playranger locate output/demo/bugs/BUG-001 --source examples/demo_game` 可读输出

- [x] **T3.1.6 新增 `tests/test_p3_1_code_locator.py`**
  - 临时 JS / HTML / Python 夹具
  - 演示游戏 hp/score 定位
  - 无源码降级
  - advisor 注入
  - 实测：25 项全部通过

- [x] **T3.1.7 P3-1 验收与文档同步**
  - 运行 `locate` 对 BUG-001/BUG-002
  - 更新 `docs/SUBMISSION_功能说明与亮点不足.md`（新增功能 14/15、亮点、测试数）
  - 更新 `docs/ROADMAP_不足收敛与差异化.md`（P1-3、P3-1 标记完成）
  - 全量回归
  - 实测：BUG-001 → `index.html:181` + `state.hp = 100;`；BUG-002 → `index.html:149` 去 `- 200`；全量回归 220 项通过

- [x] **T3.1.8 第二个真实第三方游戏验证（2048，MIT）**
  - 引入未修改的上游源码夹具 `tests/fixtures/thirdparty_2048`（14 个文件 + `PROVENANCE.json` SHA-256 清单）
  - 原生源码定位：`js/game_manager.js:48`（`setup`）、`:186`（`move`）、`js/html_actuator.js:110`（`updateScore`）
  - 注入缺陷副本：遗漏重置 → `+    this.score = 0;`；渲染偏移 → 去 `- 100`；渲染未绑定 → 绑定 `this.score`
  - CLI `locate --json --write` 端到端 + 无源码降级
  - 同步修复：原型式函数归属、`self.` 前缀、patch 接收者前缀推断
  - 实测：`tests/test_p3_1_thirdparty_game.py` 16 项全部通过；全量回归 236 项通过

## 完成标准

- [x] 新增测试全绿，全量回归无新增失败
- [x] 演示游戏可复现三态落盘
- [x] 演示游戏可定位到具体源码行并给出 patch
- [x] 第二个真实第三方游戏（2048）可定位到具体源码行并给出 patch
- [x] 无源码场景自动降级，且报告明确说明
