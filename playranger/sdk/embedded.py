"""PlayRanger 嵌入式 SDK。

直接在本地使用 PlayRanger 核心模块，无需启动 API 服务。
适合脚本化、快速原型、集成到 CI/CD 的场景。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EmbeddedTestResult:
    """嵌入式测试结果。"""
    success: bool
    total_steps: int
    bugs_found: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    conclusion: str = "human_required"
    error: str | None = None

    @property
    def bug_count(self) -> int:
        return len(self.bugs_found)


class PlayRanger:
    """PlayRanger 本地嵌入式 SDK。

    直接调用核心模块，无需启动独立服务。

    使用示例::

        from playranger.sdk import PlayRanger

        pr = PlayRanger()

        # 查看系统能力
        caps = pr.get_capabilities()
        print(f"浏览器可用: {caps.browser_available}")
        print(f"VLM 可用: {caps.vlm_available}")

        # 运行测试
        result = pr.run_test(
            game_url="http://example.com/game.html",
            goal="找到所有可复现的缺陷",
            max_steps=50,
        )
        print(f"步数: {result.total_steps}")
        print(f"发现 bug: {result.bug_count}")

        # 列出 bug
        for bug in result.bugs_found:
            print(bug["bug_id"], bug["title"])
    """

    def __init__(self, output_dir: str | None = None):
        """
        Args:
            output_dir: Bug 输出目录，默认 output/bugs
        """
        self.output_dir = output_dir or os.environ.get(
            "PLAYRANGER_OUTPUT_DIR", "output/bugs"
        )
        self._bugs: list[dict] = []

    # ─── 系统能力 ──────────────────────────────────────────────

    def get_capabilities(self) -> Any:
        """获取系统能力信息。

        Returns:
            SystemCapabilities 对象
        """
        from playranger.core.capabilities import detect_capabilities
        return detect_capabilities()

    # ─── 测试运行 ──────────────────────────────────────────────

    def run_test(
        self,
        game_url: str,
        goal: str = "找到所有可复现的缺陷",
        max_steps: int = 50,
        seed: int | None = None,
        headless: bool = True,
    ) -> EmbeddedTestResult:
        """运行一次自动测试。

        Args:
            game_url: 游戏页面 URL
            goal: 测试目标
            max_steps: 最大步数
            seed: 随机种子
            headless: 是否无头模式

        Returns:
            测试结果
        """
        engine = None
        try:
            # 创建组件
            from playranger.core.brain.brain_factory import create_brain
            from playranger.core.agent.react_loop import ReActLoop
            from playranger.core.engines.action_validator import ActionValidator
            from playranger.core.agent.recorder import BugRecorder

            os.makedirs(self.output_dir, exist_ok=True)

            planner = create_brain()
            validator = ActionValidator()
            recorder = BugRecorder(output_dir=self.output_dir)

            # 创建引擎
            from playranger.core.engines.play_engine import PlayEngine
            engine = PlayEngine(url=game_url, headless=headless)

            if seed is not None:
                try:
                    engine.sdk_set_seed(seed)
                except Exception:
                    pass

            # 运行
            loop = ReActLoop(
                play_engine=engine,
                planner=planner,
                validator=validator,
                recorder=recorder,
                max_l1_retries=2,
                max_l2_retries=3,
                max_l3_retries=2,
            )

            result = loop.run(goal=goal, max_steps=max_steps)
            d = result.to_dict()

            bugs = d.get("bugs_found", [])
            self._bugs.extend(bugs)

            return EmbeddedTestResult(
                success=d.get("success", False),
                total_steps=d.get("total_steps", 0),
                bugs_found=bugs,
                history=d.get("history", []),
                conclusion=d.get("conclusion", "human_required"),
            )

        except Exception as e:
            return EmbeddedTestResult(
                success=False,
                total_steps=0,
                bugs_found=[],
                history=[],
                conclusion="human_required",
                error=str(e),
            )
        finally:
            if engine is not None:
                try:
                    engine.close()
                except Exception:
                    pass

    # ─── Bug 复现 ──────────────────────────────────────────────

    def replay_bug(
        self,
        bug_dir: str,
        game_url: str,
        runs: int = 3,
        headless: bool = True,
    ) -> Any:
        """从 bug 目录复现。

        Args:
            bug_dir: Bug 输出目录
            game_url: 游戏 URL
            runs: 复现次数
            headless: 是否无头模式

        Returns:
            ReproductionReport 对象
        """
        import os
        from playranger.core.agent.replayer import Replayer

        trace_file = os.path.join(bug_dir, "action_trace.jsonl")
        assertion_file = os.path.join(bug_dir, "assertion.json")

        if not os.path.exists(trace_file):
            raise FileNotFoundError(f"trace file not found: {trace_file}")

        def make_engine():
            from playranger.core.engines.play_engine import PlayEngine
            return PlayEngine(url=game_url, headless=headless)

        replayer = Replayer(engine_factory=make_engine)
        return replayer.replay_from_file(
            trace_file=trace_file,
            assertion_file=assertion_file if os.path.exists(assertion_file) else None,
            num_runs=runs,
        )

    # ─── 便捷方法 ──────────────────────────────────────────────

    def quick_test(
        self,
        game_url: str,
        goal: str = "快速检查常见缺陷",
        max_steps: int = 20,
    ) -> str:
        """快速测试并返回简要结果字符串。

        用于快速验证或命令行输出。
        """
        result = self.run_test(game_url=game_url, goal=goal, max_steps=max_steps)
        if result.error:
            return f"❌ 测试失败: {result.error}"

        lines = [
            f"✅ 测试完成（{result.total_steps} 步）",
            f"   结论: {result.conclusion}",
            f"   发现 {result.bug_count} 个缺陷:",
        ]
        for bug in result.bugs_found:
            lines.append(f"   - [{bug.get('severity', '?')}] {bug.get('title', 'unknown')}")

        return "\n".join(lines)
