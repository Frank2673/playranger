# Spec：P1-3 回放确定性 + P3-1 代码级修复定位

> 目标：把「回放靠固定 sleep + 随机种子缺失 + 复现只有二态」升级为「稳定等待 + 种子可注入 + 三态复现落盘」，再把「规则模板修复建议」升级为「AST/静态扫描定位到 文件:行:列 + patch 草案 + 置信度降级说明」。
>
> 约束：不改动现有缺陷包结构中的既有字段语义；新增字段一律向后兼容；不引入新第三方依赖（JS 用轻量扫描器，Python 用标准库 `ast`）。

---

## 一、P1-3 回放确定性增强

### 1.1 问题与目标

现状：
- `PlayEngine.execute()` 前后直接 `read_state()`，动作后没有等待页面稳定；
- `wait` 动作使用固定毫秒；
- `Replayer.replay_from_file()` 不传 `initial_state` / `seed`；
- `sdk_set_seed()` 对不支持的游戏静默失败，无法判断种子是否生效；
- `ReproductionReport` 只有 `is_reproducible`（≥80%）二态；
- 缺陷包没有记录测试开始时间与每步耗时。

目标：
1. **统一等待策略**：动作前后等待「网络空闲 + 连续 2 帧 DOM 稳定 + 动画结束」，替代固定 sleep；超时可降级，不阻塞主流程。
2. **确定性记录**：缺陷包记录 `seed`、`started_at`、每步 `duration_ms`；回放时通过 `window.__PLAYRANGER_SEED__` 注入，SDK 支持时再调用 `setSeed`，不支持则明确标注。
3. **三态复现**：`reproduced / partial / not_reproduced`，并写入 `defect.json` 的 `reproduction` 字段。

### 1.2 新增模块 `playranger/core/engines/settle.py`

```python
@dataclass
class SettleResult:
    status: str          # stable / timeout / unsupported / error
    frames: int          # 连续稳定帧数（目标 2）
    animations: int      # 结束时仍在运行的动画数
    elapsed_ms: int

    def to_dict(self) -> dict
```

- `SETTLE_SCRIPT`：注入页面的 JS Promise，规则：
  - `MutationObserver` 监听 `documentElement`（subtree/childList/attributes/characterData）；
  - 每次 `requestAnimationFrame` 检查：无 mutation 且 `document.getAnimations()` 中无 `running` → `frames += 1`，否则清零；
  - `frames >= 2` 或硬超时（`timeout_ms`）即 resolve，返回 `{status, frames, animations, elapsed_ms}`。
- `settle(page, timeout_ms=1500) -> SettleResult`：
  - 先 `page.wait_for_load_state("networkidle", timeout=min(300, timeout_ms))`，异常忽略；
  - 再 `page.evaluate(SETTLE_SCRIPT, timeout_ms)`；
  - 任何异常返回 `SettleResult("error", 0, 0, 0)` 并 `page.wait_for_timeout(80)` 兜底；
  - 永不抛异常。
- `build_seed_script(seed: int) -> str`：返回 `window.__PLAYRANGER_SEED__ = <int>;`；`bool` 与非法类型抛 `ValueError`。

### 1.3 修改 `playranger/core/engines/play_engine.py`

- `StepRecord` 新增：`duration_ms: float = 0.0`、`settle: dict = field(default_factory=dict)`。
- `PlayEngine.__init__` 新增参数：
  - `seed: int | None = None`
  - `settle: bool = True`
  - `settle_timeout_ms: int = 1500`
- `start()`：
  - `goto` 之前若 `seed is not None`：`page.add_init_script(build_seed_script(seed))`；
  - `_wait_for_sdk()` 之后调用 `self.seed_supported = self.sdk_set_seed(seed)`（吞异常，返回 bool）；
  - 新增属性 `seed_mode`：`"sdk"`（setSeed 生效）/ `"global"`（仅注入全局变量）/ `"none"`（无种子）。
- `execute()`：
  - `t0 = time.perf_counter()`；
  - `_do_execute` 前调用 `self._settle()`；执行后再次 `self._settle()`；
  - 写入 `record.duration_ms` 与 `record.settle`。
- `_settle()`：`self.settle` 为 False 时直接返回；否则调用 `settle(self._page, self.settle_timeout_ms)` 并存到 `self._last_settle`。
- `sdk_set_seed()`：改为返回 `bool`（SDK 无 `setSeed` 返回 False，不抛异常）。
- `trace_to_list()`：输出新增 `duration_ms`、`settle`。

### 1.4 修改 `playranger/core/agent/replayer.py`

- `ReplayResult` 新增：`duration_ms: float = 0.0`、`seed_mode: str = ""`。
- `ReproductionReport` 新增：
  - `seed_mode: str = ""`
  - `property reproduction_state -> str`：
    - `rate >= 0.8` → `"reproduced"`
    - `0 < rate < 0.8` → `"partial"`
    - `rate == 0` → `"not_reproduced"`
  - `to_dict()`：包含 `bug_id/total_runs/success_runs/reproduction_rate/state/seed_mode/avg_duration_ms`。
- `replay_once()`：
  - 创建 engine 后、`start()` 前设置 `engine.seed = seed`；
  - `start()` 后记录 `seed_mode = engine.seed_mode`；
  - 统计 `t0/t1` 写入 `duration_ms`。
- `replay_multiple()`：把首个 run 的 `seed_mode` 写入报告。
- `replay_from_file()` 新增 `initial_state: dict | None = None`、`seed: int | None = None` 并透传。
- 新增 `update_defect_reproduction(bug_dir, report) -> dict`：
  - 读 `defect.json`（缺失返回 `{}`）；
  - 更新 `reproduction_rate`、`reproduction_attempts`；
  - 写入 `reproduction = {state, rate, runs, success_runs, seed_mode, avg_duration_ms, updated_at}`；
  - 先写 `.tmp` 再 `replace`，返回该 `reproduction` 对象。

### 1.5 修改 `playranger/core/agent/recorder.py` 与 `react_loop.py`

- `BugRecorder.capture()` 新增 `started_at: str = ""`；写入 `defect.json` 顶层 `started_at`。
- `ReActLoop` 在运行开始时生成 ISO 时间，调用 `capture(started_at=run_started_at)`。

### 1.6 修改 `playranger/cli.py` 的 `cmd_replay`

- 读取 `defect.get("initial_state")` 与 `seed.json`，传给 `replay_from_file`。
- 报告后调用 `update_defect_reproduction(bug_dir, report)`。
- 输出：三态中文（完全复现 / 部分复现 / 未复现）、`seed_mode`、平均耗时。
- 退出码：`reproduced` → 0，`partial` → 2，`not_reproduced` → 1。

### 1.7 P1-3 验收

1. `pytest tests/test_p1_3_determinism.py` 全绿（无浏览器依赖）。
2. 演示游戏（`examples/demo_game/index.html`）回放 3 次：
   - trace：`#start-btn` → `#level1-next` → `set_param hp=40` → `#level2-restart`
   - assertion：`{"state_equals": {"hp": 100, "scene": "level1"}}`
   - 期望：`state=reproduced`，`defect.json.reproduction` 落盘。
3. 若本机 Playwright 浏览器不可用：明确报告「单元级验收通过，浏览器级验收未执行」，不伪造结果。

---

## 二、P3-1 代码级修复定位

### 2.1 问题与目标

现状：`advisor.py` 输出的是「规则模板 + 证据定制」，不知道缺陷在源码哪一行。
目标：
1. 建立「状态字段 → 源码符号」映射；
2. 用 AST（Python）/ 轻量静态扫描（JS/HTML）检索读写点，输出 `文件:行:列`；
3. 按异常类型选择定位策略（重置遗漏 / 计分不一致 / 渲染错位）；
4. 输出 patch 草案（unified diff）+ 置信度；定位不到时明确降级为通用模板。

### 2.2 新增模块 `playranger/core/agent/code_locator.py`

数据结构：

```python
@dataclass
class CodeHit:
    path: str        # 相对源码根目录
    line: int
    column: int
    symbol: str      # 字段名
    kind: str        # read / write / declaration
    function: str    # 所在函数（找不到为 "global"）
    snippet: str     # 原始行
    score: float

@dataclass
class CodeLocation:
    fields: list[str]
    hits: dict[str, list[CodeHit]]
    strategies: list[str]
    patch: str
    confidence: float
    note: str
```

字段提取：
- `extract_fields(bug, provenance=None)`：合并 `initial_state`、`final_state`、`assertion.state_equals` 的键；
- 过滤元字段：`logic_step / available_actions / interactables / game_over / victory / raw / error`；
- 最多 6 个字段，保持出现顺序。
- `extract_fields_from_html(html)`：正则 `data-field=["']([A-Za-z_]\w*)`，用于 DOM 数据属性场景。

扫描器：
- `scan_js(source, path, fields)`：
  - 计算每行起始 offset，得到 line/column；
  - write：`\b(?:state|game|this|s)\.FIELD\s*(?:[+\-*/]?=)(?!=)` 与 `\bFIELD\s*:`（对象字面量声明）；
  - read：`\b(?:state|game|this|s)\.FIELD\b`；
  - 函数归属：从匹配位置向前找最近的 `function name(`、`name = (...) =>`、`name(...) {`、`async name(`，取函数名，找不到为 `global`。
- `scan_html(path, fields)`：抽取所有 `<script>...</script>` 块，按块前置换行数换算绝对行号，再调用 `scan_js`。
- `scan_python(source, path, fields)`：`ast` 遍历 `Attribute` / `Subscript`，判断父节点是否 `Assign/AugAssign` 得 write，否则 read；记录最近的 `FunctionDef/AsyncFunctionDef/ClassDef` 名称；列号 `col_offset + 1`。
- `locate(field, roots)`：
  - 支持文件或目录；跳过 `node_modules/.git/dist/build/__pycache__`；
  - 扩展名 `.js/.ts/.jsx/.tsx/.mjs/.html/.py`；
  - 最多 400 个文件、单文件 ≤ 1.5MB；
  - 按 `score` 降序，去重 `(path, line, column)`。

定位策略 `STRATEGY_RULES`：
- `hp_not_reset`：
  - 重置类函数名：`reset|restart|init|start|level|scene|newGame|begin`；
  - 若这些函数中存在该字段的 read 但无 write → `note` 标记「疑似遗漏重置」；
  - patch：在最佳重置函数内最后一个 write 之后追加 `state.<field> = <initial_value>;`（初始值取 `bug.initial_state[field]`）。
- `score_mismatch`：
  - 写点：累加/赋值；
  - 渲染点：函数名含 `render|update|show|display|result|win|hud|draw` 且包含 `textContent|innerHTML|innerText`；
  - 若渲染表达式出现 `field - <数字>` → patch 替换为不带偏移的 `state.<field>`。
- `ui_display_error`：渲染点存在但未引用字段 → patch 将字面量替换为 `state.<field>`。
- 其他：只列读写点，不生成 patch。

`build_patch(path, line, old_line, new_line)`：输出 `--- a/<path>` / `+++ b/<path>` / `@@ -line,1 +line,1 @@` 的 unified diff 片段。

`locate_bug(bug, source_root=None, provenance=None) -> CodeLocation`：
- source_root 解析顺序：显式参数 → `bug["source_root"]` → 环境变量 `PLAYRANGER_SOURCE_ROOT` → `game_url` 为 `file://` 时的父目录；
- 置信度：基础 0.25；命中 write +0.2；命中重置/渲染函数 +0.15；生成 patch +0.2；≥2 个字段命中 +0.1；上限 0.9；
- 完全无命中：`confidence=0.0`，`note="未在源码中定位到相关符号，已降级为通用修复模板"`。

### 2.3 修改 `playranger/core/agent/advisor.py`

- `FixPlan` 新增 `code_location: dict | None = None`，`to_dict()` 与 `to_markdown()` 输出「### 代码定位（静态扫描）」区块（字段 → 命中列表 → patch → 置信度说明）。
- `advise(bug, provenance=None, source_root=None)`：
  - 定位成功时：在最前面插入 `FixStep("按定位结果修改", ..., file_hint="path:line:col", code_hint=patch)`；
  - `verification_steps` 追加「修改后重跑 `playranger replay <bug_dir>`，复现状态应为 not_reproduced」；
  - `evidence_note` 追加定位说明；`confidence` 最多 +0.1（上限 0.95）。
- `fix_plan_from_file(bug_dir, source_root=None)` 透传。

### 2.4 修改 `playranger/core/agent/intel.py`

- `analyze_bug(bug, bug_dir=None, source_root=None)` → `advise(..., source_root=...)`；
- `save_intel()` 从 `bug["source_root"]` 或环境变量取值并传入。

### 2.5 新增 CLI `playranger locate`

```
playranger locate <bug_dir> [--source ROOT] [--write] [--json]
```

- 读 `defect.json`；调用 `locate_bug`；
- 文本输出：字段、命中 `path:line:col [write/read] function`、patch、置信度；
- `--write` 写入缺陷包 `code_location.json`；
- 退出码：有命中 0，无命中 1。

### 2.6 P3-1 验收

1. `pytest tests/test_p3_1_code_locator.py` 全绿。
2. 演示游戏静态定位：
   - `hp_not_reset` → 命中 `level2-restart` 重置函数，patch 追加 `state.hp = 100;`；
   - `score_mismatch` → 命中 `(state.score - 200)` 所在行，patch 去掉 `- 200`。
3. `playranger locate output/demo/bugs/BUG-001 --source examples/demo_game` 输出可读定位结果。
4. 若仓库内没有第二个可运行游戏：用测试夹具（含 `<script>` 的 HTML 小样）覆盖第二种结构，并在汇报中说明「第二个真实第三方游戏验证未执行」。

---

## 三、影响面与风险

| 文件 | 变更 | 风险 |
|---|---|---|
| `core/engines/settle.py` | 新增 | 低；纯新增 |
| `core/engines/play_engine.py` | 新增字段与等待 | 中；等待会增加单步耗时，已加 `settle=False` 开关 |
| `core/agent/replayer.py` | 三态、种子、落盘 | 中；`replay_from_file` 参数向后兼容 |
| `core/agent/recorder.py` | `started_at` 可选参数 | 低；默认空串 |
| `core/agent/code_locator.py` | 新增 | 低；纯新增 |
| `core/agent/advisor.py` | 可选定位注入 | 低；无 source_root 时行为不变 |
| `core/agent/intel.py` | 透传 source_root | 低 |
| `playranger/cli.py` | `replay` 增强 + `locate` 新增 | 低 |
| 测试/文档 | 新增与更新 | 低 |

回滚策略：P1-3 可通过 `settle=False` 关闭等待；P3-1 不传 `source_root` 即退回原规则模板。
