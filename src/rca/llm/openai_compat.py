"""Client for any OpenAI-compatible /chat/completions endpoint (OpenRouter, OpenAI, Azure,
Together, vLLM, Ollama...).  Configured purely from environment variables:

    LLM_BASE_URL   default https://openrouter.ai/api/v1
    LLM_API_KEY
    LLM_MODEL      default model;  LLM_MODEL_<ROLE> overrides per agent role
    LLM_TEMPERATURE (optional; omitted when unset because some models reject it)
    LLM_API_VERSION (optional; Azure OpenAI `api-version` query param)
"""
from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any

import httpx

from .base import LLMError, LLMResponse, ToolCall

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenAICompatClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None,
                 max_tokens: int = 8000, timeout_s: float = 120.0, max_retries: int = 3, concurrency: int = 16):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or ""
        self.model = model or os.environ.get("LLM_MODEL") or "anthropic/claude-sonnet-4.5"
        if not self.api_key:
            raise LLMError("LLM_API_KEY is not set (copy .env.example to .env), or run with --llm fake")
        self.max_tokens, self.max_retries = max_tokens, max_retries
        azure = "azure.com" in self.base_url
        headers = {"api-key": self.api_key} if azure else {"Authorization": f"Bearer {self.api_key}"}
        self._params = {"api-version": os.environ["LLM_API_VERSION"]} if os.environ.get("LLM_API_VERSION") else None
        self._http = httpx.AsyncClient(timeout=timeout_s, headers=headers)
        self._sem = asyncio.Semaphore(concurrency)
        self.retries = 0

    def model_for(self, role: str) -> str:
        return os.environ.get(f"LLM_MODEL_{role.upper()}", self.model) if role else self.model

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *,
                       role: str = "", meta: dict[str, Any] | None = None) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model_for(role), "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"      # forced tool_choice is rejected by several current models
        if os.environ.get("LLM_TEMPERATURE"):
            body["temperature"] = float(os.environ["LLM_TEMPERATURE"])
        last_err = ""
        for attempt in range(self.max_retries + 1):
            try:
                async with self._sem:
                    r = await self._http.post(f"{self.base_url}/chat/completions", json=body, params=self._params)
                if r.status_code in RETRY_STATUS:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                elif r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:400]}")       # non-retryable
                else:
                    data = r.json()
                    if data.get("error"):                                          # OpenRouter can 200 with an error body
                        last_err = f"provider error: {str(data['error'])[:200]}"
                    elif not data.get("choices"):
                        last_err = "empty choices"
                    else:
                        return self._parse(data, body["model"])
            except (httpx.TimeoutException, httpx.TransportError) as ex:
                last_err = f"{type(ex).__name__}: {ex}"
            except (ValueError, KeyError, TypeError, IndexError, AttributeError) as ex:
                # 2xx with a non-JSON or malformed body (gateway HTML page, choice without a message):
                # must surface as LLMError so agents recover instead of the investigation aborting.
                last_err = f"malformed response ({type(ex).__name__}: {str(ex)[:160]})"
            self.retries += 1
            if os.environ.get("LLM_DEBUG"):
                print(f"[llm] retry {attempt + 1} ({role}): {last_err[:120]}", flush=True)
            if attempt < self.max_retries:
                await asyncio.sleep(min(20, 1.5 * 2 ** attempt) + random.random())
        raise LLMError(f"LLM call failed after {self.max_retries + 1} attempts: {last_err}")

    @staticmethod
    def _parse(data: dict[str, Any], model: str) -> LLMResponse:
        msg = data["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args, err = (json.loads(raw) if isinstance(raw, str) else raw), None
                if not isinstance(args, dict):
                    args, err = {}, "arguments must be a JSON object"
            except json.JSONDecodeError as ex:
                args, err = {}, f"invalid JSON arguments: {ex}"
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args, parse_error=err))
        usage = data.get("usage") or {}
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        raw_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if msg.get("tool_calls"):
            # echo the ids we answer with: a call the provider sent without an id got one above, and the
            # tool reply's tool_call_id must match the assistant message or the next request is rejected
            raw_msg["tool_calls"] = [{**tc, "id": c.id} for tc, c in zip(msg["tool_calls"], calls)]
        return LLMResponse(content=content, tool_calls=calls, prompt_tokens=usage.get("prompt_tokens", 0),
                           completion_tokens=usage.get("completion_tokens", 0), model=data.get("model", model), raw_message=raw_msg)

    async def aclose(self) -> None:
        await self._http.aclose()
