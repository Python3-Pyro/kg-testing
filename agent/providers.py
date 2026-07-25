"""LLM provider abstraction so SQLAgent/GraphAgent's tool-use loop can run
against Anthropic, native OpenAI, or any OpenRouter-hosted model without the
agent code caring which. Two calling conventions exist in practice:
Anthropic's Messages API (content blocks, tool_use/tool_result) and
OpenAI-compatible Chat Completions (function tool_calls, role="tool"
messages) — OpenRouter and native OpenAI both speak the latter, differing
only in api_key/base_url/occasionally extra params like reasoning_effort.

Each provider normalizes a turn into an LLMResponse and knows how to append
its own turn (assistant response + tool results) back onto the shared
`messages` list in its own wire format — the agent loop only ever touches
the normalized LLMResponse fields, never provider-specific shapes.
"""
import json
import os
from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    text: str
    tool_calls: list = field(default_factory=list)  # [{"id", "name", "input"}]
    stop_reason: str = "end_turn"  # "tool_use" | "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    _raw: object = None  # provider-specific, used only by append_assistant_turn


class AnthropicProvider:
    def __init__(self, model, api_key=None):
        import anthropic
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    def _to_provider_tool(self, tool):
        return tool  # already Anthropic-shaped {name, description, input_schema}

    def call(self, system, tools, messages, max_tokens=2048):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[self._to_provider_tool(t) for t in tools],
            messages=messages,
        )
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in response.content if b.type == "tool_use"
        ]
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            _raw=response,
        )

    def append_assistant_turn(self, messages, response):
        messages.append({"role": "assistant", "content": response._raw.content})

    def append_tool_results(self, messages, results):
        # results: list of (tool_call_id, result_dict)
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tc_id, "content": str(result)}
                for tc_id, result in results
            ],
        })


class OpenAICompatibleProvider:
    """Covers both native OpenAI and OpenRouter — same wire format, only
    api_key/base_url (and occasionally reasoning_effort, for newer OpenAI
    reasoning models that reject tool calls at their default reasoning
    setting in the Chat Completions endpoint) differ."""

    def __init__(self, model, api_key, base_url=None, reasoning_effort=None, max_tokens=1024):
        from openai import OpenAI
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.reasoning_effort = reasoning_effort
        self.default_max_tokens = max_tokens

    def _to_provider_tool(self, tool):
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }

    def call(self, system, tools, messages, max_tokens=None):
        full_messages = [{"role": "system", "content": system}] + messages
        kwargs = dict(
            model=self.model,
            messages=full_messages,
            tools=[self._to_provider_tool(t) for t in tools],
            max_completion_tokens=max_tokens or self.default_max_tokens,
        )
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        response = self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        tool_calls = [
            {"id": tc.id, "name": tc.function.name, "input": json.loads(tc.function.arguments)}
            for tc in (msg.tool_calls or [])
        ]
        usage = response.usage
        return LLMResponse(
            text=msg.content or "",
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            _raw=msg,
        )

    def append_assistant_turn(self, messages, response):
        msg = response._raw
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])],
        })

    def append_tool_results(self, messages, results):
        for tc_id, result in results:
            messages.append({"role": "tool", "tool_call_id": tc_id, "content": str(result)})


# Curated set of models actually verified working this session (see CLAUDE.md
# Phase 3 "Multi-model comparison" for the transcripts). "key" is what the UI
# and eval CLI use to select a model; everything else configures the provider.
MODEL_REGISTRY = {
    "claude-sonnet-5": {
        "label": "Claude Sonnet 5 (Anthropic)",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
    },
    "claude-haiku-4.5": {
        "label": "Claude Haiku 4.5 (Anthropic)",
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
    },
    "gpt-5.6-luna": {
        "label": "GPT-5.6 Luna (OpenAI)",
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "none",
    },
    "gpt-4o-openrouter": {
        "label": "GPT-4o (via OpenRouter)",
        "provider": "openrouter",
        "model": "openai/gpt-4o",
    },
    "gemma-4-31b-openrouter": {
        "label": "Gemma 4 31B (via OpenRouter)",
        "provider": "openrouter",
        "model": "google/gemma-4-31b-it",
    },
    "deepseek-v3-openrouter": {
        "label": "DeepSeek V3 (via OpenRouter)",
        "provider": "openrouter",
        "model": "deepseek/deepseek-chat",
    },
}

DEFAULT_MODEL_KEY = "claude-sonnet-5"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def get_provider(model_key=DEFAULT_MODEL_KEY):
    if model_key not in MODEL_REGISTRY:
        raise ValueError(f"Unknown model key {model_key!r}. Choices: {list(MODEL_REGISTRY)}")
    spec = MODEL_REGISTRY[model_key]

    if spec["provider"] == "anthropic":
        return AnthropicProvider(model=spec["model"], api_key=os.environ["ANTHROPIC_API_KEY"])
    if spec["provider"] == "openai":
        return OpenAICompatibleProvider(
            model=spec["model"],
            api_key=os.environ["OPENAI_API_KEY"],
            reasoning_effort=spec.get("reasoning_effort"),
        )
    if spec["provider"] == "openrouter":
        return OpenAICompatibleProvider(
            model=spec["model"],
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url=OPENROUTER_BASE_URL,
            reasoning_effort=spec.get("reasoning_effort"),
        )
    raise ValueError(f"Unknown provider {spec['provider']!r} for model key {model_key!r}")
