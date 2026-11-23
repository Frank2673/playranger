"""PlayRanger 远程客户端 SDK。

通过 HTTP API 与 PlayRanger 服务端通信。
适合多语言、分布式、服务端部署的场景。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

from playranger.sdk.models import (
    BugDetail,
    BugList,
    Capabilities,
    Health,
    ReplayResult,
    StepList,
    TestInfo,
)


class PlayRangerClient:
    """PlayRanger 远程 HTTP 客户端。

    使用示例::

        from playranger.sdk import PlayRangerClient

        client = PlayRangerClient("http://localhost:8000")

        # 健康检查
        health = client.health()
        print(health.status, health.version)

        # 启动测试
        test = client.start_test(
            game_url="http://example.com/game.html",
            goal="找到所有可复现的缺陷",
            max_steps=50,
        )
        print(test.test_id, test.status)

        # 等待完成
        test = client.wait_for_test(test.test_id)
        print(f"发现 {test.bugs_found} 个 bug")

        # 列出 bug
        bugs = client.list_bugs()
        for bug in bugs.bugs:
            print(bug.bug_id, bug.title)

        # 复现 bug
        result = client.replay_bug(bug.bug_id, runs=5)
        print(f"复现率: {result.rate_pct}")
    """

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0):
        """
        Args:
            base_url: 服务端地址，如 http://localhost:8000
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._session: Any = None

    def _get_session(self) -> Any:
        """懒加载 requests session。"""
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _get(self, path: str, params: dict | None = None) -> dict:
        sess = self._get_session()
        resp = sess.get(self._url(path), params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict | None = None) -> dict:
        sess = self._get_session()
        resp = sess.post(self._url(path), json=json, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ─── 健康检查 ──────────────────────────────────────────────

    def health(self) -> Health:
        """健康检查。"""
        data = self._get("/health")
        return Health(status=data["status"], version=data["version"])

    # ─── 系统能力 ──────────────────────────────────────────────

    def get_capabilities(self) -> Capabilities:
        """获取系统能力信息。"""
        data = self._get("/api/v1/capabilities")
        return Capabilities.from_dict(data)

    # ─── 测试接口 ──────────────────────────────────────────────

    def start_test(
        self,
        game_url: str,
        goal: str = "找到所有可复现的缺陷",
        max_steps: int = 50,
        mode: str = "auto",
        seed: int | None = None,
    ) -> TestInfo:
        """启动一次自动测试。

        Args:
            game_url: 游戏页面 URL
            goal: 测试目标
            max_steps: 最大步数
            mode: 测试模式（auto / explore / verify）
            seed: 随机种子（可选）

        Returns:
            测试信息
        """
        payload: dict[str, Any] = {
            "game_url": game_url,
            "goal": goal,
            "max_steps": max_steps,
            "mode": mode,
        }
        if seed is not None:
            payload["seed"] = seed

        data = self._post("/api/v1/tests/start", json=payload)
        return TestInfo.from_dict(data)

    def get_test(self, test_id: str) -> TestInfo:
        """查询测试状态和结果。"""
        data = self._get(f"/api/v1/tests/{test_id}")
        return TestInfo.from_dict(data)

    def get_test_steps(
        self,
        test_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> StepList:
        """获取测试的步骤轨迹。"""
        data = self._get(
            f"/api/v1/tests/{test_id}/steps",
            params={"limit": limit, "offset": offset},
        )
        return StepList.from_dict(data)

    def wait_for_test(
        self,
        test_id: str,
        poll_interval: float = 1.0,
        timeout: float | None = None,
    ) -> TestInfo:
        """等待测试完成。

        Args:
            test_id: 测试 ID
            poll_interval: 轮询间隔（秒）
            timeout: 超时时间（秒），None 表示不超时

        Returns:
            最终测试信息

        Raises:
            TimeoutError: 超过超时时间仍未完成
        """
        start = time.time()
        while True:
            test = self.get_test(test_id)
            if test.is_done:
                return test
            if timeout is not None and (time.time() - start) > timeout:
                raise TimeoutError(f"test {test_id} timed out after {timeout}s")
            time.sleep(poll_interval)

    # ─── Bug 接口 ──────────────────────────────────────────────

    def list_bugs(self, limit: int = 50, offset: int = 0) -> BugList:
        """列出所有已发现的 bug。"""
        data = self._get("/api/v1/bugs", params={"limit": limit, "offset": offset})
        return BugList.from_dict(data)

    def get_bug(self, bug_id: str, include_trace: bool = False) -> BugDetail:
        """查看单个 bug 详情。"""
        data = self._get(
            f"/api/v1/bugs/{bug_id}",
            params={"include_trace": "true" if include_trace else "false"},
        )
        return BugDetail.from_dict(data)

    # ─── 复现接口 ──────────────────────────────────────────────

    def replay_bug(self, bug_id: str, runs: int = 3) -> ReplayResult:
        """复现指定 bug。

        Args:
            bug_id: Bug ID
            runs: 复现次数（1-20）

        Returns:
            复现结果
        """
        data = self._post(f"/api/v1/replay/{bug_id}", json={"runs": runs})
        return ReplayResult.from_dict(data)
