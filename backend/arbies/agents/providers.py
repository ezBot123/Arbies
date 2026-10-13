"""LLM providers for agent narrative analysis.

LLMs are only used for analysis/reporting. They never run in the trading path
and can only act through the agent's ToolGateway.

* ``AnthropicProvider`` - official Anthropic Python SDK, manual tool loop.
* ``OpenAICompatibleProvider`` - any OpenAI-compatible /chat/completions endpoint
  (raw HTTP via httpx), as required by the configurable-provider design.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from arbies.agents.tools import ToolGateway
from arbies.logging_setup import get_logger

log = get_logger(__name__)

# USD per 1M tokens (input, output). Estimates only - verify against current pricing.
PRICING = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-fable-5-1": (Decimal("10"), Decimal("50")),
}


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    est_cost_usd: Decimal = Decimal(0)
    stop_reason: str = ""
    notes: list[str] = field(default_factory=list)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    pin, pout = PRICING.get(model, (Decimal(0), Decimal(0)))
    return (pin * input_tokens + pout * output_tokens) / Decimal(1_000_000)


def _tool_result_text(result) -> str:
    text = json.dumps(result, default=str)
    return text if len(text) < 30000 else text[:30000] + "...(truncated)"


class LLMProvider:
    name = "none"

    async def run(self, system: str, prompt: str, gateway: ToolGateway, max_tool_calls: int) -> LLMResult:
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, client=None):
        import anthropic
        self.model = model
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key)

    async def run(self, system: str, prompt: str, gateway: ToolGateway, max_tool_calls: int) -> LLMResult:
        tools = [{"name": t.name, "description": t.description, "input_schema": t.json_schema()}
                 for t in gateway.available_tools()]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        res = LLMResult(text="", model=self.model)
        calls = 0
        while True:
            kwargs = dict(model=self.model, max_tokens=16000, system=system, messages=messages,
                          output_config={"effort": "medium"},
                          # server-side refusal fallback (routes declined requests to a fallback model)
                          betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            if tools and calls < max_tool_calls:
                kwargs["tools"] = tools
            resp = await self.client.beta.messages.create(**kwargs)
            res.input_tokens += resp.usage.input_tokens or 0
            res.output_tokens += resp.usage.output_tokens or 0
            res.stop_reason = resp.stop_reason or ""
            if resp.stop_reason == "refusal":
                res.notes.append("model declined the request")
                break
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                res.text = "".join(b.text for b in resp.content if b.type == "text")
                break
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                calls += 1
                try:
                    out = await gateway.call(block.name, dict(block.input or {}))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": _tool_result_text(out)})
                except Exception as e:  # noqa: BLE001 - report errors back to the model
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"error: {e}",
                                    "is_error": True})
            messages.append({"role": "user", "content": results})
            if calls >= max_tool_calls + 5:
                res.notes.append("tool call limit reached")
                break
        res.est_cost_usd = estimate_cost(self.model, res.input_tokens, res.output_tokens)
        return res


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str | None, model: str, base_url: str | None = None, transport=None):
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.transport = transport

    async def run(self, system: str, prompt: str, gateway: ToolGateway, max_tool_calls: int) -> LLMResult:
        tools = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                   "parameters": t.json_schema()}}
                 for t in gateway.available_tools()]
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        res = LLMResult(text="", model=self.model)
        calls = 0
        async with httpx.AsyncClient(timeout=120, transport=self.transport) as http:
            while True:
                body = {"model": self.model, "messages": messages}
                if tools and calls < max_tool_calls:
                    body["tools"] = tools
                r = await http.post(f"{self.base_url}/chat/completions", json=body, headers=self.headers)
                r.raise_for_status()
                data = r.json()
                usage = data.get("usage") or {}
                res.input_tokens += usage.get("prompt_tokens", 0)
                res.output_tokens += usage.get("completion_tokens", 0)
                choice = data["choices"][0]
                msg = choice["message"]
                res.stop_reason = choice.get("finish_reason") or ""
                messages.append(msg)
                tcs = msg.get("tool_calls") or []
                if not tcs:
                    res.text = msg.get("content") or ""
                    break
                for tc in tcs:
                    calls += 1
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                        out = await gateway.call(tc["function"]["name"], args)
                        content = _tool_result_text(out)
                    except Exception as e:  # noqa: BLE001
                        content = f"error: {e}"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})
                if calls >= max_tool_calls + 5:
                    res.notes.append("tool call limit reached")
                    break
        res.est_cost_usd = estimate_cost(self.model, res.input_tokens, res.output_tokens)
        return res


def build_provider(settings) -> LLMProvider | None:
    if settings.ai_provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider(settings.anthropic_api_key.get_secret_value(), settings.anthropic_model)
    if settings.ai_provider == "openai" and (settings.openai_api_key or settings.openai_base_url):
        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
        return OpenAICompatibleProvider(key, settings.openai_model, settings.openai_base_url)
    return None
