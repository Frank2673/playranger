"""LLM 客户端 —— 封装 OpenAI 兼容接口，支持强制 JSON、重试和超时。

支持任何 OpenAI 兼容的推理服务：
- DGX Spark 上的 SGLang（本地推理，首选）
- 魔搭 API（兜底）
- 任何其他 /v1 兼容端点

Day 2 实现：真实 HTTP 请求 + JSON Schema 强制输出 + 指数退避重试。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError


class LLMError(Exception):
    """LLM 调用失败的统一异常。"""


class LLMClient:
    """大语言模型客户端。

    对接任何 OpenAI 兼容的推理服务。
    支持：普通聊天 / JSON 模式 / JSON Schema 强制输出 / 指数退避重试。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "sk-placeholder",
        model: str = "qwen3-8b",
        timeout: int = 30,
        max_retries: int = 2,
        temperature: float = 0.7,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self._client = httpx.Client(timeout=timeout)

    # ── 公共 API ───────────────────────────────────────────────

    def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """普通文本聊天，返回字符串内容。

        Raises:
            LLMError: 重试耗尽后仍然失败
        """
        payload = self._build_payload(messages, **kwargs)
        data = self._request_with_retry(payload)
        return self._extract_text(data)

    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """强制 JSON 模式输出，返回解析后的 dict。

        Args:
            messages: 对话消息列表
            schema: 可选。传 dict 或 pydantic BaseModel 子类，
                    会把 JSON Schema 注入到 system prompt 中引导模型输出。

        Raises:
            LLMError: 请求失败或 JSON 解析失败（重试耗尽后）
        """
        # pydantic 模型 → JSON Schema
        schema_dict: dict[str, Any] | None = None
        if schema is not None and isinstance(schema, type) and issubclass(schema, BaseModel):
            schema_dict = schema.model_json_schema()
        elif isinstance(schema, dict):
            schema_dict = schema

        # 把 schema 注入到 system prompt（兼容不支持 response_format.schema 的平台）
        if schema_dict is not None:
            schema_text = json.dumps(schema_dict, ensure_ascii=False, indent=2)
            schema_instruction = (
                f"你必须严格按照以下 JSON Schema 输出，不要加任何多余文字：\n"
                f"```json\n{schema_text}\n```\n"
                f"只输出 JSON 对象，不要有任何解释、代码块标记或额外内容。"
            )
            # 在第一条 system 消息后面追加 schema 要求
            new_messages = list(messages)
            if new_messages and new_messages[0].get("role") == "system":
                new_messages[0] = {
                    "role": "system",
                    "content": new_messages[0]["content"] + "\n\n" + schema_instruction,
                }
            else:
                new_messages.insert(0, {"role": "system", "content": schema_instruction})
            messages = new_messages

        payload = self._build_payload(messages, response_format="json", **kwargs)
        # response_format 只用 json_object（最大兼容性，DeepSeek/通义/智谱都支持）
        # 不使用 schema 字段（部分平台不支持）
        payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                data = self._request_with_retry(payload, _skip_retry=True)
                text = self._extract_text(data)
                # 清理可能的代码块标记（```json ... ```）
                text = text.strip()
                if text.startswith("```json"):
                    text = text[7:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
                result = json.loads(text)
                if not isinstance(result, dict):
                    raise ValueError(f"返回值不是 object: {type(result)}")
                return result
            except (json.JSONDecodeError, ValueError) as e:
                last_error = e
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break

        raise LLMError(f"JSON 解析失败（重试 {self.max_retries} 次）: {last_error}")

    def chat_pydantic(
        self,
        messages: list[dict[str, str]],
        model_cls: type[BaseModel],
        **kwargs: Any,
    ) -> BaseModel:
        """输出强制符合 pydantic 模型，返回模型实例。

        比 chat_json 多一层 pydantic 校验，失败自动重试。
        """
        schema = model_cls.model_json_schema()
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                data = self.chat_json(messages, schema=schema, **kwargs)
                return model_cls.model_validate(data)
            except ValidationError as e:
                last_error = e
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break

        raise LLMError(f"pydantic 校验失败（重试 {self.max_retries} 次）: {last_error}")

    def health_check(self) -> tuple[bool, str]:
        """快速健康检查：发一条最短消息看服务是否可达。

        Returns:
            (ok, 描述信息)
        """
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0,
            }
            self._request_with_retry(payload, _skip_retry=True)
            return True, "ok"
        except Exception as e:
            return False, str(e)

    def close(self) -> None:
        self._client.close()

    # ── 内部方法 ───────────────────────────────────────────────

    def _build_payload(
        self,
        messages: list[dict[str, str]],
        response_format: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _request_with_retry(
        self,
        payload: dict[str, Any],
        _skip_retry: bool = False,
    ) -> dict[str, Any]:
        """发请求 + 指数退避重试。

        只对 5xx / 网络错误 / 429 重试；4xx 错误直接抛。
        """
        retries = 0 if _skip_retry else self.max_retries
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                if resp.status_code >= 400:
                    raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return resp.json()
            except (httpx.HTTPError, httpx.TransportError) as e:
                last_error = e
                if attempt < retries:
                    time.sleep(1.0 * (2 ** attempt))  # 1s, 2s, 4s...
                    continue
                break

        raise LLMError(f"请求失败（重试 {retries} 次）: {last_error}")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"响应格式异常: {e}")
