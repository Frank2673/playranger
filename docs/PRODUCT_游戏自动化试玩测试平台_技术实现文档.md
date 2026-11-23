# PlayRanger「试玩队」—— 游戏自动化试玩与测试 Agent
## 技术实现文档（TRAE 好应用大赏赛参赛版）

> 版本：v0.2　｜　更新：2026-11-23　｜　形态：本地 Web 应用（FastAPI + 浏览器 Agent + 实时控制台）
> 一句话：给独游开发者/游戏团队一个「能真的把游戏玩起来、把 Bug 记下来、把修复建议落到源码行」的自动化试玩测试台。

---

## 一、要解决的真实痛点

| # | 开发者真实痛点 | 现状做法 | PlayRanger 的做法 |
|---|---|---|---|
| 1 | 每次发版都要人工回归，独立开发者没有 QA | 自己点、录视频、手写 Bug | 浏览器 Agent 按目标自动试玩，逐步留证 |
| 2 | 多款游戏/多个版本要同时测，笔记本跑不动 | 一次只开一个，排队等 | 按可用内存自动算并发，任务队列 + 动态扩缩 |
| 3 | 测试过程是黑盒，团队无法审查 | 只看一句「跑完了」 | 控制台实时展示步骤、状态、Bug 证据链、报告 |
| 4 | Bug 描述不可复现，开发来回问 | 截图 + 口述 | 缺陷包含动作序列、状态差异、种子、复现率、源码定位 |
| 5 | 修 Bug 靠猜，改完不知道有没有引入回归 | 改一版跑一遍 | patch 沙箱校验 + 可选测试命令 + 变更前后结果 |
| 6 | 工具只能本地命令行，团队协作困难 | 各自装环境 | 账号注册登录、首个账号管理员、任务与缺陷持久化 |

**明确不做**：不做通用 RPA、不做云游戏平台、不承诺「全自动修完所有 Bug」。修复是「定位 + patch 草案 + 沙箱验证」，最终由开发者确认。

---

## 二、比赛要求映射

| 比赛关注点 | 本作品对应实现 | 可演示证据 |
|---|---|---|
| 真实可用，不是静态页面 | Playwright 真浏览器执行、真实 Bug 包、实时 API | 启动真实游戏 → 任务运行 → 控制台进度变化 |
| AI 能力落地 | LLM Planner + 视觉检测 + ReAct 自修正 + 规则修复建议 | 无 API Key 自动降级 MockBrain，仍可演示流程 |
| 解决实际痛点 | 自动试玩、并发调度、Bug 审查、修复验证 | 单游戏/多游戏并发、内存自适应 |
| 完整体验 | 注册登录 → 建任务 → 看板 → 审查 → 修复验证 | 一条龙 Demo 脚本（见第八节） |
| 工程质量 | 302 项自动化测试、持久化、鉴权、限流边界 | `pytest` 全绿、数据库可查 |
| 安全与边界 | 密码 PBKDF2、Token 会话、路径白名单、修复只在沙箱 | 无 Key 401、越界路径拒绝 |
| 可持续 | 分阶段路线图、实验方案、诚实不足清单 | ROADMAP + 实验文档 |

---

## 三、总体架构

```
┌──────────────────────────────────────────────────────────────┐
│  浏览器控制台（/app）                                          │
│  登录/注册 · 内存仪表 · 并发滑块 · 任务看板 · Bug 审查 · 事件流  │
└───────────────▲──────────────────────────────┬───────────────┘
                │ JSON API / 轮询              │ 操作指令
┌───────────────┴──────────────────────────────▼───────────────┐
│  FastAPI 控制面（playranger/api/console.py）                   │
│  Auth（PBKDF2 + Token） · System（内存/CPU） · Tasks · Fixes    │
└───────────────▲──────────────────────────────┬───────────────┘
                │                              │
┌───────────────┴──────────────┐  ┌────────────▼───────────────┐
│  SQLite 持久层                │  │  任务编排器 Orchestrator    │
│  users/sessions/tasks/events │  │  队列 · Worker 池 · 启停    │
└──────────────────────────────┘  │  暂停 · 扩缩 · 取消         │
                                  └────────────┬───────────────┘
                                               │ 每个任务独立线程
                                  ┌────────────▼───────────────┐
                                  │  真实执行器 Runner          │
                                  │  PlayEngine + ReActLoop     │
                                  │  Recorder + Memory + 定位   │
                                  └────────────┬───────────────┘
                                               │ 产物
                        ┌──────────────────────▼──────────────────────┐
                        │ output/tasks/<task_id>/                       │
                        │  bugs/BUG-xxx/{defect,provenance,fix_plan,...}│
                        │  result.json · progress.json · screenshots/   │
                        └─────────────────────────────────────────────┘
```

### 关键设计决策

1. **控制面与执行面分离**：API 只读写数据库与内存状态；执行器独立线程运行浏览器，避免一个任务卡死拖垮 API。
2. **SQLite 而非内存**：账号、任务、事件重启不丢；单机零运维，符合独立开发者场景。
3. **浏览器每次任务独立实例**：互不污染 localStorage/缓存；并发上限由内存决定。
4. **协作式取消**：ReActLoop 每步回调检查取消标记，最多等一个动作周期；不强行跨线程关闭 Playwright。
5. **修复只进沙箱**：不直接改用户仓库，先生成副本、校验 patch、可选跑测试命令，再由用户决定是否落盘。

---

## 四、数据模型（SQLite）

```sql
users(id INTEGER PK, username TEXT UNIQUE, password_hash TEXT, salt TEXT,
      role TEXT CHECK(role IN ('admin','member')), created_at TEXT)

sessions(token TEXT PK, user_id INTEGER, created_at TEXT, expires_at TEXT)

tasks(id TEXT PK, user_id INTEGER, project_id TEXT, game_url TEXT, goal TEXT,
      max_steps INTEGER, seed INTEGER, status TEXT, workers INTEGER,
      output_dir TEXT, created_at TEXT, started_at TEXT, finished_at TEXT,
      result_json TEXT, error TEXT)

events(id INTEGER PK AUTOINCREMENT, task_id TEXT, level TEXT,
       message TEXT, created_at TEXT)
```

- 任务状态机：`queued → running → completed | failed | cancelled`；进程重启时遗留 `running` 统一标记 `interrupted`。
- 首个注册用户 `role=admin`，后续为 `member`；管理员可查看全部用户与全局并发。
- Token 默认 7 天过期，`Authorization: Bearer <token>`。

---

## 五、并发与内存自适应

### 5.1 计算规则（可复算）

```
reserve_gb      = 2.0                  # 留给系统/浏览器外壳
per_worker_mb   = 1536                 # 单个试玩任务峰值预算
available_gb    = psutil.virtual_memory().available / 1024**3
recommended     = floor((available_gb - reserve_gb) * 1024 / per_worker_mb)
recommended     = clamp(recommended, 1, min(8, cpu_count))
```

- 页面展示：总内存、可用内存、单任务预算、建议并发、当前并发。
- 用户可在 `1..hard_max(8)` 内手动覆盖；超过建议值时控制台标黄提示「可能触发 swap」。
- 扩缩容只影响「之后从队列取任务」，不会中断正在跑的任务。

### 5.2 调度器

- 内存队列 + 固定目标并发；每 0.5s 由 supervisor 补足 worker。
- `pause()`：不再取新任务；`resume()`：继续。
- `stop_task(id)`：队列中直接取消；运行中置取消标记，Runner 在下一步退出。
- `stop_all()`：暂停 + 取消全部运行任务。
- 事件表记录每次状态变化，控制台按 `since_id` 增量拉取。

---

## 六、真实执行流程（单任务）

1. 创建输出目录 `output/tasks/<task_id>/`；
2. `PlayEngine` 启动 Edge/Chromium（CI 参数、固定视口、重试）；
3. `BugRecorder` 接管缺陷包；`AgentMemory` 读取历史经验；
4. `ReActLoop` 循环：感知 → 规划（LLM/Mock）→ 校验 → 执行 → 验证 → 修正；
5. 每步回调写入 `progress.json` 与事件表（步号、动作、状态、异常）；
6. 发现缺陷：记录动作序列、初始/最终状态、截图、种子、复现率、代码定位、修复方案；
7. 结束：写 `result.json`（步数、状态、Bug 数、运行环境、brain 来源）；
8. 控制台可打开 Bug 卡片查看证据与 patch，并调用修复沙箱验证。

### 性能采样与无头兜底

- 页面内：`requestAnimationFrame` 帧间隔 + `PerformanceObserver('longtask')` + `performance.memory`；
- 无头模式 rAF 被节流、帧样本不足时，自动切换 **CDP Performance 指标**：
  `TaskDuration / ScriptDuration / LayoutDuration / JSHeapUsedSize` 的窗口增量；
- 输出 `mode=cdp_fallback`、`busy_ratio`、`heap_used_mb`、`long_task_count`，并明确 `fps_unavailable=true`，**不伪造 FPS**；
- 判定：`busy_ratio > 0.85` 或 `heap_used_mb` 超阈值 → `performance` 缺陷。

---

## 七、API 一览（控制台）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/console/auth/register` | 注册；首个用户自动 admin |
| POST | `/api/console/auth/login` | 登录，返回 Bearer Token |
| POST | `/api/console/auth/logout` | 注销当前 Token |
| GET | `/api/console/auth/me` | 当前用户 |
| GET | `/api/console/system` | 内存/CPU/建议并发/运行数 |
| GET | `/api/console/tasks` | 任务列表（含进度、Bug 数） |
| POST | `/api/console/tasks` | 创建任务（默认自动并发） |
| GET | `/api/console/tasks/{id}` | 任务详情 |
| POST | `/api/console/tasks/{id}/stop` | 停止（队列/运行） |
| POST | `/api/console/tasks/{id}/retry` | 复制配置重新入队 |
| GET | `/api/console/tasks/{id}/bugs` | 读取该任务缺陷包摘要 |
| POST | `/api/console/scale` | 调整目标并发 |
| POST | `/api/console/pause` / `resume` | 暂停/恢复取任务 |
| POST | `/api/console/stop-all` | 全部停止 |
| GET | `/api/console/events?since=` | 增量事件流 |
| POST | `/api/console/fixes/validate` | 校验 patch 是否可应用 |
| POST | `/api/console/fixes/verify` | 沙箱应用 + 可选测试命令 |
| GET | `/api/console/tasks/{id}/steps` | 逐步试玩轨迹 |
| GET | `/api/console/tasks/{id}/artifacts/{name}` | 读取任务产物（截图/状态文件） |
| GET | `/api/console/admin/users` | 管理员查看用户列表 |
| GET | `/api/console/admin/overview` | 管理员全局概览 |

> 兼容：原 `/api/v1/*` 保持不变；控制台接口独立前缀，避免破坏既有集成。

---

## 八、Demo 脚本（评委 5 分钟）

1. 启动：`playranger serve` → 打开 `http://127.0.0.1:8000/app`；
2. 注册第一个账号 → 自动成为管理员，顶部显示角色；
3. 系统卡显示本机内存与建议并发（例如 32GB → 建议 8）；
4. 新建任务：填入 `examples/demo_game/index.html` 与目标，步数 10；
5. 同一页面再建 2 个任务（不同目标/不同游戏），把并发从 2 调到 4 → 看板同时出现多个 running；
6. 点击暂停 → 队列不再启动；恢复 → 继续；停止某个任务 → 状态 `cancelled`；
7. 打开任务 → 步骤时间线、Bug 卡片 → 查看证据链、源码定位、修复建议；
8. 在修复面板点击「沙箱验证」→ 返回 patch 是否可应用（可选跑测试命令）；
9. 管理员接口可查看用户列表；退出登录后访问任务接口返回 401。

---

## 九、安全与边界

- 密码：PBKDF2-HMAC-SHA256，200k 迭代，随机盐；不存明文。
- 会话：随机 32 字节 Token，服务端以 SHA-256 摘要入库，有效期 7 天；前端 localStorage 保存原始值，退出或 401 自动清除。
- 路径：修复沙箱仅允许 `PLAYRANGER_WORKSPACE_ROOTS` 白名单内目录。
- 资源：并发硬上限 8；单任务步数上限 500；修复测试命令超时 120s。
- 诚实边界：
  - 无 LLM API Key 时自动降级 MockBrain（会走 wait 动作），Demo 需配置真实 Key 才能看到有效探索；
  - 修复是「patch 草案 + 沙箱验证」，不是无人值守自动合并；
  - patch 校验已落地 `git apply --check`（非 git 目录用临时仓库等价校验），沙箱应用与可选测试命令已验证；但「修复后缺陷消失」的回归判定尚未自动化；
  - A/B/C/E 对照实验仍 pending，不输出发现率/节省工时结论；C 组目前只有 2 个夹具用例；
  - 暂只支持 Web 游戏；Unity/手游通过适配协议占位，未验证。

---

## 十、实施状态与验收（已落地）

| 阶段 | 内容 | 状态 | 落地位置 / 验证 |
|---|---|---|---|
| P1 控制面 | SQLite、注册登录、首用户 admin、系统信息 | **已完成** | `playranger/platform/db.py`、`auth.py`、`system.py`；`tests/test_platform_core.py` |
| P2 调度 | 队列、Worker 池、内存建议、暂停/恢复/扩缩/停止 | **已完成** | `playranger/platform/orchestrator.py`；并发上限、暂停、取消、重跑测试 |
| P3 执行 | Runner 接入 PlayEngine/ReActLoop、步进进度、Bug 归档 | **已完成** | `playranger/platform/runner.py`；真实 demo_game 8 步跑通并产出 `steps.jsonl` / `result.json` / 截图 |
| P4 可视化 | 控制台登录、内存仪表、任务看板、Bug 审查 | **已完成** | `playranger/web/index.html`（真实 API 轮询，非静态 mock）；API E2E 返回 200/40KB |
| P5 修复 | patch 校验、沙箱应用、测试命令 | **已完成** | `playranger/platform/fixes.py`；正确 patch 通过、错误 patch 拒绝、原目录不被修改、测试命令在沙箱执行 |
| P6 性能兜底 | CDP 指标、无头模式判定 | **已完成** | `core/engines/perf.py::summarize_cdp`、`play_engine._cdp_perf_fallback`；真实浏览器返回 `mode=cdp_fallback`、`fps_unavailable=true` |

### 10.1 关键实现约束

- `/api/console` 与 `/api/v1` 完全独立；既有集成与 302 项回归不受影响。
- 任务入队使用**条件更新**（`status NOT IN ('queued','running')`），避免调度器刚领取的 running 被提交动作写回 queued，导致同一任务被执行两次。
- 调度器每 0.5s 补位；缩容只影响后续领取，不打断运行中任务。
- 取消为协作式：`ReActLoop` 每步开始/结束检查 `should_stop()`，最多等待一个动作周期。
- 前端无外部依赖，单文件 HTML；Token 存 `localStorage`，401 自动退出。

### 10.2 已验证证据

- 新增平台测试 **25 项全部通过**（`tests/test_platform_core.py` 16 项、`tests/test_platform_console.py` 9 项）。
- 真实浏览器 E2E：demo_game 跑完 8 步，生成 `progress.json`、`steps.jsonl`、`result.json`、16 张步骤截图；API 全链路（注册 → 扩缩 → 建任务 → 轮询 → 步骤/事件）通过。
- CDP 兜底实测返回 `busy_ratio=0.013`、`heap_used_mb=1.8`、`fps_unavailable=true`，未伪造 FPS。
- 全量回归：既有 302 项 + 新增 25 项，运行方式见第十一节。

### 10.3 尚未完成（诚实标注）

- A 人工 / B 盲测 / C 状态桥接 / E 人工定位对照实验 still pending；C 组仅 2 个夹具用例，**不能**据此给发现率、误报率或节省工时结论。
- 修复后的自动回归（重新跑任务确认缺陷消失）尚未接入修复沙箱。
- 多机分布式调度、Unity/手游真机适配未实现。

---

## 十一、运行方式

```bash
# 安装
pip install -r requirements.txt

# 启动（默认 0.0.0.0:8000）
playranger server --port 8000
# 或
python -m uvicorn playranger.api.main:app --host 127.0.0.1 --port 8000

# 打开控制台
http://127.0.0.1:8000/app/

# 指定本机 Edge（未执行 playwright install 时）
# Windows PowerShell:
$env:PLAYWRIGHT_CHROMIUM_PATH="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 测试
pytest tests -q -p no:cacheprovider
```

环境变量：

| 变量 | 作用 |
|---|---|
| `PLAYRANGER_DB_PATH` | SQLite 路径（默认 `output/platform.db`） |
| `PLAYRANGER_TASKS_DIR` | 任务输出根（默认 `output/tasks`） |
| `PLAYRANGER_SANDBOX_ROOT` | 修复沙箱根（默认 `output/fix_sandbox`） |
| `PLAYRANGER_WORKSPACE_ROOTS` | 修复沙箱允许的源码根，`;` 分隔 |
| `PLAYRANGER_HEADLESS` | 任务执行是否无头（默认 1） |
| `PLAYRANGER_CAPTURE_SCREENSHOTS` | 是否保存步骤截图（默认 1） |
| `PLAYRANGER_PER_WORKER_MB` | 单任务内存预算（默认 1536） |
| `PLAYRANGER_RESERVE_GB` | 系统预留内存（默认 2.0） |
| `PLAYRANGER_MAX_WORKERS` | 并发硬上限（默认 8，允许 1-32） |
| `LLM_API_BASE / LLM_API_KEY / LLM_MODEL` | 真实 Planner 配置 |

---

## 十二、与现有文档的关系

- 能力/亮点/不足：[`SUBMISSION_功能说明与亮点不足.md`](SUBMISSION_功能说明与亮点不足.md)
- 不足收敛路线：[`ROADMAP_不足收敛与差异化.md`](ROADMAP_不足收敛与差异化.md)
- 2048 对照实验：[`EXPERIMENT_2048_真实项目对照实验方案.md`](EXPERIMENT_2048_真实项目对照实验方案.md)
- 代码级定位与修复：[`tasks_P1-3_P3-1.md`](tasks_P1-3_P3-1.md)
