"""
Model providers — the seam that lets the same agent talk to Claude today and
a local model tomorrow.

Every provider takes the same inputs (system prompt, messages, tool schemas)
and returns the same shape (`Reply`), so nothing above this file knows or
cares which model answered. Adding a provider means adding a class here and
a line in `build_provider` — nothing else in the codebase changes.

Two provider styles are supported deliberately:
  * AnthropicProvider — the known-good brain, native tool-use API.
  * OpenAICompatProvider — the local one: llama.cpp, Ollama, LM Studio and
    vLLM all speak this same dialect, so one class covers every local runner
    the user is likely to try.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A model's request to run one tool."""
    id: str
    name: str
    args: dict


@dataclass
class Reply:
    """One turn of model output, normalized across providers."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    usage: dict = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider:
    """Interface every provider implements."""

    name = "base"

    def complete(self, system: str, messages: list[dict], tools: list[dict],
                 grammar: str | None = None) -> Reply:
        raise NotImplementedError

    # Providers speak different dialects for conversation history. Each one
    # converts our neutral history into its own format inside complete(), so
    # the agent loop only ever deals with the neutral form:
    #   {"role": "user"|"assistant", "content": str}
    #   {"role": "tool_result", "id": str, "content": str, "is_error": bool}
    #   {"role": "tool_use", "calls": [ToolCall, ...], "text": str}


class AnthropicProvider(Provider):
    """Claude via the official SDK — native tool use, no translation games."""

    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, max_tokens: int = 8000):
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY itself
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, system: str, messages: list[dict], tools: list[dict],
                 grammar: str | None = None) -> Reply:
        # grammar is a llama.cpp feature; the hosted API has no equivalent, so
        # it's accepted and ignored rather than making callers special-case.
        payload = []
        for m in messages:
            if m["role"] == "tool_result":
                payload.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m["id"],
                        "content": m["content"],
                        "is_error": m.get("is_error", False),
                    }],
                })
            elif m["role"] == "tool_use":
                blocks: list[dict] = []
                if m.get("text"):
                    blocks.append({"type": "text", "text": m["text"]})
                for c in m["calls"]:
                    blocks.append({
                        "type": "tool_use", "id": c.id, "name": c.name, "input": c.args,
                    })
                payload.append({"role": "assistant", "content": blocks})
            else:
                payload.append({"role": m["role"], "content": m["content"]})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": payload,
        }
        if tools:
            kwargs["tools"] = [{
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            } for t in tools]

        resp = self.client.messages.create(**kwargs)

        text_parts, calls = [], []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, args=dict(block.input)))

        return Reply(
            text="".join(text_parts),
            tool_calls=calls,
            raw=resp,
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
        )


class OpenAICompatProvider(Provider):
    """
    Any OpenAI-compatible endpoint: llama.cpp's server, Ollama, LM Studio,
    vLLM. One class covers them all because they share a wire format.

    Local models are less reliable at tool calling than Claude, so this
    provider is forgiving: if a model emits a tool call with arguments as a
    JSON *string* (very common) instead of an object, it's parsed rather
    than crashing the run.
    """

    name = "openai-compat"

    def __init__(self, model: str, base_url: str, api_key: str = "not-needed",
                 max_tokens: int = 4096, timeout: float = 300.0):
        import httpx

        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def complete(self, system: str, messages: list[dict], tools: list[dict],
                 grammar: str | None = None) -> Reply:
        """
        `grammar` is a llama.cpp GBNF grammar. When given, the server can only
        emit text the grammar allows — a small model that would otherwise
        ramble is made *incapable* of answering in the wrong shape. This is
        the single most useful trick for getting reliable structured answers
        out of a local model, and it has no equivalent in the hosted APIs.
        """
        payload = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "tool_result":
                payload.append({
                    "role": "tool", "tool_call_id": m["id"], "content": m["content"],
                })
            elif m["role"] == "tool_use":
                payload.append({
                    "role": "assistant",
                    "content": m.get("text") or None,
                    "tool_calls": [{
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.args)},
                    } for c in m["calls"]],
                })
            else:
                payload.append({"role": m["role"], "content": m["content"]})

        body: dict[str, Any] = {
            "model": self.model,
            "messages": payload,
            "max_tokens": self.max_tokens,
        }
        if grammar:
            body["grammar"] = grammar
        if tools:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            } for t in tools]

        r = self.client.post("/chat/completions", json=body)
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]["message"]

        calls = []
        for tc in (choice.get("tool_calls") or []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    # A local model produced unparseable arguments. Pass it
                    # through as a string so the tool layer can return a
                    # useful error instead of the whole run dying here.
                    args = {"_raw": raw_args}
            else:
                args = raw_args or {}
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}",
                                  name=fn.get("name", ""), args=args))

        return Reply(
            text=choice.get("content") or "",
            tool_calls=calls,
            raw=data,
            usage=data.get("usage", {}),
        )


def build_provider(cfg: dict) -> Provider:
    """Make a provider from a config dict — see config.py for the shape."""
    kind = cfg.get("provider", "anthropic")
    if kind == "anthropic":
        return AnthropicProvider(
            model=cfg.get("model", "claude-sonnet-4-5"),
            api_key=cfg.get("api_key") or None,
            max_tokens=int(cfg.get("max_tokens", 8000)),
        )
    if kind in ("openai-compat", "local", "ollama", "llamacpp", "lmstudio"):
        return OpenAICompatProvider(
            model=cfg.get("model", "local-model"),
            base_url=cfg.get("base_url", "http://localhost:8080/v1"),
            api_key=cfg.get("api_key") or "not-needed",
            max_tokens=int(cfg.get("max_tokens", 4096)),
        )
    raise ValueError(f"Unknown provider: {kind!r}")
