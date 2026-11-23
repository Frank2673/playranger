"""VLM 视觉模型客户端 —— 对接支持 vision 的 OpenAI 兼容接口。

支持：
- 单图分析（inspect）
- 多图对比（compare）
- pydantic 结构化输出
- 指数退避重试

和 LLMClient 设计一致，方便 brain_factory 统一管理。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx


def _image_to_base64(image_path: str) -> str:
    """把图片文件转为 base64 data URL。"""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"图片不存在: {image_path}")
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "jpg":
        suffix = "jpeg"
    if suffix not in ("png", "jpeg", "gif", "webp"):
        suffix = "png"  # 默认按 png 处理
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/{suffix};base64,{b64}"


class VLMClient:
    """视觉语言模型客户端 —— OpenAI 兼容接口。

    用法：
        vlm = VLMClient(base_url="https://.../v1", api_key="sk-...", model="qwen-vl-max")
        result = vlm.inspect("screenshot.png", "这张图里有没有按钮？")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "sk-placeholder",
        model: str = "qwen-vl-max-latest",
        timeout: int = 60,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout)

    # ─── 公开 API ───────────────────────────────────────────────

    def inspect(self, image_path: str, prompt: str) -> str:
        """分析单张图片，返回文本回答。

        Args:
            image_path: 本地图片路径
            prompt: 提问内容

        Returns:
            VLM 的文本回答
        """
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": _image_to_base64(image_path)}},
                ],
            }
        ]
        return self._chat(messages)

    def inspect_json(self, image_path: str, prompt: str) -> dict:
        """分析单张图片，返回 JSON 格式回答。

        用 response_format=json_object 强制输出 JSON。
        """
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt + " 请用 JSON 格式回答。"},
                    {"type": "image_url", "image_url": {"url": _image_to_base64(image_path)}},
                ],
            }
        ]
        text = self._chat(messages, response_format={"type": "json_object"})
        return self._parse_json(text)

    def compare(self, image_a: str, image_b: str, prompt: str) -> str:
        """对比两张图片，返回文本回答。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt + "\n第一张图："},
                    {"type": "image_url", "image_url": {"url": _image_to_base64(image_a)}},
                    {"type": "text", "text": "第二张图："},
                    {"type": "image_url", "image_url": {"url": _image_to_base64(image_b)}},
                ],
            }
        ]
        return self._chat(messages)

    def compare_json(self, image_a: str, image_b: str, prompt: str) -> dict:
        """对比两张图片，返回 JSON。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt + "\n请用 JSON 格式回答。\n第一张图："},
                    {"type": "image_url", "image_url": {"url": _image_to_base64(image_a)}},
                    {"type": "text", "text": "第二张图："},
                    {"type": "image_url", "image_url": {"url": _image_to_base64(image_b)}},
                ],
            }
        ]
        text = self._chat(messages, response_format={"type": "json_object"})
        return self._parse_json(text)

    def health_check(self) -> tuple[bool, str]:
        """快速健康检查 —— 发一个极简请求看接口是否通。

        优先用 GET /models（OpenAI 标准），如果失败再用极简 chat 请求兜底。
        """
        try:
            resp = self._client.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code == 200:
                return True, "models endpoint OK"
            if resp.status_code == 401:
                return False, "401 Unauthorized — 检查 API Key"
            if resp.status_code in (404, 405):
                # /models 不支持，退回 /chat/completions 试一下
                return self._chat_health_check()
            return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as e:
            # 连接异常
            return False, f"连接失败: {e}"

    def close(self) -> None:
        """关闭 HTTP 客户端。"""
        self._client.close()

    # ─── 内部方法 ───────────────────────────────────────────────

    def _chat_health_check(self) -> tuple[bool, str]:
        """用一次极简 chat 做健康检查。"""
        try:
            messages = [{"role": "user", "content": "hi"}]
            self._chat(messages, max_tokens=5)
            return True, "chat endpoint OK"
        except Exception as e:
            return False, str(e)

    def _chat(
        self,
        messages: list[dict],
        response_format: dict | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """底层 chat 调用 + 指数退避重试。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if response_format:
            payload["response_format"] = response_format
        if max_tokens:
            payload["max_tokens"] = max_tokens

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                raise
            except (KeyError, IndexError) as e:
                # 响应格式不对
                last_error = ValueError(f"响应格式错误: {e}")
                raise

        # 不会走到这里，仅供类型检查
        if last_error:
            raise last_error
        raise RuntimeError("unknown error")

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从模型输出中提取 JSON —— 兼容带 ```json``` 标记和纯 JSON。"""
        text = text.strip()
        # 去掉可能的 ```json ... ``` 包裹
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试找第一个 { 和最后一个 }
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise
