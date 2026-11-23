"""修复深化方案 —— 把「一个 bug」翻译成「怎么修、改哪里、怎么验收」。

定位：
- 不做代码自动改写，只给可执行的修复路径（独游团队真正缺的是定位与验收标准）
- 规则驱动、可解释，结论随溯源信息（provenance）动态定制
- 每条方案包含：根因候选、修复步骤、影响模块、验证清单、工作量与回归风险

设计原则：
1. 先给「最可能的根因」，再给「排查顺序」，避免一上来就大改
2. 每个修复步骤都尽量带文件/函数提示，降低小团队上下文切换成本
3. 验证清单必须可点击复跑（seed + 断言 + 复现率），而不是"再点一遍看看"
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from playranger.core.agent.code_locator import locate_bug
from playranger.core.agent.provenance import BugProvenance, build_provenance

EFFORT_LEVELS = ("低", "中", "高")
RISK_LEVELS = ("低", "中", "高")


# ─── 数据结构 ───────────────────────────────────────────────────


@dataclass
class FixStep:
    """一个修复步骤。"""

    title: str
    detail: str
    file_hint: str = ""
    code_hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FixPlan:
    """一个缺陷的修复方案。"""

    bug_id: str = ""
    anomaly_type: str = ""
    severity: str = "info"
    confidence: float = 0.0
    root_causes: list[str] = field(default_factory=list)
    fix_steps: list[FixStep] = field(default_factory=list)
    verification_steps: list[str] = field(default_factory=list)
    affected_modules: list[str] = field(default_factory=list)
    effort: str = "中"
    regression_risk: str = "中"
    evidence_note: str = ""
    code_location: dict | None = None

    def to_dict(self) -> dict:
        return {
            "bug_id": self.bug_id,
            "anomaly_type": self.anomaly_type,
            "severity": self.severity,
            "confidence": round(self.confidence, 2),
            "root_causes": self.root_causes,
            "fix_steps": [s.to_dict() for s in self.fix_steps],
            "verification_steps": self.verification_steps,
            "affected_modules": self.affected_modules,
            "effort": self.effort,
            "regression_risk": self.regression_risk,
            "evidence_note": self.evidence_note,
            "code_location": self.code_location,
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"## 修复方案：{self.bug_id}（{self.anomaly_type}）")
        lines.append("")
        lines.append(
            f"- 严重度：**{self.severity}**　方案置信度：{self.confidence:.0%}"
            f"　工作量：{self.effort}　回归风险：{self.regression_risk}"
        )
        if self.affected_modules:
            lines.append(f"- 影响模块：{'、'.join(self.affected_modules)}")
        if self.evidence_note:
            lines.append(f"- 定位依据：{self.evidence_note}")
        lines.append("")

        lines.append("### 可能根因（按概率排序）")
        for i, cause in enumerate(self.root_causes, 1):
            lines.append(f"{i}. {cause}")
        lines.append("")

        lines.append("### 修复步骤")
        for i, step in enumerate(self.fix_steps, 1):
            hint = f"（{step.file_hint}）" if step.file_hint else ""
            lines.append(f"{i}. **{step.title}**{hint}：{step.detail}")
            if step.code_hint:
                lines.append("")
                lines.append("```")
                lines.append(step.code_hint.rstrip())
                lines.append("```")
        lines.append("")

        lines.append("### 验证清单")
        for item in self.verification_steps:
            lines.append(f"- [ ] {item}")
        lines.append("")

        if self.code_location:
            lines.extend(_code_location_markdown(self.code_location))

        return "\n".join(lines).rstrip() + "\n"


# ─── 规则表 ─────────────────────────────────────────────────────
# anomaly_type -> dict(
#   root_causes, modules, steps[(title, detail, file_hint, code_hint)],
#   verify, effort, risk
# )

_RULES: dict[str, dict[str, Any]] = {
    "hp_not_reset": {
        "root_causes": [
            "关卡初始化函数只重置了场景与分数，遗漏了角色生命值",
            "重新开始走的是「重载场景」而非「重走初始化流程」，旧状态被沿用",
            "生命值存在多个副本（UI 层 / 逻辑层），只重置了其中一个",
        ],
        "modules": ["关卡初始化", "玩家状态", "重开流程"],
        "steps": [
            (
                "统一状态重置入口",
                "把所有需要重置的字段收敛到一个 reset_level() / init_player()，"
                "禁止在多个地方零散赋值，避免下次再漏字段。",
                "src/game/level.js 或 game/scene/*.js",
                "function resetLevel() {\n"
                "  state.player.hp = state.player.maxHp;\n"
                "  state.score = 0;\n"
                "  state.level = 1;\n"
                "}",
            ),
            (
                "补一条状态不变量断言",
                "在关卡启动后断言 hp === maxHp、score === 0，让同类问题在下一轮自动被捕获。",
                "SDK / 测试侧断言",
                '{"type": "state_equals", "field": "hp", "value": 100}',
            ),
            (
                "核对重开入口",
                "检查「重新开始」按钮是否调用 resetLevel()，而不是 location.reload() 或仅切场景。",
                "UI 事件绑定 / 路由",
                "",
            ),
        ],
        "verify": [
            "用原 seed 复跑同一目标，断言 hp 在重开后等于初始值",
            "连续重开 3 次，确认 hp / score / level 都回到基线",
            "检查报告里该缺陷结论是否从 human_required 变为 auto_confirmed",
        ],
        "effort": "低",
        "risk": "低",
    },
    "score_mismatch": {
        "root_causes": [
            "结算页读取的分数来源与游戏中累加的不是同一个变量",
            "结算前做了取整 / 格式化，导致显示值与实际值不一致",
            "异步更新未等待，结算读到了上一帧的旧分数",
        ],
        "modules": ["计分系统", "结算 UI", "数据绑定"],
        "steps": [
            (
                "对齐单一数据源",
                "确认游戏中加分与结算读取的是同一个 state.score，而不是两个独立计数。",
                "src/game/score.js / ui/result.js",
                "// 结算页只读 state.score，不再二次累加\nresult.textContent = String(state.score);",
            ),
            (
                "检查格式化时机",
                "取整只用于展示文本，不写回状态；避免 Math.floor 把实际分数改掉。",
                "结算渲染函数",
                "",
            ),
            (
                "补结算断言",
                "对同一局同时断言内部 score 与展示文本，二者必须相等。",
                "断言配置",
                '{"type": "state_equals", "field": "score", "value": 120}',
            ),
        ],
        "verify": [
            "用原 seed 复跑，确认结算分数等于内部 score",
            "构造边界分数（0 / 最大值 / 跨关累加）各跑一次",
        ],
        "effort": "低",
        "risk": "低",
    },
    "state_inconsistency": {
        "root_causes": [
            "状态机缺少非法迁移拦截，旧状态残留",
            "同一状态被多处写入，最后写入者覆盖了正确值",
            "异步回调在场景切换后仍然写状态",
        ],
        "modules": ["状态机", "场景生命周期", "异步任务"],
        "steps": [
            (
                "画出合法迁移表",
                "列出 scene 的合法迁移（menu → playing → result），非法迁移直接拒绝并打日志。",
                "src/game/state-machine.js",
                "if (!ALLOWED[state.scene].includes(nextScene)) return false;",
            ),
            (
                "场景销毁时取消挂起任务",
                "清理定时器 / 请求回调，避免旧场景写入新场景状态。",
                "场景卸载钩子",
                "",
            ),
            (
                "把不一致字段加入每步校验",
                "对关键字段做前后置校验，出现漂移立即记录缺陷而不是继续跑。",
                "verifier / 断言",
                "",
            ),
        ],
        "verify": [
            "复跑原轨迹，确认状态迁移不再跳变",
            "快速连续切场景 10 次，检查是否出现旧状态回写",
        ],
        "effort": "中",
        "risk": "中",
    },
    "ui_display_error": {
        "root_causes": [
            "UI 绑定字段与逻辑字段命名不一致（hp vs health）",
            "只在初始化时渲染一次，状态变化后没有刷新",
            "多语言 / 单位换算导致展示值偏移",
        ],
        "modules": ["UI 绑定层", "渲染循环"],
        "steps": [
            (
                "建立字段映射表",
                "把逻辑字段到 UI 字段的映射集中声明，避免各处手写。",
                "ui/bindings.js",
                "const BINDINGS = { hp: '#hp-value', score: '#score-value' };",
            ),
            (
                "状态变更后统一刷新",
                "在状态提交点触发一次 render()，而不是散落在各业务分支。",
                "渲染层",
                "",
            ),
            (
                "补 UI 一致性断言",
                "读取 DOM 文本与内部状态比对，纳入自动试玩检查。",
                "断言配置",
                "",
            ),
        ],
        "verify": [
            "复跑原路径，确认 UI 文本与内部状态一致",
            "触发一次扣血 / 加分，确认无需刷新页面即可更新",
        ],
        "effort": "低",
        "risk": "低",
    },
    "logic_error": {
        "root_causes": [
            "边界条件未处理（0、负数、最大值）",
            "计算顺序或运算符优先级写错",
            "浮点数累加导致误差放大",
        ],
        "modules": ["核心逻辑", "数值计算"],
        "steps": [
            (
                "复现并最小化输入",
                "从溯源的动作序列中抽出触发前状态，构造最小输入用例。",
                "tests/",
                "",
            ),
            (
                "补边界单测",
                "对 0 / 负数 / 最大值 / 临界值各写一条断言，先红后绿。",
                "tests/test_logic.py",
                "",
            ),
            (
                "固定计算顺序",
                "涉及多步计算时显式加括号，金额 / 分数使用整数运算。",
                "逻辑实现",
                "",
            ),
        ],
        "verify": [
            "单测覆盖边界值并通过",
            "用原 seed 复跑，确认断言由失败转为通过",
        ],
        "effort": "中",
        "risk": "中",
    },
    "crash_page": {
        "root_causes": [
            "未捕获的前端异常导致页面白屏 / 错误页",
            "资源加载失败（脚本 / 图片 / 音频）未做降级",
            "浏览器 API 兼容性问题",
        ],
        "modules": ["启动流程", "资源加载", "异常兜底"],
        "steps": [
            (
                "先拿到堆栈",
                "从缺陷包读取 console / pageerror 日志，定位第一个抛出点。",
                "screenshots/ 与运行日志",
                "",
            ),
            (
                "加全局异常兜底",
                "window.onerror / unhandledrejection 中上报并展示可恢复提示，避免整页不可用。",
                "入口脚本",
                "window.addEventListener('error', e => reportFatal(e.error));",
            ),
            (
                "资源加载失败降级",
                "关键资源失败时使用占位资源并继续，非关键资源静默跳过。",
                "资源加载器",
                "",
            ),
        ],
        "verify": [
            "复跑原轨迹，确认不再进入错误页",
            "断网 / 404 模拟一次，确认有降级而不是崩溃",
        ],
        "effort": "中",
        "risk": "中",
    },
    "crash": {
        "root_causes": [
            "未捕获异常导致主循环中断",
            "空引用 / undefined 访问",
            "内存或资源耗尽",
        ],
        "modules": ["主循环", "异常处理"],
        "steps": [
            (
                "定位崩溃点",
                "读取执行异常与最后一步动作，确认崩溃发生在动作执行还是渲染阶段。",
                "运行日志",
                "",
            ),
            (
                "主循环加保护",
                "单帧异常不应终止整个循环，捕获后记录并跳到下一帧。",
                "主循环",
                "try { update(); } catch (e) { logError(e); }",
            ),
            (
                "补空值防御",
                "对可能为空的查询结果与配置项做默认值处理。",
                "业务代码",
                "",
            ),
        ],
        "verify": [
            "复跑原轨迹不再崩溃",
            "连续运行 5 分钟，确认无内存增长异常",
        ],
        "effort": "中",
        "risk": "中",
    },
    "black_screen": {
        "root_causes": [
            "渲染循环停止（异常 / 条件分支提前 return）",
            "画布尺寸为 0 或离屏",
            "场景切换后未重新挂载渲染器",
        ],
        "modules": ["渲染管线", "场景切换"],
        "steps": [
            (
                "确认渲染循环是否还在跑",
                "在 draw 首帧打点，观察黑屏后是否仍有帧回调。",
                "renderer / 主循环",
                "console.debug('frame', performance.now());",
            ),
            (
                "检查画布尺寸与可见性",
                "断点查看 canvas.width/height 与 CSS display/opacity。",
                "样式与初始化",
                "",
            ),
            (
                "场景切换后重挂渲染器",
                "切场景时确保 renderer 绑定到当前 canvas，旧场景不残留。",
                "场景管理器",
                "",
            ),
        ],
        "verify": [
            "复跑原路径，黑屏帧占比降为 0",
            "连续切场景 5 次，画面均正常",
        ],
        "effort": "中",
        "risk": "中",
    },
    "white_screen": {
        "root_causes": [
            "首屏脚本报错，DOM 未挂载",
            "资源 404 导致入口阻塞",
            "容器高度为 0",
        ],
        "modules": ["首屏加载", "DOM 挂载"],
        "steps": [
            (
                "查看首屏控制台",
                "优先确认是否有脚本 404 / 语法错误。",
                "入口 HTML / JS",
                "",
            ),
            (
                "首屏加占位与超时",
                "加载超过阈值时展示占位内容，避免纯白。",
                "index.html",
                "",
            ),
            (
                "检查容器尺寸",
                "确认根容器有明确高度（100vh 或固定像素）。",
                "样式文件",
                "",
            ),
        ],
        "verify": [
            "刷新 3 次首屏均可正常渲染",
            "弱网模拟下不出现长时间白屏",
        ],
        "effort": "低",
        "risk": "低",
    },
    "frozen_screen": {
        "root_causes": [
            "主线程被同步长任务阻塞",
            "死循环或等待未满足的条件",
            "弹窗 / 阻塞式 API 挂起",
        ],
        "modules": ["主线程", "输入处理"],
        "steps": [
            (
                "抓一次性能采样",
                "用浏览器 Performance 面板确认长任务位置。",
                "运行时",
                "",
            ),
            (
                "拆分长任务",
                "把大循环拆帧执行，避免单帧超过 50ms。",
                "业务逻辑",
                "",
            ),
            (
                "移除阻塞式调用",
                "alert / confirm / 同步 XHR 一律替换为非阻塞实现。",
                "全局搜索",
                "",
            ),
        ],
        "verify": [
            "复跑原路径不再冻结",
            "连续操作 3 分钟，最长帧耗时低于 50ms",
        ],
        "effort": "中",
        "risk": "中",
    },
    "error_popup": {
        "root_causes": [
            "业务异常直接冒泡到全局提示",
            "接口失败未捕获",
            "资源加载失败触发原生弹窗",
        ],
        "modules": ["异常处理", "网络层"],
        "steps": [
            (
                "定位弹窗来源",
                "搜索 alert / throw 位置，确认是业务主动抛还是框架兜底。",
                "全局搜索",
                "",
            ),
            (
                "改为非阻塞提示",
                "使用页面内 Toast 替代原生弹窗，并记录错误码。",
                "UI 提示层",
                "",
            ),
            (
                "接口失败重试与降级",
                "对可重试错误做指数退避，不可重试时展示可恢复入口。",
                "网络层",
                "",
            ),
        ],
        "verify": [
            "复跑原路径不再出现弹窗",
            "断网情况下仍可继续操作",
        ],
        "effort": "低",
        "risk": "低",
    },
    "interaction_blocked": {
        "root_causes": [
            "原生 alert 阻塞事件循环",
            "全屏遮罩未正确关闭",
            "事件监听被移除或指针事件被禁用",
        ],
        "modules": ["输入系统", "UI 遮罩"],
        "steps": [
            (
                "移除阻塞式弹窗",
                "alert / confirm 改为页面内提示，保证事件循环不被挂起。",
                "全局搜索",
                "",
            ),
            (
                "检查遮罩层级与关闭逻辑",
                "确认 loading / modal 在失败分支也会被关闭。",
                "UI 组件",
                "finally { hideLoading(); }",
            ),
            (
                "补交互可用性检查",
                "每步执行前检查可点击元素数量，为 0 时记录缺陷。",
                "断言 / 探测器",
                "",
            ),
        ],
        "verify": [
            "复跑原路径可正常继续操作",
            "异常分支下仍能点击重试",
        ],
        "effort": "低",
        "risk": "低",
    },
    "text_overflow": {
        "root_causes": [
            "容器宽度固定且未处理长文本",
            "缺少换行 / 省略规则",
            "字体加载后尺寸变化未重排",
        ],
        "modules": ["UI 样式"],
        "steps": [
            (
                "加换行与溢出规则",
                "对可能超长的文本设置 overflow-wrap / text-overflow。",
                "样式文件",
                "overflow-wrap: anywhere;\ntext-overflow: ellipsis;",
            ),
            (
                "用最长文案做验收",
                "取多语言最长串 / 最大数字做测试数据。",
                "测试数据",
                "",
            ),
        ],
        "verify": [
            "最长文案下不溢出、不遮挡关键按钮",
        ],
        "effort": "低",
        "risk": "低",
    },
    "ui_broken": {
        "root_causes": [
            "布局约束缺失，容器尺寸变化时错位",
            "绝对定位写死像素值",
            "不同分辨率下未适配",
        ],
        "modules": ["布局", "响应式"],
        "steps": [
            (
                "改用弹性 / 网格布局",
                "减少绝对定位，按钮与面板使用 flex / grid 排列。",
                "样式文件",
                "",
            ),
            (
                "补多分辨率验收",
                "至少覆盖 1280×720 与 1920×1080 两档。",
                "手动回归",
                "",
            ),
        ],
        "verify": [
            "两档分辨率下元素不重叠、不超出屏幕",
        ],
        "effort": "中",
        "risk": "低",
    },
    "layout_break": {
        "root_causes": [
            "栅格断点缺失",
            "动态内容撑破容器",
            "滚动区域高度计算错误",
        ],
        "modules": ["布局", "样式"],
        "steps": [
            (
                "补断点规则",
                "为窄屏补充断点，关键操作区始终可见。",
                "样式文件",
                "",
            ),
            (
                "限制动态内容尺寸",
                "列表 / 弹窗设置 max-height 与滚动。",
                "组件样式",
                "",
            ),
        ],
        "verify": [
            "窄屏与宽屏下布局均完整",
        ],
        "effort": "中",
        "risk": "低",
    },
    "visual_glitch": {
        "root_causes": [
            "图层顺序 / z-index 冲突",
            "渲染时序问题（上一帧残留）",
            "像素比适配错误导致错色",
        ],
        "modules": ["渲染", "样式层级"],
        "steps": [
            (
                "确认是否可稳定复现",
                "若只在特定帧出现，优先检查清屏与双缓冲。",
                "渲染循环",
                "ctx.clearRect(0, 0, canvas.width, canvas.height);",
            ),
            (
                "固定层级规范",
                "为 UI 各层定义统一 z-index 常量，禁止魔法数字。",
                "样式常量",
                "",
            ),
        ],
        "verify": [
            "连续观察 30 秒无闪烁 / 残影",
        ],
        "effort": "中",
        "risk": "低",
    },
    "performance": {
        "root_causes": [
            "单帧计算量过大",
            "频繁 DOM 操作 / 重排",
            "资源未压缩或重复加载",
        ],
        "modules": ["渲染", "资源", "主循环"],
        "steps": [
            (
                "先量化再优化",
                "记录平均帧耗时与最差帧，避免凭感觉改。",
                "性能面板",
                "",
            ),
            (
                "减少每帧分配",
                "复用对象与数组，避免在 draw 中创建临时对象。",
                "主循环",
                "",
            ),
            (
                "资源预加载与缓存",
                "首屏后预加载下一关资源，避免切场景卡顿。",
                "资源管理器",
                "",
            ),
        ],
        "verify": [
            "平均帧耗时下降且最差帧不超过 50ms",
        ],
        "effort": "高",
        "risk": "中",
    },
    "strategy_stuck": {
        "root_causes": [
            "关卡存在死路或缺失引导，玩家无法判断下一步",
            "关键可交互元素不可见 / 不可点击",
            "目标条件无法达成（数值或逻辑卡死）",
            "Agent 策略本身陷入循环（非游戏缺陷）",
        ],
        "modules": ["关卡设计", "交互可用性", "Agent 策略"],
        "steps": [
            (
                "先人工确认是否真卡死",
                "按复现动作序列手动走一遍，确认是人类也会卡住，还是 Agent 策略问题。",
                "复现轨迹",
                "",
            ),
            (
                "输出当前可用交互清单",
                "在卡死点采集可点击元素与提示文本，判断是否缺少下一步入口。",
                "探测器",
                "",
            ),
            (
                "补引导或放宽目标条件",
                "若确为设计问题，补充视觉引导 / 提示；若为目标过严，调整判定条件。",
                "关卡配置",
                "",
            ),
        ],
        "verify": [
            "同一路径复跑可继续推进",
            "连续 3 局不再触发连续 L2 失败",
        ],
        "effort": "中",
        "risk": "中",
    },
    "unknown": {
        "root_causes": [
            "异常类型未登记，规则库暂无对应经验",
            "可能是新玩法 / 新交互引入的未知问题",
        ],
        "modules": ["待定位"],
        "steps": [
            (
                "补充人工标注",
                "先根据截图与轨迹确认现象，再把该异常登记到规则库。",
                "triage._RULES",
                "",
            ),
            (
                "缩小复现范围",
                "从回放窗口二分定位最早出现异常的那一步。",
                "复现工具",
                "",
            ),
        ],
        "verify": [
            "人工确认现象可复现",
            "补充规则后同类缺陷可自动定级",
        ],
        "effort": "中",
        "risk": "中",
    },
}


# ─── 生成入口 ───────────────────────────────────────────────────


def advise(
    bug: dict,
    provenance: BugProvenance | None = None,
    source_root: str | None = None,
) -> FixPlan:
    """为一条缺陷生成修复深化方案。

    Args:
        bug: 缺陷字典（与 recorder / API 记录同构）
        provenance: 已构建的溯源结果；不传则内部构建
        source_root: 源码根目录；不传则回落到缺陷包 / 环境变量，取不到即不注入定位

    Returns:
        FixPlan
    """
    prov = provenance or build_provenance(bug)
    anomaly = str(bug.get("anomaly_type", "")) or "unknown"
    rule = _RULES.get(anomaly, _RULES["unknown"])

    root_causes = _specialize_causes(rule["root_causes"], prov)
    fix_steps = [
        FixStep(title=t, detail=d, file_hint=f, code_hint=c)
        for (t, d, f, c) in rule["steps"]
    ]
    verification = _specialize_verification(list(rule["verify"]), prov)

    confidence = _confidence(prov)
    evidence_note = _describe_origin(prov)

    plan = FixPlan(
        bug_id=str(bug.get("bug_id", "")),
        anomaly_type=anomaly,
        severity=str(bug.get("severity", "info")),
        confidence=confidence,
        root_causes=root_causes,
        fix_steps=fix_steps,
        verification_steps=verification,
        affected_modules=list(rule["modules"]),
        effort=rule["effort"],
        regression_risk=rule["risk"],
        evidence_note=evidence_note,
    )

    # P3-1：静态扫描定位到 文件:行:列，命中时把「按定位结果修改」提到第一步
    location = locate_bug(bug, source_root=source_root, provenance=prov)
    if location.has_hits:
        first = _first_hit(location)
        total = sum(len(hits) for hits in location.hits.values())
        plan.fix_steps.insert(
            0,
            FixStep(
                title="按定位结果修改",
                detail=(
                    f"静态扫描命中 {total} 处，建议先改 "
                    f"{first.path}:{first.line}:{first.column}（{first.function}）"
                ),
                file_hint=f"{first.path}:{first.line}:{first.column}",
                code_hint=location.patch,
            ),
        )
        plan.verification_steps.append(
            "修改后重跑 `playranger replay <bug_dir>`，复现状态应为 not_reproduced"
        )
        loc_note = f"静态定位：{first.path}:{first.line}"
        plan.evidence_note = (
            f"{plan.evidence_note}，{loc_note}" if plan.evidence_note else loc_note
        )
        plan.confidence = round(min(0.95, plan.confidence + 0.1), 2)
        plan.code_location = location.to_dict()

    return plan


def fix_plan_from_file(bug_dir: str, source_root: str | None = None) -> FixPlan:
    """从缺陷包目录直接生成修复方案。"""
    from playranger.core.agent.recorder import load_defect

    record = load_defect(bug_dir)
    bug = record.to_dict()
    return advise(
        bug,
        build_provenance(bug, bug_dir=bug_dir),
        source_root=source_root,
    )


def cluster_bugs(bugs: list[dict]) -> list[dict]:
    """按「异常类型 + 触发阶段」聚类，便于批量派单。

    独游团队通常一次只修一类问题，聚类能直接把 10 个 bug 收敛成 3 个修复任务。
    """
    groups: dict[tuple[str, str], dict] = {}
    for bug in bugs:
        anomaly = str(bug.get("anomaly_type", "unknown"))
        stage = _stage_of(str(bug.get("severity", "info")))
        key = (anomaly, stage)
        group = groups.setdefault(
            key,
            {
                "anomaly_type": anomaly,
                "stage": stage,
                "bug_ids": [],
                "count": 0,
                "highest_severity": "info",
                "titles": [],
            },
        )
        group["bug_ids"].append(bug.get("bug_id", ""))
        group["count"] += 1
        group["titles"].append(bug.get("title", ""))
        if _sev_index(str(bug.get("severity", "info"))) > _sev_index(group["highest_severity"]):
            group["highest_severity"] = str(bug.get("severity", "info"))

    result = sorted(
        groups.values(),
        key=lambda g: (_sev_index(g["highest_severity"]), g["count"]),
        reverse=True,
    )
    return result


# ─── 内部 ───────────────────────────────────────────────────────


def _first_hit(location) -> Any:
    """优先取 patch 指向的那条命中，其次取分数最高的命中。"""
    patch = getattr(location, "patch", "") or ""
    path = ""
    for text in patch.splitlines():
        if text.startswith("--- a/"):
            path = text[len("--- a/") :]
            break
    header = re.search(r"^@@ -(\d+),", patch, re.M)
    if header:
        line = int(header.group(1))
        for hits in location.hits.values():
            for hit in hits:
                if hit.line == line and (not path or hit.path == path):
                    return hit
    for hits in location.hits.values():
        if hits:
            return hits[0]
    return None


def _code_location_markdown(location: dict) -> list[str]:
    """渲染「### 代码定位（静态扫描）」区块。"""
    lines: list[str] = ["### 代码定位（静态扫描）", ""]

    note = location.get("note") or ""
    if note:
        lines.append(f"- 说明：{note}")
    lines.append(f"- 置信度：{float(location.get('confidence') or 0.0):.0%}")
    strategies = location.get("strategies") or []
    if strategies:
        lines.append(f"- 策略：{'、'.join(strategies)}")
    lines.append("")

    for field_name, hits in (location.get("hits") or {}).items():
        if not hits:
            continue
        lines.append(f"**{field_name}**")
        for hit in hits[:8]:
            lines.append(
                f"- `{hit.get('path')}:{hit.get('line')}:{hit.get('column')}` "
                f"[{hit.get('kind')}] {hit.get('function')}"
            )
        lines.append("")

    patch = location.get("patch") or ""
    if patch:
        lines.append("```diff")
        lines.append(patch.rstrip())
        lines.append("```")
        lines.append("")

    return lines


def _specialize_causes(causes: list[str], prov: BugProvenance) -> list[str]:
    """把溯源里的具体字段差异注入根因描述，避免模板味。"""
    result = list(causes)

    hit = {d.get("field"): d for d in prov.state_diff}
    for field in ("hp", "score", "level", "scene"):
        diff = hit.get(field)
        if not diff:
            continue
        expected = diff.get("expected")
        line = (
            f"实测证据：`{field}` 在触发前后为 "
            f"{_short(diff.get('before'))} → {_short(diff.get('after'))}"
        )
        if expected is not None:
            line += f"，期望 {_short(expected)}"
        result.insert(0, line)

    if prov.assertion_check and not prov.assertion_check.get("passed", True):
        ac = prov.assertion_check
        result.insert(
            0,
            f"断言 `{ac.get('field')}` 期望 {_short(ac.get('expected'))}，"
            f"实际 {_short(ac.get('actual'))}，失败点可直接用于回归",
        )

    # 去重保序
    seen: set[str] = set()
    unique: list[str] = []
    for item in result:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _specialize_verification(items: list[str], prov: BugProvenance) -> list[str]:
    if prov.origin_step is not None:
        items.insert(0, f"从第 {prov.origin_step} 步的复现窗口重放，确认异常消失")
    if prov.screenshots:
        items.append("对照缺陷包 screenshots/ 下的失败截图，确认视觉层面也已修复")
    if prov.evidence_sources:
        items.append("确认证据链校验仍通过（截图未被替换、条目未被删改）")
    return items


def _confidence(prov: BugProvenance) -> float:
    score = 0.55
    if prov.origin_step is not None:
        score += 0.1
    if prov.state_diff:
        score += 0.1
    if prov.assertion_check and not prov.assertion_check.get("passed", True):
        score += 0.1
    if len(prov.findings) >= 2:
        score += 0.1
    if prov.screenshots:
        score += 0.05
    return round(min(1.0, score), 2)


def _describe_origin(prov: BugProvenance) -> str:
    parts: list[str] = []
    if prov.origin_step is not None:
        parts.append(f"触发于第 {prov.origin_step} 步")
    if prov.origin_action:
        parts.append(f"动作 {BugProvenance._format_action(prov.origin_action)}")
    if prov.channels:
        parts.append("通道 " + "/".join(prov.channel_labels()))
    if prov.state_diff:
        parts.append(f"{len(prov.state_diff)} 个字段发生异常变化")
    return "，".join(parts)


def _stage_of(severity: str) -> str:
    """把严重度映射到修复阶段，便于排期。"""
    if severity in ("blocker", "critical"):
        return "立即修"
    if severity == "major":
        return "本迭代修"
    return "排期修"


def _sev_index(severity: str) -> int:
    order = ("info", "minor", "major", "critical", "blocker")
    try:
        return order.index(severity)
    except ValueError:
        return 0


def _short(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text if len(text) <= 40 else text[:37] + "..."


__all__ = [
    "FixPlan",
    "FixStep",
    "advise",
    "cluster_bugs",
    "fix_plan_from_file",
]
