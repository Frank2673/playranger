"""试玩引擎 —— 用 Playwright 驱动浏览器玩游戏。

核心能力：
- SDK 模式：通过 page.evaluate 调用页面上的 window.__playranger__
- 盲测模式：扫描 DOM 推断可交互元素（P1 阶段实现）
- 逻辑步编号贯穿所有状态和动作
- 探索模式 / 复现模式 双模式支持
- 截图、状态快照、轨迹记录

设计原则（来自 Codex 方案交叉验证）：
- 语义接口（SDK 调用）和真实键鼠输入要分别验收，不混淆覆盖范围
- 失败时保留证据，不用 try-catch 吞关键错误
- 状态断言用业务结果（hp/score/scene），不用"页面可见"当通过条件
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

# Playwright 延迟可用：未安装时允许导入本模块（doctor / 原生平台适配器不需要浏览器），
# 只有真正启动 PlayEngine 时才报明确错误，避免「装不上 playwright 就整个包不可用」。
try:
    from playwright.sync_api import (
        sync_playwright,
        Page,
        Browser,
        Playwright,
        TimeoutError as PWTimeoutError,
    )

    PLAYWRIGHT_AVAILABLE = True
    PLAYWRIGHT_IMPORT_ERROR: str | None = None
except ImportError as _pw_err:  # pragma: no cover - 取决于环境
    PLAYWRIGHT_AVAILABLE = False
    PLAYWRIGHT_IMPORT_ERROR = str(_pw_err)
    sync_playwright = None  # type: ignore[assignment]
    Page = Any  # type: ignore[misc,assignment]
    Browser = Any  # type: ignore[misc,assignment]
    Playwright = Any  # type: ignore[misc,assignment]
    PWTimeoutError = TimeoutError  # type: ignore[misc,assignment]

from playranger.core.engines.settle import (
    DEFAULT_TIMEOUT_MS,
    SettleResult,
    build_seed_script,
    settle as settle_page,
)
from playranger.core.engines import perf as perf_sampler


# CI 环境固定启动参数：禁 GPU、禁 /dev/shm、静音、关后台节流
_CI_LAUNCH_ARGS = [
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--mute-audio",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--force-device-scale-factor=1",
]


# ── 数据结构 ───────────────────────────────────────────────


@dataclass
class StepRecord:
    """单步记录，用于轨迹追踪。"""

    logic_step: int
    action_type: str
    action_target: str | None
    action_params: dict[str, Any]
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    ok: bool
    error: str | None = None
    screenshot_before: str | None = None
    screenshot_after: str | None = None
    duration_ms: float = 0.0
    settle: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class GameState:
    """游戏状态快照。"""

    scene: str
    hp: int
    score: int
    level: int
    game_over: bool
    victory: bool
    available_actions: list[str]
    interactables: list[dict[str, Any]]
    raw: dict[str, Any]  # 原始状态，扩展用

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameState":
        return cls(
            scene=data.get("scene", "unknown"),
            hp=int(data.get("hp", 0)),
            score=int(data.get("score", 0)),
            level=int(data.get("level", 0)),
            game_over=bool(data.get("game_over", False)),
            victory=bool(data.get("victory", False)),
            available_actions=list(data.get("available_actions", [])),
            interactables=list(data.get("interactables", [])),
            raw=data,
        )


# ── 引擎主体 ───────────────────────────────────────────────


class PlayEngine:
    """浏览器驱动引擎。

    两种模式：
    - SDK 模式（推荐）：调用页面 window.__playranger__.observe/execute
    - 盲测模式（P1）：纯 DOM 扫描 + 视觉定位

    支持探索/复现两种运行模式（通过 mode 参数控制行为）。
    """

    def __init__(
        self,
        url: str,
        headless: bool = True,
        sdk_mode: bool = True,
        mode: str = "explore",  # explore / reproduce
        slow_mo: int = 0,
        default_timeout: int = 5000,
        executable_path: str | None = None,
        seed: int | None = None,
        settle: bool = True,
        settle_timeout_ms: int = DEFAULT_TIMEOUT_MS,
        ci_mode: bool = False,
        retry: int = 1,
        viewport: tuple[int, int] = (1280, 720),
        navigation_timeout_ms: int | None = None,
        action_timeout_ms: int | None = None,
        perf: bool = False,
        perf_thresholds: dict | None = None,
    ) -> None:
        self.url = url
        self.headless = headless
        self.sdk_mode = sdk_mode
        self.mode = mode
        self.slow_mo = slow_mo
        self.default_timeout = default_timeout

        # P1-4 CI 稳定性：固定参数 / 视口 / 重试 / 超时分级
        self.ci_mode = bool(ci_mode or os.environ.get("PLAYRANGER_CI") == "1")
        self.retry = max(0, int(retry))
        self.viewport = viewport
        self.navigation_timeout_ms = navigation_timeout_ms or max(default_timeout, 15000)
        self.action_timeout_ms = action_timeout_ms or default_timeout
        self.retry_count = 0
        self.retry_reasons: list[str] = []

        # P1-5 性能采样
        self.perf = perf
        self.perf_thresholds = perf_thresholds or {}

        # 确定性：随机种子 + 页面稳定等待
        self.seed = seed
        self.settle = settle
        self.settle_timeout_ms = settle_timeout_ms
        self.seed_supported = False
        self._last_settle: SettleResult | None = None

        # 浏览器可执行文件路径：优先参数 → 环境变量 → Playwright 默认
        if executable_path:
            self.executable_path = executable_path
        else:
            self.executable_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None

        # Playwright 资源
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None

        # 状态追踪
        self._logic_step: int = 0
        self._trace: list[StepRecord] = []
        self._started: bool = False

        # 无头 rAF 节流兜底：CDP Performance 指标
        self._cdp_session: Any | None = None
        self._cdp_baseline: dict[str, float] | None = None
        self._cdp_baseline_at: float | None = None

    # ── 生命周期 ────────────────────────────────────────

    def start(self) -> None:
        """启动浏览器，打开页面，等待 SDK 就绪。

        P1-4：失败自动重试（默认 1 次），每次失败原因写入 retry_reasons，
        供报告与 CI 日志使用；重试仍失败时抛出最后一次异常。
        """
        if self._started:
            return

        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "未安装 playwright，无法启动浏览器引擎。"
                "请执行 `pip install playwright && playwright install chromium`；"
                "若只想测 Android/Windows/Linux/iOS，可不装浏览器，直接用对应原生适配器。"
                f"（原始错误: {PLAYWRIGHT_IMPORT_ERROR}）"
            )

        last_error: Exception | None = None
        for attempt in range(self.retry + 1):
            try:
                self._start_once()
                self._started = True
                return
            except Exception as e:  # noqa: BLE001 —— 启动阶段任何异常都可重试
                last_error = e
                self.retry_reasons.append(str(e)[:200])
                self._teardown()
                if attempt < self.retry:
                    self.retry_count += 1
                    # 线性退避，给残留进程一点清理时间
                    time.sleep(0.5 * (attempt + 1))

        raise RuntimeError(
            f"浏览器启动失败（已尝试 {self.retry + 1} 次）：{last_error}"
            f"；失败原因：{' | '.join(self.retry_reasons)}"
        ) from last_error

    def _start_once(self) -> None:
        """单次启动流程（start 的重试单元）。"""
        self._pw = sync_playwright().start()

        launch_args = list(_CI_LAUNCH_ARGS) if (self.ci_mode or self.headless) else []
        launch_kwargs: dict = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
        }
        if launch_args:
            launch_kwargs["args"] = launch_args
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path

        self._browser = self._pw.chromium.launch(**launch_kwargs)
        width, height = self.viewport
        self._page = self._browser.new_page(
            viewport={"width": int(width), "height": int(height)},
            reduced_motion="reduce",
        )
        self._page.set_default_timeout(self.action_timeout_ms)

        # 打开页面
        if self.perf:
            self._page.add_init_script(perf_sampler.PERF_SCRIPT)
        if self.seed is not None:
            # 页面脚本执行前注入全局种子，SDK 不支持 setSeed 时也能读到
            self._page.add_init_script(build_seed_script(self.seed))
        self._page.goto(
            self.url,
            timeout=self.navigation_timeout_ms,
            wait_until="domcontentloaded",
        )

        # SDK 模式下等待 SDK 注入
        if self.sdk_mode:
            self._wait_for_sdk()
            if self.seed is not None:
                try:
                    self.seed_supported = self.sdk_set_seed(self.seed)
                except Exception:
                    self.seed_supported = False

        if self.perf:
            self._init_cdp_metrics()

    def _wait_for_sdk(self, timeout_ms: int = 5000) -> None:
        """等待 window.__playranger__ 就绪。"""
        assert self._page is not None
        try:
            self._page.wait_for_function(
                "() => typeof window.__playranger__ !== 'undefined'",
                timeout=timeout_ms,
            )
        except PWTimeoutError as e:
            raise RuntimeError(
                f"等待 PlayRanger SDK 超时（{timeout_ms}ms）。"
                "请确认页面已注入 window.__playranger__。"
            ) from e

    def close(self) -> None:
        """关闭浏览器，释放资源。"""
        self._teardown()
        self._started = False

    def _teardown(self) -> None:
        """只释放资源，不改变 _started（供启动失败重试使用）。"""
        if self._cdp_session is not None:
            try:
                self._cdp_session.detach()
            except Exception:
                pass
            self._cdp_session = None
        self._cdp_baseline = None
        self._cdp_baseline_at = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._page = None

    # ── 上下文管理 ──────────────────────────────────────

    def __enter__(self) -> "PlayEngine":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── 确定性辅助 ──────────────────────────────────────

    @property
    def seed_mode(self) -> str:
        """种子注入方式：sdk（setSeed 生效）/ global（仅全局变量）/ none。"""
        if self.seed is None:
            return "none"
        return "sdk" if self.seed_supported else "global"

    def _settle(self) -> None:
        """等待页面稳定（可在构造时用 settle=False 关闭）。"""
        if not self.settle or self._page is None:
            return
        self._last_settle = settle_page(self._page, self.settle_timeout_ms)

    # ── 状态读取 ────────────────────────────────────────

    def read_state(self) -> GameState:
        """读取当前游戏状态。

        SDK 模式：调用 __playranger__.observe()
        盲测模式：从 DOM 推断（P1 实现）
        """
        assert self._page is not None

        if self.sdk_mode:
            raw = self._page.evaluate("() => window.__playranger__.observe()")
        else:
            raw = self._scan_dom_state()

        return GameState.from_dict(raw)

    def _scan_dom_state(self) -> dict[str, Any]:
        """盲测模式：从 DOM 推断状态（P1 实现，先给骨架）。"""
        assert self._page is not None
        # 简单版本：从可见文本和元素推断
        buttons = self._page.evaluate("""() => {
            const btns = [];
            document.querySelectorAll('button').forEach(b => {
                const rect = b.getBoundingClientRect();
                btns.push({
                    id: b.id,
                    text: b.innerText,
                    visible: rect.width > 0 && rect.height > 0,
                    x: rect.left + rect.width/2,
                    y: rect.top + rect.height/2,
                });
            });
            return btns;
        }""")
        return {
            "scene": "unknown",
            "hp": 0,
            "score": 0,
            "level": 0,
            "game_over": False,
            "victory": False,
            "available_actions": ["click", "wait"],
            "interactables": buttons,
        }

    # ── 动作执行 ────────────────────────────────────────

    def execute(
        self,
        action_type: str,
        target: str | None = None,
        params: dict[str, Any] | None = None,
        capture_screenshots: bool = False,
    ) -> StepRecord:
        """执行一个动作，记录完整轨迹。

        这是核心方法——每调一次，逻辑步 +1，记录前后状态和结果。

        Args:
            action_type: 动作类型（click / keypress / wait / set_param 等）
            target: 目标（DOM 选择器 / 坐标等）
            params: 动作参数
            capture_screenshots: 是否记录前后截图

        Returns:
            StepRecord — 完整的步骤记录
        """
        assert self._page is not None
        params = params or {}
        started_at = time.perf_counter()

        # 复现模式下，不允许某些"破坏性"动作（比如随机导航）
        if self.mode == "reproduce":
            # 复现模式只允许白名单内的确定性动作
            pass  # 具体约束放在 action_validator 里做

        # 动作前等待页面稳定，保证前状态可观察
        self._settle()

        # 前状态
        state_before = self.read_state()
        shot_before = None
        if capture_screenshots:
            shot_before = self._screenshot_bytes()

        # 执行
        ok = True
        error: str | None = None
        try:
            self._do_execute(action_type, target, params)
        except Exception as e:
            ok = False
            error = str(e)

        # 动作后等待页面稳定，保证后状态反映真实结果
        self._settle()

        # 后状态
        state_after = self.read_state()
        shot_after = None
        if capture_screenshots:
            shot_after = self._screenshot_bytes()

        # 记录
        self._logic_step += 1
        record = StepRecord(
            logic_step=self._logic_step,
            action_type=action_type,
            action_target=target,
            action_params=params,
            state_before=state_before.raw,
            state_after=state_after.raw,
            ok=ok,
            error=error,
            screenshot_before=shot_before,
            screenshot_after=shot_after,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
            settle=self._last_settle.to_dict() if self._last_settle else {},
        )
        self._trace.append(record)
        return record

    def _do_execute(
        self,
        action_type: str,
        target: str | None,
        params: dict[str, Any],
    ) -> None:
        """实际执行动作的内部方法。

        SDK 模式：通过 SDK 的 execute() 执行（语义层）
        非 SDK 模式：用 Playwright 原生 API 操作（输入层，端到端）
        """
        assert self._page is not None

        if action_type == "wait":
            wait_ms = int(params.get("ms", 500))
            self._page.wait_for_timeout(wait_ms)
            return

        if self.sdk_mode:
            # SDK 模式：通过 JS 接口执行
            result = self._page.evaluate(
                """([type, target, params]) => {
                    return window.__playranger__.execute({
                        type: type,
                        target: target,
                        ...params
                    });
                }""",
                [action_type, target, params],
            )
            if not result or not result.get("ok"):
                error_msg = result.get("error", "unknown error") if result else "no response"
                raise RuntimeError(f"SDK 动作失败: {action_type} -> {error_msg}")
            return

        # 非 SDK 模式：原生 Playwright 操作
        if action_type == "click":
            if not target:
                raise ValueError("click 动作需要 target 选择器")
            self._page.click(target)
        elif action_type == "fill":
            if not target:
                raise ValueError("fill 动作需要 target 选择器")
            value = params.get("value", "")
            self._page.fill(target, str(value))
        elif action_type == "keypress":
            key = params.get("key", "Enter")
            self._page.keyboard.press(str(key))
        elif action_type == "goto":
            if target:
                self._page.goto(target)
        else:
            raise ValueError(f"未知动作类型: {action_type}")

    # ── 截图 ────────────────────────────────────────────

    def screenshot(self, path: str) -> str:
        """截图保存到文件，返回文件路径。"""
        assert self._page is not None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._page.screenshot(path=path)
        return path

    def _screenshot_bytes(self) -> str:
        """截图并返回 base64（存在内存里，用于小步记录）。"""
        assert self._page is not None
        # 用路径代替，省内存
        return ""  # Day3 简化，不开内存截图

    # ── 轨迹与状态 ──────────────────────────────────────

    @property
    def logic_step(self) -> int:
        return self._logic_step

    @property
    def trace(self) -> list[StepRecord]:
        return list(self._trace)

    def trace_to_list(self) -> list[dict[str, Any]]:
        """把轨迹转成可序列化的 list。"""
        result = []
        for r in self._trace:
            result.append({
                "logic_step": r.logic_step,
                "action_type": r.action_type,
                "action_target": r.action_target,
                "action_params": r.action_params,
                "state_before": r.state_before,
                "state_after": r.state_after,
                "ok": r.ok,
                "error": r.error,
                "duration_ms": r.duration_ms,
                "settle": r.settle,
                "timestamp": r.timestamp,
            })
        return result

    def reset_trace(self) -> None:
        """清空轨迹和逻辑步计数（复现模式重新开始时用）。"""
        self._logic_step = 0
        self._trace = []

    # ── SDK 工具方法 ────────────────────────────────────

    def sdk_reset(self) -> None:
        """通过 SDK 重置游戏到初始状态（需要 SDK 支持 reset）。"""
        assert self._page is not None
        if not self.sdk_mode:
            raise RuntimeError("sdk_reset 只能在 SDK 模式下使用")
        self._page.evaluate("() => window.__playranger__.reset && window.__playranger__.reset()")
        self.reset_trace()

    def sdk_set_seed(self, seed: int) -> bool:
        """设置随机种子（需要 SDK 支持）。

        Returns:
            True 表示 SDK 已接受种子；False 表示游戏未提供 setSeed。
        """
        assert self._page is not None
        if not self.sdk_mode:
            raise RuntimeError("sdk_set_seed 只能在 SDK 模式下使用")
        try:
            return bool(
                self._page.evaluate(
                    """(seed) => {
                        const sdk = window.__playranger__;
                        if (!sdk || typeof sdk.setSeed !== 'function') return false;
                        sdk.setSeed(seed);
                        return true;
                    }""",
                    seed,
                )
            )
        except Exception:
            return False

    def sdk_get_seed(self) -> int | None:
        """获取当前随机种子。"""
        assert self._page is not None
        if not self.sdk_mode:
            return None
        return self._page.evaluate(
            "() => window.__playranger__.getSeed ? window.__playranger__.getSeed() : null"
        )

    def get_capabilities(self) -> dict[str, bool]:
        """探测 SDK 能力位（可试玩/可取证/可确定性重放/可调参）。"""
        assert self._page is not None
        if not self.sdk_mode:
            return {
                "playable": False,
                "evidential": True,  # 至少能截图
                "deterministic": False,
                "tunable": False,
            }
        return self._page.evaluate("""() => {
            const sdk = window.__playranger__;
            return {
                playable: !!(sdk.observe && sdk.execute),
                evidential: !!(sdk.observe && sdk.getEvents),
                deterministic: !!(sdk.setSeed && sdk.getSeed),
                tunable: !!(sdk.setParam && sdk.getParam),
            };
        }""")

    # ── 性能采样（P1-5）──────────────────────────────────

    def _init_cdp_metrics(self) -> None:
        """建立 CDP 性能会话并记录基线；失败不影响测试主流程。"""
        if self._page is None or self._cdp_session is not None:
            return
        try:
            session = self._page.context.new_cdp_session(self._page)
            session.send("Performance.enable")
            self._cdp_session = session
            self._cdp_baseline = self._cdp_metrics()
            self._cdp_baseline_at = time.perf_counter()
        except Exception:
            self._cdp_session = None
            self._cdp_baseline = None
            self._cdp_baseline_at = None

    def _cdp_metrics(self) -> dict[str, float]:
        """读取 Performance.getMetrics，转成 name → value。"""
        assert self._cdp_session is not None
        payload = self._cdp_session.send("Performance.getMetrics")
        metrics: dict[str, float] = {}
        for item in payload.get("metrics", []) or []:
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and isinstance(value, (int, float)):
                metrics[name] = float(value)
        return metrics

    def _cdp_perf_fallback(self) -> dict | None:
        """rAF 帧样本不足时，用 CDP 窗口增量给出可解释指标。

        没有基线时现场采 0.5s 窗口，并明确标记 fps_unavailable。
        """
        if not self.perf or self._page is None:
            return None
        try:
            if self._cdp_session is None:
                self._init_cdp_metrics()
            if self._cdp_session is None:
                return None
            before = self._cdp_baseline
            started = self._cdp_baseline_at
            if before is None or started is None:
                before = self._cdp_metrics()
                started = time.perf_counter()
                time.sleep(0.5)
            after = self._cdp_metrics()
            wall_s = max(0.001, time.perf_counter() - started)
            return perf_sampler.summarize_cdp(
                before, after, wall_s, self.perf_thresholds
            ).to_dict()
        except Exception:
            return None

    def perf_sample(self) -> dict:
        """读取页面内性能采集结果并做阈值判定。

        未开启 perf 或页面未安装采集脚本时，返回 insufficient_data，
        不编造 FPS 数字；无头模式帧样本不足时自动切 CDP 兜底。
        """
        if not self.perf or self._page is None:
            return {
                "insufficient_data": True,
                "violations": ["未开启性能采样（构造时传 perf=True）"],
                "frame_count": 0,
            }
        raw = self._page.evaluate(
            """() => {
                const p = window.__PLAYRANGER_PERF__;
                if (!p) return null;
                return {
                    frames: p.frames,
                    longTasks: p.longTasks,
                    heap: p.heap,
                    started_at: p.started_at,
                    now: performance.now(),
                };
            }"""
        )
        if not raw:
            fallback = self._cdp_perf_fallback()
            if fallback is not None:
                return fallback
            return {
                "insufficient_data": True,
                "violations": ["页面未安装性能采集脚本"],
                "frame_count": 0,
            }
        sample = perf_sampler.summarize(raw, self.perf_thresholds).to_dict()
        if sample.get("insufficient_data"):
            fallback = self._cdp_perf_fallback()
            if fallback is not None and not fallback.get("insufficient_data"):
                return fallback
        return sample

    def runtime_stats(self) -> dict:
        """运行环境与稳定性统计（写进报告 / CI 日志）。"""
        return {
            "ci_mode": self.ci_mode,
            "headless": self.headless,
            "viewport": list(self.viewport),
            "retry": self.retry,
            "retry_count": self.retry_count,
            "retry_reasons": list(self.retry_reasons),
            "navigation_timeout_ms": self.navigation_timeout_ms,
            "action_timeout_ms": self.action_timeout_ms,
            "seed_mode": self.seed_mode,
            "perf_enabled": self.perf,
            "perf_fallback": "cdp_metrics" if self.perf else "none",
        }

    # ── 页面访问（给上层用）──────────────────────────────

    @property
    def page(self) -> Page:
        """直接访问 Playwright Page 对象（慎用）。"""
        assert self._page is not None
        return self._page
