"""C3 LLM 客户端 —— 五模型统一调用接口。

每个模型节点通过此客户端调用对应的 LLM。
使用 langchain-openai 兼容接口（阿里百炼 DashScope 兼容 OpenAI API）。
"""

from __future__ import annotations

import json
from typing import Any

from food_agent_v2.core.config import load_config


class ModelInvocationError(Exception):
    """模型调用确定性失败（T17：不模板回答，fail-closed 交由 runner 转 MODEL_CALL_FAILED）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LLMClient:
    """五模型 LLM 调用客户端。

    角色 → 模型分配：
    - query_understanding     → LLM_MODEL_REASONING (qwen3.7-max)
    - health_menu_planning    → LLM_MODEL_REASONING
    - menu_decision           → LLM_MODEL_REASONING
    - answer_generation       → LLM_MODEL_ANSWER (qwen3.7-plus)
    - unified_review          → LLM_MODEL_REASONING
    """

    def __init__(self):
        cfg = load_config()
        self._api_key = cfg.llm.api_key
        self._base_url = cfg.llm.base_url
        self._timeout = cfg.llm.timeout_seconds
        self._max_retries = cfg.llm.max_retries
        self._llm_config = cfg.llm
        # P2：进程生命周期复用 OpenAI client（内部 httpx 连接池 keep-alive），
        # 避免每次 _call_openai 新建 client + 连接池。惰性创建，测试注入 FakeLLM 不触发。
        self._openai = None

    def invoke(
        self,
        role: str,
        system_prompt: str,
        user_message: str,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        """调用 LLM，返回结构化响应。

        Args:
            role: 模型角色名
            system_prompt: 系统提示词（含角色策略、工具说明、Schema）
            user_message: 用户消息（含 ModelContext 投影）
            tools: OpenAI 格式的工具定义列表
            response_format: JSON Schema 格式的响应结构
            timeout_seconds: 单次请求超时 override（None 用默认 self._timeout）

        Returns:
            {"content": str, "tool_calls": list, "usage": dict}
        """
        model = self._llm_config.model_for_role(role)

        if not self._api_key or not self._base_url:
            # T17：无模型配置即确定性失败，绝不返回模板/mock 响应
            raise ModelInvocationError(
                "MODEL_NOT_CONFIGURED", "缺少 LLM API 配置（api_key/base_url）")

        return self._call_openai(
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            tools=tools,
            response_format=response_format,
            extra_body=self._llm_config.extra_body_for_role(role),
            timeout_seconds=timeout_seconds,
        )

    def invoke_messages(
        self,
        role: str,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        response_format: dict | None = None,
    ) -> dict:
        """继续一次标准 tool-calling 会话，保留 assistant/tool 对应关系。"""
        model = self._llm_config.model_for_role(role)
        if not self._api_key or not self._base_url:
            raise ModelInvocationError(
                "MODEL_NOT_CONFIGURED", "缺少 LLM API 配置（api_key/base_url）")
        return self._call_openai(
            model=model,
            system_prompt=system_prompt,
            user_message=None,
            messages=messages,
            tools=tools,
            response_format=response_format,
            extra_body=self._llm_config.extra_body_for_role(role),
        )

    def _call_openai(
        self, model: str, system_prompt: str, user_message: str | None,
        tools: list[dict] | None, response_format: dict | None,
        extra_body: dict | None,
        messages: list[dict[str, Any]] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        """通过 OpenAI 兼容 API 调用。"""
        import time as _time

        from openai import OpenAI

        if self._openai is None:
            self._openai = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=0,  # T17：SDK 不自动重试，失败交给 runner fail-closed
            )
        client = self._openai

        request_messages = [{"role": "system", "content": system_prompt}]
        if messages is not None:
            request_messages.extend(messages)
        else:
            request_messages.append({"role": "user", "content": user_message or ""})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": request_messages,
            "temperature": 0.1,
        }

        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format
        if extra_body:
            kwargs["extra_body"] = extra_body
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds

        start = _time.perf_counter()
        response = client.chat.completions.create(**kwargs)
        elapsed = _time.perf_counter() - start

        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments)
                    if tc.function.arguments else {},
                }
                for tc in (choice.message.tool_calls or [])
            ],
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                "elapsed_ms": round(elapsed * 1000),
                "model": model,
            },
        }

# 全局单例
_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
