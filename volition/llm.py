from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable

from .config import LLMConfig
from .tools import ToolRegistry
from .utils import extract_json_object


class OpenAICompatLLM:
    def __init__(self, config: LLMConfig):
        self.config = config

    def available(self) -> bool:
        return bool(self.config.api_key and self.config.model and self.config.api_base)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.config.api_base.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _message_text(self, message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(parts).strip()
        return str(content or "").strip()

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        data = self._request(payload)
        choice = (data.get("choices") or [{}])[0]
        return choice.get("message") or {}

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        message = self.complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return self._message_text(message)

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        base_temp = self.config.temperature if temperature is None else temperature
        content = self.chat(system_prompt, user_prompt, temperature=temperature, max_tokens=max_tokens)
        data = extract_json_object(content)
        if data is not None:
            return data

        retry_prompt = (
            f"{user_prompt}\n\n"
            "重要：你的回复必须是一个单独的、可被 json.loads 解析的 JSON 对象。"
            "不要输出解释、代码块、前后缀或任何额外文本。"
        )
        content_retry = self.chat(
            system_prompt,
            retry_prompt,
            temperature=min(base_temp, 0.2),
            max_tokens=max_tokens,
        )
        data = extract_json_object(content_retry)
        if data is not None:
            return data

        final_content = content_retry or content
        if str(final_content).strip():
            repaired = self.chat(
                "你是一个 JSON 修复器。你只负责把给定内容整理成唯一一个有效 JSON 对象。",
                f"""
原始任务说明：
{user_prompt}

模型第一次回复：
{content}

模型第二次回复：
{content_retry}

请只输出一个有效 JSON 对象，严格满足原始任务说明，不要输出任何额外文本。
""",
                temperature=0.0,
                max_tokens=max_tokens,
            )
            data = extract_json_object(repaired)
            if data is not None:
                return data
            final_content = repaired or final_content

        raise RuntimeError(f"LLM 没有返回可解析的 JSON：{final_content}")

    def tool_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        registry: ToolRegistry,
        allowed_tools: list[str],
        on_tool_call: Callable[[str, dict[str, Any], Any], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_rounds: int = 6,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        specs = registry.specs(allowed_tools)
        for _ in range(max(1, int(max_rounds))):
            message = self.complete(
                messages,
                tools=specs,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
            tool_calls = message.get("tool_calls") or []
            assistant_entry: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)
            if not tool_calls:
                return self._message_text(message)
            for call in tool_calls:
                function_data = call.get("function") or {}
                tool_name = str(function_data.get("name") or "").strip()
                raw_args = function_data.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = {}
                result = registry.execute(tool_name, args)
                if on_tool_call is not None:
                    try:
                        on_tool_call(tool_name, args, result)
                    except Exception:
                        pass
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "name": tool_name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise RuntimeError("LLM 工具调用轮数超出上限，仍未给出最终答案。")

    def tool_chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        registry: ToolRegistry,
        allowed_tools: list[str],
        on_tool_call: Callable[[str, dict[str, Any], Any], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_rounds: int = 6,
    ) -> dict[str, Any]:
        content = self.tool_chat(
            system_prompt,
            user_prompt,
            registry=registry,
            allowed_tools=allowed_tools,
            on_tool_call=on_tool_call,
            temperature=temperature,
            max_tokens=max_tokens,
            max_rounds=max_rounds,
        )
        data = extract_json_object(content)
        if data is None:
            repaired = self.chat(
                "你是一个 JSON 修复器。你只负责把给定内容整理成唯一一个有效 JSON 对象。",
                f"""
原始任务说明：
{user_prompt}

模型最终回复：
{content}

请只输出一个有效 JSON 对象，严格满足原始任务说明，不要输出任何额外文本。
""",
                temperature=0.0,
                max_tokens=max_tokens,
            )
            data = extract_json_object(repaired)
            if data is None:
                raise RuntimeError(f"LLM 没有返回可解析的 JSON：{repaired or content}")
        return data
