"""Day 3 集成测试：PlayEngine + Demo Game + SDK 接口。

需要 Playwright + Chromium 浏览器。
如果浏览器没装，测试会自动跳过。
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

# 检查 Playwright 是否可用
playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402


# 浏览器可执行文件路径：支持用环境变量指定
# Playwright 自带的 chromium-headless-shell 没装时，可以用完整 Chrome
CHROME_EXECUTABLE = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None


DEMO_GAME_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "examples", "demo_game", "index.html",
)


def _file_url(path: str) -> str:
    return "file:///" + os.path.abspath(path).replace("\\", "/")


@pytest.fixture
def browser():
    """惰性浏览器 fixture：只有真正调用 new_page() 时才启动 Playwright。

    背景：PlayEngine 在测试内自建 sync_playwright；如果 fixture 提前启动，
    同线程嵌套会触发 Playwright 的「Sync API inside the asyncio loop」错误。
    惰性化后，只请求 fixture 但不用的测试不会产生嵌套。
    """

    state = {"pw": None, "browser": None}

    class _LazyBrowser:
        def new_page(self, *args, **kwargs):
            if state["browser"] is None:
                state["pw"] = sync_playwright().start()
                launch_kwargs = {"headless": True}
                if CHROME_EXECUTABLE:
                    launch_kwargs["executable_path"] = CHROME_EXECUTABLE
                state["browser"] = state["pw"].chromium.launch(**launch_kwargs)
            return state["browser"].new_page(*args, **kwargs)

        def close(self):
            if state["browser"] is not None:
                state["browser"].close()
                state["browser"] = None
            if state["pw"] is not None:
                state["pw"].stop()
                state["pw"] = None

    lazy = _LazyBrowser()
    yield lazy
    lazy.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.goto(_file_url(DEMO_GAME_PATH))
    page.wait_for_load_state("domcontentloaded")
    # 等 SDK 就绪
    page.wait_for_function(
        "() => typeof window.__playranger__ !== 'undefined'"
    )
    yield page
    page.close()


class TestDemoGameSDK:
    """Demo 游戏的 SDK 接口测试。"""

    def test_sdk_exists(self, page):
        """SDK 已注入。"""
        sdk = page.evaluate("() => window.__playranger__")
        assert sdk is not None
        assert sdk["sdkVersion"] == "1.0.0"

    def test_observe_initial_state(self, page):
        """初始状态正确。"""
        state = page.evaluate("() => window.__playranger__.observe()")
        assert state["scene"] == "menu"
        assert state["hp"] == 100
        assert state["score"] == 0
        assert state["level"] == 0
        assert state["victory"] is False
        assert "click" in state["available_actions"]
        assert len(state["interactables"]) > 0

    def test_execute_click_start(self, page):
        """点击开始游戏 → 进入 level1。"""
        result = page.evaluate("""() => window.__playranger__.execute({
            type: "click",
            target: "#start-btn"
        })""")
        assert result["ok"] is True

        state = page.evaluate("() => window.__playranger__.observe()")
        assert state["scene"] == "level1"
        assert state["level"] == 1
        assert state["score"] == 0
        assert state["hp"] == 100

    def test_execute_click_next_level(self, page):
        """下一关 → 进入 level2，加分。"""
        # 先到 level1
        page.evaluate("() => window.__playranger__.execute({type: 'click', target: '#start-btn'})")
        # 点下一关
        result = page.evaluate("""() => window.__playranger__.execute({
            type: "click",
            target: "#level1-next"
        })""")
        assert result["ok"] is True

        state = page.evaluate("() => window.__playranger__.observe()")
        assert state["scene"] == "level2"
        assert state["level"] == 2
        assert state["score"] == 100

    def test_bug_001_hp_not_reset(self, page):
        """BUG-001：从 level2 点重新开始，HP 不重置。"""
        # 到 level2
        page.evaluate("() => window.__playranger__.execute({type: 'click', target: '#start-btn'})")
        page.evaluate("() => window.__playranger__.execute({type: 'click', target: '#level1-next'})")

        # 先看 HP 是不是 100
        state_before = page.evaluate("() => window.__playranger__.observe()")
        assert state_before["hp"] == 100

        # 把 HP 改成 30（模拟受伤）
        page.evaluate("""() => window.__playranger__.execute({
            type: "set_param",
            param: "hp",
            value: 30
        })""")
        state_after_damage = page.evaluate("() => window.__playranger__.observe()")
        assert state_after_damage["hp"] == 30

        # 点"重新开始"
        page.evaluate("""() => window.__playranger__.execute({
            type: "click",
            target: "#level2-restart"
        })""")

        # BUG：HP 应该重置为 100，但实际上还是 30
        state_after_restart = page.evaluate("() => window.__playranger__.observe()")
        assert state_after_restart["scene"] == "level1"
        # 这就是 bug：HP 没重置。断言这里故意写成期望正确的，让测试 FAIL 证明 bug 存在
        # （测试的目标就是验证 bug 能被检测到）
        assert state_after_restart["hp"] == 30, (
            "BUG-001 不再复现：HP 已经重置为 100 了"
        )

    def test_execute_unknown_action_fails(self, page):
        """未知动作应该失败。"""
        result = page.evaluate("""() => window.__playranger__.execute({
            type: "fly_to_the_moon",
            target: "#btn"
        })""")
        assert result["ok"] is False
        assert "unknown" in result["error"]

    def test_execute_click_missing_selector_fails(self, page):
        """不存在的选择器应该失败。"""
        result = page.evaluate("""() => window.__playranger__.execute({
            type: "click",
            target: "#nonexistent-btn"
        })""")
        assert result["ok"] is False
        assert "element not found" in result["error"]

    def test_seed(self, page):
        """种子设置/获取。"""
        page.evaluate("() => window.__playranger__.setSeed(12345)")
        seed = page.evaluate("() => window.__playranger__.getSeed()")
        assert seed == 12345

    def test_reset(self, page):
        """重置回初始状态。"""
        # 先玩几步
        page.evaluate("() => window.__playranger__.execute({type: 'click', target: '#start-btn'})")
        page.evaluate("() => window.__playranger__.execute({type: 'click', target: '#level1-next'})")
        state_before = page.evaluate("() => window.__playranger__.observe()")
        assert state_before["level"] == 2

        # 重置
        page.evaluate("() => window.__playranger__.reset()")
        state_after = page.evaluate("() => window.__playranger__.observe()")
        assert state_after["scene"] == "menu"
        assert state_after["hp"] == 100
        assert state_after["score"] == 0
        assert state_after["level"] == 0

    def test_logic_step_increments(self, page):
        """逻辑步每次 +1。"""
        step_before = page.evaluate("() => window.__playranger__.observe().logic_step")
        page.evaluate("() => window.__playranger__.execute({type: 'wait'})")
        step_after = page.evaluate("() => window.__playranger__.observe().logic_step")
        assert step_after == step_before + 1

    def test_events_log(self, page):
        """事件日志记录。"""
        # 重置后事件应该是空的（或者只有 reset）
        page.evaluate("() => window.__playranger__.reset()")
        events_before = page.evaluate("() => window.__playranger__.getEvents()")

        # 做两个动作
        page.evaluate("() => window.__playranger__.execute({type: 'click', target: '#start-btn'})")
        page.evaluate("() => window.__playranger__.execute({type: 'wait', ms: 100})")

        events = page.evaluate("() => window.__playranger__.getEvents()")
        # 至少应该有 click 和 wait 两个事件
        event_types = [e["type"] for e in events]
        assert "click" in event_types
        assert "wait" in event_types


class TestPlayEngine:
    """PlayEngine 封装测试。"""

    def test_play_engine_start_and_read_state(self):
        from playranger.core.engines.play_engine import PlayEngine

        engine = PlayEngine(
            url=_file_url(DEMO_GAME_PATH),
            headless=True,
            sdk_mode=True,
        )
        engine.start()
        try:
            state = engine.read_state()
            assert state.scene == "menu"
            assert state.hp == 100
            assert "click" in state.available_actions
        finally:
            engine.close()

    def test_play_engine_context_manager(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            state = engine.read_state()
            assert state.scene == "menu"
            assert engine.logic_step == 0

    def test_play_engine_execute_records_trace(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            # 执行一个动作
            record = engine.execute("click", target="#start-btn")

            assert record.ok is True
            assert record.action_type == "click"
            assert record.action_target == "#start-btn"
            assert record.logic_step == 1
            assert engine.logic_step == 1

            # 状态应该变化了
            assert record.state_before["scene"] == "menu"
            assert record.state_after["scene"] == "level1"

            # 轨迹里应该有一条
            assert len(engine.trace) == 1
            assert engine.trace[0].logic_step == 1

    def test_play_engine_multiple_steps(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            engine.execute("click", target="#start-btn")
            engine.execute("click", target="#level1-next")
            engine.execute("wait", params={"ms": 100})

            assert engine.logic_step == 3
            assert len(engine.trace) == 3

            # 最后状态应该在 level2
            final_state = engine.read_state()
            assert final_state.scene == "level2"
            assert final_state.score == 100

    def test_play_engine_screenshot(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            with tempfile.TemporaryDirectory() as tmpdir:
                path = os.path.join(tmpdir, "test.png")
                result = engine.screenshot(path)
                assert os.path.exists(result)
                assert os.path.getsize(result) > 1000  # 至少 1KB

    def test_play_engine_failed_action(self):
        """失败的动作也应该被记录。"""
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            record = engine.execute("click", target="#nonexistent-button")

            # SDK 模式下失败的 click 应该 ok=False
            assert record.ok is False
            assert record.error is not None
            assert "element not found" in record.error
            assert record.logic_step == 1  # 失败也记一步

    def test_play_engine_reset_trace(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            engine.execute("wait")
            assert engine.logic_step == 1
            assert len(engine.trace) == 1

            engine.reset_trace()
            assert engine.logic_step == 0
            assert len(engine.trace) == 0

    def test_play_engine_trace_to_list(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            engine.execute("wait")
            trace_list = engine.trace_to_list()

            assert isinstance(trace_list, list)
            assert len(trace_list) == 1
            assert trace_list[0]["action_type"] == "wait"
            assert "state_before" in trace_list[0]
            assert "state_after" in trace_list[0]
            assert "timestamp" in trace_list[0]

    def test_play_engine_get_capabilities(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            caps = engine.get_capabilities()
            # Demo 游戏支持所有四种能力
            assert caps["playable"] is True
            assert caps["evidential"] is True
            assert caps["deterministic"] is True
            assert caps["tunable"] is True

    def test_play_engine_sdk_reset(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            # 玩几步
            engine.execute("click", target="#start-btn")
            engine.execute("click", target="#level1-next")
            state = engine.read_state()
            assert state.level == 2

            # 重置
            engine.sdk_reset()
            state_after = engine.read_state()
            assert state_after.scene == "menu"
            assert state_after.hp == 100
            assert engine.logic_step == 0  # 重置后逻辑步归零

    def test_play_engine_seed(self):
        from playranger.core.engines.play_engine import PlayEngine

        with PlayEngine(url=_file_url(DEMO_GAME_PATH)) as engine:
            engine.sdk_set_seed(999)
            assert engine.sdk_get_seed() == 999


class TestReplayer:
    """Replayer 回放器集成测试。"""

    def test_replay_once_basic(self):
        """基础重放：从轨迹重放动作，能到达相同状态。"""
        from playranger.core.engines.play_engine import PlayEngine
        from playranger.core.agent.replayer import Replayer

        def make_engine():
            return PlayEngine(
                url=_file_url(DEMO_GAME_PATH),
                headless=True,
                sdk_mode=True,
            )

        replayer = Replayer(engine_factory=make_engine)

        # 构造一个简单轨迹：开始游戏 → 下一关
        trace = [
            {"action_type": "click", "action_target": "#start-btn"},
            {"action_type": "click", "action_target": "#level1-next"},
        ]

        result = replayer.replay_once(action_trace=trace)

        assert result.success is True  # 无断言时默认成功
        assert result.steps_executed == 2
        assert result.final_state["scene"] == "level2"
        assert result.final_state["score"] == 100
        assert result.error is None

    def test_replay_with_assertion_pass(self):
        """带断言的重放：断言通过则 success=True。"""
        from playranger.core.engines.play_engine import PlayEngine
        from playranger.core.agent.replayer import Replayer

        def make_engine():
            return PlayEngine(url=_file_url(DEMO_GAME_PATH), headless=True)

        replayer = Replayer(engine_factory=make_engine)

        trace = [
            {"action_type": "click", "action_target": "#start-btn"},
        ]

        # 断言：到 level1 且 HP=100
        assertion = {
            "state_equals": {"scene": "level1", "hp": 100},
        }

        result = replayer.replay_once(
            action_trace=trace,
            assertion=assertion,
        )

        assert result.success is True
        assert result.final_state["scene"] == "level1"

    def test_replay_with_assertion_fail(self):
        """带断言的重放：断言不通过则 success=False。"""
        from playranger.core.engines.play_engine import PlayEngine
        from playranger.core.agent.replayer import Replayer

        def make_engine():
            return PlayEngine(url=_file_url(DEMO_GAME_PATH), headless=True)

        replayer = Replayer(engine_factory=make_engine)

        trace = [
            {"action_type": "click", "action_target": "#start-btn"},
        ]

        # 错误断言：期望 score=999
        assertion = {
            "state_equals": {"score": 999},
        }

        result = replayer.replay_once(
            action_trace=trace,
            assertion=assertion,
        )

        assert result.success is False

    def test_replay_bug_001_detection(self):
        """复现 BUG-001：从 level2 点重新开始，HP 不重置。"""
        from playranger.core.engines.play_engine import PlayEngine
        from playranger.core.agent.replayer import Replayer, build_assertion_from_dict

        def make_engine():
            return PlayEngine(url=_file_url(DEMO_GAME_PATH), headless=True)

        replayer = Replayer(engine_factory=make_engine)

        # BUG-001 触发轨迹：
        # 1. 开始游戏
        # 2. 下一关到 level2
        # 3. 把 HP 改成 30
        # 4. 点"重新开始"
        # 期望（bug 存在时）：HP 还是 30，不是 100
        trace = [
            {"action_type": "click", "action_target": "#start-btn"},
            {"action_type": "click", "action_target": "#level1-next"},
            {
                "action_type": "set_param",
                "action_params": {"param": "hp", "value": 30},
            },
            {"action_type": "click", "action_target": "#level2-restart"},
        ]

        # 验证 bug 的断言：HP 应该是 30（bug 存在时）
        # 注意：这里我们断言 "hp == 30" 来证明 bug 存在
        # 如果将来 bug 被修了，这个测试会失败
        assertion_bug_exists = {"state_equals": {"hp": 30, "scene": "level1"}}

        result = replayer.replay_once(
            action_trace=trace,
            assertion=assertion_bug_exists,
        )

        # BUG-001 应该能被复现
        assert result.success is True, (
            "BUG-001 未能复现：HP 不是 30，可能 bug 已被修复"
        )
        assert result.final_state["hp"] == 30
        assert result.final_state["scene"] == "level1"

    def test_replay_multiple_with_seed(self, browser):
        """多次重放 + 固定种子：每次结果应该一致。"""
        from playranger.core.engines.play_engine import PlayEngine
        from playranger.core.agent.replayer import Replayer

        def make_engine():
            return PlayEngine(url=_file_url(DEMO_GAME_PATH), headless=True)

        replayer = Replayer(engine_factory=make_engine)

        trace = [
            {"action_type": "click", "action_target": "#start-btn"},
            {"action_type": "click", "action_target": "#level1-next"},
        ]

        assertion = {"state_equals": {"scene": "level2", "level": 2}}

        report = replayer.replay_multiple(
            action_trace=trace,
            num_runs=3,
            assertion=assertion,
            seed=42,
            bug_id="test-determinism",
        )

        assert report.total_runs == 3
        assert report.success_runs == 3
        assert report.reproduction_rate == 1.0
        assert report.is_reproducible is True
        assert report.bug_id == "test-determinism"

    def test_build_assertion_numeric_comparisons(self):
        """断言函数：数值比较类。"""
        from playranger.core.agent.replayer import build_assertion_from_dict

        state = {"hp": 50, "score": 200}

        # less than
        fn = build_assertion_from_dict({"state_less_than": {"hp": 100}})
        ok, _ = fn(state)
        assert ok is True

        fn = build_assertion_from_dict({"state_less_than": {"hp": 50}})
        ok, _ = fn(state)
        assert ok is False  # 50 >= 50 不满足 <

        # greater than
        fn = build_assertion_from_dict({"state_greater_than": {"score": 100}})
        ok, _ = fn(state)
        assert ok is True

        fn = build_assertion_from_dict({"state_greater_than": {"score": 200}})
        ok, _ = fn(state)
        assert ok is False  # 200 <= 200 不满足 >
