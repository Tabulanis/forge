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
    # The provider's own untranslated assistant content, when replaying it
    # verbatim matters. Claude's newer models (Opus 5, Fable 5) think before
    # they answer, and the API requires those thinking blocks passed back
    # unchanged when a tool loop continues — a history rebuilt from just
    # text + calls silently drops them. Other providers leave this None and
    # ignore it when it belongs to someone else.
    assistant_blocks: Any = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider:
    """Interface every provider implements."""

    name = "base"

    def complete(self, system: str, messages: list[dict], tools: list[dict],
                 grammar: str | None = None) -> Reply:
        raise NotImplementedError

    def context_limit(self) -> int:
        """How many tokens fit in this model's window. 0 = unknown."""
        return 0

    # Providers speak different dialects for conversation history. Each one
    # converts our neutral history into its own format inside complete(), so
    # the agent loop only ever deals with the neutral form:
    #   {"role": "user"|"assistant", "content": str}
    #   {"role": "tool_result", "id": str, "content": str, "is_error": bool}
    #   {"role": "tool_use", "calls": [ToolCall, ...], "text": str}


# Models whose safety classifiers can decline a request outright; for these
# we opt into Anthropic's server-side fallback so a decline gets retried on
# a substitute model automatically.
SAFETY_FALLBACK_MODELS = {"claude-fable-5", "claude-mythos-5", "claude-opus-5"}


class AnthropicProvider(Provider):
    """Claude via the official SDK — native tool use, no translation games."""

    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, max_tokens: int = 8000):
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY itself
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def context_limit(self) -> int:
        return 200_000   # every current Claude model

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
                if m.get("assistant_blocks") is not None:
                    # Replay Claude's own blocks untouched — thinking blocks
                    # included. Blocks from a different Claude model are
                    # dropped server-side, so a mid-session model switch is
                    # still safe.
                    payload.append({"role": "assistant",
                                    "content": m["assistant_blocks"]})
                    continue
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

        import anthropic

        try:
            if self.model in SAFETY_FALLBACK_MODELS:
                # These models run safety classifiers that can decline a benign
                # request. fallbacks="default" re-runs a declined request on
                # Anthropic's recommended substitute model inside the same call,
                # so the user gets an answer instead of silence.
                resp = self.client.beta.messages.create(
                    **kwargs,
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            else:
                resp = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise RuntimeError(
                "Claude rejected the API key. Set a valid one with\n"
                "  export ANTHROPIC_API_KEY=sk-ant-...\n"
                "before starting Forge, or paste it into the dashboard's "
                "Add-a-model form. Or switch to a local model: /model qwen30b"
            ) from None
        except anthropic.NotFoundError:
            raise RuntimeError(
                f"Claude doesn't recognize the model name {self.model!r}. "
                f"Current names include claude-opus-5, claude-fable-5, "
                f"claude-sonnet-5, claude-haiku-4-5 — fix it in the dashboard "
                f"or ~/.forge/config.yaml."
            ) from None
            # Note: keep this list in sync with web/index.html's datalist.
        except anthropic.APIConnectionError:
            raise RuntimeError(
                "Couldn't reach Claude's servers — is the internet up? "
                "A local model works offline: /model qwen30b"
            ) from None

        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }

        if resp.stop_reason == "refusal":
            return Reply(
                text="Claude declined this request (safety filters). This is "
                     "sometimes a false positive — rephrasing the ask usually "
                     "clears it.",
                raw=resp,
                usage=usage,
            )

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
            usage=usage,
            assistant_blocks=list(resp.content),
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
                 max_tokens: int = 4096, timeout: float = 300.0,
                 context: int = 0):
        import httpx

        self.model = model
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self._context = int(context)   # explicit config beats probing
        self._probed: int | None = None
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def context_limit(self) -> int:
        """
        The window size, from config if given, else asked of the server.

        llama.cpp answers /props at the server root (not under /v1) with the
        n_ctx it was actually started with — the ground truth, since the
        window is set by the -c flag at launch, not by the model file.
        Runners without /props (Ollama, LM Studio) get a conservative 8k
        default rather than an optimistic one: compacting a bit early is
        cheap, overflowing is not.
        """
        if self._context > 0:
            return self._context
        if self._probed is None:
            import httpx
            self._probed = 0
            root = self.base_url[:-3] if self.base_url.endswith("/v1") \
                else self.base_url
            try:
                r = httpx.get(f"{root}/props", timeout=3.0)
                if r.status_code == 200:
                    d = r.json()
                    self._probed = int(
                        d.get("default_generation_settings", {}).get("n_ctx", 0))
            except Exception:
                pass
        return self._probed or 8192

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

        import httpx

        try:
            r = self.client.post("/chat/completions", json=body)
            r.raise_for_status()
        except httpx.ConnectError:
            raise RuntimeError(
                f"Nothing is answering at {self.base_url}. Start the model "
                f"server first (~/forge/start-model.sh big) and wait ~40 "
                f"seconds, or switch models with /model."
            ) from None
        except httpx.TimeoutException:
            raise RuntimeError(
                f"The model server at {self.base_url} took too long to answer. "
                f"It may still be loading the model — wait a minute and retry. "
                f"forge doctor can tell you what's running."
            ) from None
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"The model server at {self.base_url} replied with an error "
                f"({e.response.status_code}). It may have crashed or be mid-"
                f"restart — check it with: forge doctor"
            ) from None
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
            model=cfg.get("model", "claude-opus-5"),
            api_key=cfg.get("api_key") or None,
            max_tokens=int(cfg.get("max_tokens", 8000)),
        )
    if kind in ("openai-compat", "local", "ollama", "llamacpp", "lmstudio"):
        return OpenAICompatProvider(
            model=cfg.get("model", "local-model"),
            base_url=cfg.get("base_url", "http://localhost:8080/v1"),
            api_key=cfg.get("api_key") or "not-needed",
            max_tokens=int(cfg.get("max_tokens", 4096)),
            context=int(cfg.get("context", 0)),
        )
    raise ValueError(f"Unknown provider: {kind!r}")
