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

# Her LIVE reading-in speed (prompt tokens/s), from llama-server's timings.
# It falls as her context grows (~280 tok/s near empty, ~115 at 60-90k on the
# 27B, measured 2026-09-28), so read_file prices a file with this, not a
# constant. Only replies that read >= 500 new tokens count; tiny ones are noise.
READ_SPEED: dict = {"tps": None}


# 2026-10-27: SLOTS. The reviewer's 300-token calls were landing on her chat's
# slot. llama-server runs her brain with -np 3 and gives a prompt that matches
# no slot to the least-recently-used one — which was hers, so it was evicted.
# Her chat state is ~7.6 GB and --cache-ram is 2 GB, so it can never be parked
# and comes back only by re-reading 36k-65k tokens (200-380 s). Measured in the
# day's log: 7 of 8 full re-reads came right after a 135-532-token review on
# the same slot. So each job gets its own slot by id ("id_slot" in the body).
# Only for the server her chat runs on, and only when that server says it has
# enough slots — a 1-slot server errors on an id it does not have, so it gets
# nothing and behaves exactly as before. config.yaml `slots:` overrides the
# ids; `slots: false` turns this off.
# 2026-10-31: the side lane (server._vip_side_answer) gets slot 2 to itself
# as "side": it is the only job that runs WHILE a turn runs, and its prompt
# front is frozen per turn so the slot's cache answers the second question by
# reading only the question. The helpers (steer every 6 steps, the librarian,
# the fold summarizer, the advocate) move onto the reviewer's slot 1 — all of
# them run on her own thread, one at a time, never concurrently with the
# reviewer, and none depends on a cached prefix beyond a short system prompt.
SLOT_DEFAULTS = {"chat": 0, "reviewer": 1, "helpers": 1, "side": 2}
_SLOT_COUNT: dict = {}     # server root -> how many slots its /slots listed


def _root(base_url: str) -> str:
    u = (base_url or "").rstrip("/")
    return u[:-3] if u.endswith("/v1") else u


def slot_for(base_url: str, role: str) -> int | None:
    """The slot id for this job on this server, or None to send nothing."""
    try:
        from .config import active_model_config, load_config
        cfg = load_config()
        conf = cfg.get("slots", {})
        if conf is False:
            return None
        ids = {**SLOT_DEFAULTS, **(conf if isinstance(conf, dict) else {})}
        brain = active_model_config(cfg).get("base_url", "")
    except BaseException:          # active_model_config raises SystemExit
        return None
    root = _root(base_url)
    if role not in ids or _root(brain) != root:
        return None
    if root not in _SLOT_COUNT:
        try:
            import httpx
            r = httpx.get(f"{root}/slots", timeout=3.0)
            d = r.json() if r.status_code == 200 else []
            _SLOT_COUNT[root] = len(d) if isinstance(d, list) else 0
        except Exception:
            return None            # not cached: ask again next call
    n = _SLOT_COUNT[root]
    try:
        want = [int(v) for v in ids.values()]
        return int(ids[role]) if n >= 3 and n > max(want) else None
    except (TypeError, ValueError):
        return None


def slot_kw(prov, role: str) -> dict:
    """{"id_slot": n} for prov.complete() (or a raw request body, given a
    base_url string), or {} — so providers and test fakes that know nothing
    about slots are called exactly as before."""
    url = prov if isinstance(prov, str) else getattr(prov, "base_url", None)
    n = slot_for(url, role) if isinstance(url, str) else None
    return {} if n is None else {"id_slot": n}


def note_read_speed(timings: dict) -> None:
    n, tps = timings.get("prompt_n") or 0, timings.get("prompt_per_second")
    if tps and n >= 500:
        READ_SPEED["tps"] = float(tps)


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
                 grammar: str | None = None,
                 extra_body: dict | None = None,
                 on_delta=None) -> Reply:
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
                 grammar: str | None = None,
                 extra_body: dict | None = None,
                 on_delta=None) -> Reply:
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
                "Add-a-model form. Or switch to a local model: /model big122"
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
                "A local model works offline: /model big122"
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
        # `timeout` is how long the server may go SILENT, not how long a call
        # may take. Every call streams (see complete), so the read timeout
        # resets on each chunk: a model still producing tokens is never cut
        # off; one that has gone quiet for this long is. A whole-call cap was
        # what killed her best bug-hunt run on 2026-09-05 — the server finished
        # a 2,900-token answer at 12 tok/s after the client had hung up at 300s.
        self.silence = float(timeout)
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=15.0, read=self.silence,
                                  write=60.0, pool=15.0),
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
                 grammar: str | None = None,
                 extra_body: dict | None = None,
                 on_delta=None, id_slot: int | None = None) -> Reply:
        """
        `grammar` is a llama.cpp GBNF grammar. When given, the server can only
        emit text the grammar allows — a small model that would otherwise
        ramble is made *incapable* of answering in the wrong shape. This is
        the single most useful trick for getting reliable structured answers
        out of a local model, and it has no equivalent in the hosted APIs.
        """
        body: dict[str, Any] = {"model": self.model,
                                **self._chat_body(system, messages, tools),
                                "max_tokens": self.max_tokens}
        if grammar:
            body["grammar"] = grammar
        if extra_body:
            # e.g. {"chat_template_kwargs": {"enable_thinking": False}} — lets a
            # caller skip the reasoning phase for pure-judgment/describe tasks.
            body.update(extra_body)

        # Always stream — whether or not anyone is watching. Streaming is what
        # makes the timeout a silence timeout instead of a whole-call cap (see
        # __init__). No callback → a no-op one; the Reply comes out the same.
        if on_delta is None:
            on_delta = lambda _text: None
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        if id_slot is None:
            return self._stream(body, on_delta)
        # Pinned to a slot (see SLOT_DEFAULTS). If the server refuses it — it
        # was restarted with fewer slots since we counted — forget the count
        # and send the call once more the old way, unpinned.
        body["id_slot"] = int(id_slot)
        try:
            return self._stream(body, on_delta)
        except RuntimeError as e:
            if "HTTP 4" not in str(e):
                raise
            _SLOT_COUNT.pop(_root(self.base_url), None)
            body.pop("id_slot", None)
            return self._stream(body, on_delta)

    def _chat_body(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        """The messages + tools of a chat request exactly as complete() sends
        them — one place, so prime() renders the very same prompt (2026-10-30)."""
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

        body: dict[str, Any] = {"messages": payload}
        if tools:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            } for t in tools]
        return body

    # A user message no real chat contains: rendering the chat with it and
    # without it shows where the next turn's prompt starts to differ.
    _PRIME_PROBE = "\u2063forge-prime-probe\u2063"

    def prime_text(self, system: str, messages: list[dict], tools: list[dict],
                   template_kwargs: dict) -> str:
        """The chat rendered EXACTLY as the next turn's request will render it,
        up to and including the last message, with NO generation prompt
        (2026-10-30). Two /apply-template renders on the server's own template:
        the chat as-is (which ends in a generation prompt) and the chat plus a
        probe user message. Their common prefix is everything the next turn
        shares with what the slot holds; it is checked to end within a short
        tail of the first render and before the probe. Raises on any doubt."""
        import httpx
        import os as _os
        base = self._chat_body(system, messages, tools)
        root = _root(self.base_url)

        def render(msgs):
            r = httpx.post(f"{root}/apply-template", timeout=60.0,
                           json={**base, "messages": msgs,
                                 "chat_template_kwargs": template_kwargs})
            if r.status_code != 200:
                raise RuntimeError(f"apply-template HTTP {r.status_code}: {r.text[:200]}")
            return r.json()["prompt"]

        r0 = render(base["messages"])
        r1 = render(base["messages"] + [{"role": "user", "content": self._PRIME_PROBE}])
        text = _os.path.commonprefix([r0, r1])
        if len(r0) - len(text) > 200 or self._PRIME_PROBE not in r1[len(text):]:
            raise RuntimeError(f"no clean prefix (r0 {len(r0)}, r1 {len(r1)}, common {len(text)})")
        return text

    def prime(self, system: str, messages: list[dict], tools: list[dict],
              template_kwargs: dict, id_slot: int, still=lambda: True) -> dict | None:
        """Leave `id_slot` holding exactly prime_text() — nothing generated —
        so the next turn's prompt EXTENDS the cached tokens and a hybrid
        model reuses them even after a restore from disk (which has no
        checkpoints to roll back to). Returns the server's timings, or None
        when `still()` says a new turn began meanwhile."""
        import httpx
        text = self.prime_text(system, messages, tools, template_kwargs)
        if not still():
            return None
        r = httpx.post(f"{_root(self.base_url)}/completion", timeout=1800.0,
                       json={"prompt": text, "n_predict": 0, "cache_prompt": True,
                             "id_slot": int(id_slot), "stream": False})
        if r.status_code != 200:
            raise RuntimeError(f"completion HTTP {r.status_code}: {r.text[:200]}")
        d = r.json()
        return {**(d.get("timings") or {}), "chars": len(text),
                "tokens_cached": d.get("tokens_cached")}

    def _stream(self, body: dict, on_delta) -> Reply:
        """Stream the reply: fire on_delta(text) as each content chunk arrives,
        reassemble tool-call fragments, and return the same Reply the one-shot
        path would. Only `content` is streamed — reasoning stays private (the
        UI's 'thinking' indicator covers that phase)."""
        import httpx
        parts: list[str] = []
        tool_accum: dict[int, dict] = {}
        usage: dict = {}
        try:
            with self.client.stream("POST", "/chat/completions", json=body) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if chunk.get("timings"):
                        note_read_speed(chunk["timings"])
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        parts.append(text)
                        try:
                            on_delta(text)
                        except Exception:
                            pass          # a UI hiccup must never kill the turn
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        acc = tool_accum.setdefault(idx, {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["args"] += fn["arguments"]
        except httpx.ConnectError:
            raise RuntimeError(
                "The model isn't running right now. Wait a minute and send "
                "your message again."
            ) from None
        except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
            # Keep the status code and the server's own words in the message.
            # The agent's overflow recovery greps this text for "400"/"context"
            # to decide whether trimming and retrying can save the turn —
            # llama.cpp's overflow body literally says "exceeds the available
            # context size", so hiding it turns a RECOVERABLE overflow into a
            # dead run (root-caused live 2026-08-15).
            detail = type(e).__name__
            if isinstance(e, httpx.ReadTimeout):
                detail = (f"no tokens for {int(self.silence)}s — the model went "
                          f"quiet (raise `timeout:` for this model in config.yaml "
                          f"if it is just slow)")
            if isinstance(e, httpx.HTTPStatusError):
                detail = f"HTTP {e.response.status_code}"
                try:
                    body = e.response.read().decode(errors="replace")[:200]
                    if body:
                        detail += f": {body}"
                except Exception:
                    pass
            raise RuntimeError(
                f"The model server had trouble ({detail}). Try again "
                f"in a moment."
            ) from None

        calls = []
        for idx in sorted(tool_accum):
            acc = tool_accum[idx]
            try:
                args = json.loads(acc["args"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": acc["args"]}
            calls.append(ToolCall(id=acc["id"] or f"call_{idx}",
                                  name=acc["name"], args=args))
        return Reply(text="".join(parts), tool_calls=calls, raw=None, usage=usage)


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
            # seconds of silence allowed before a call is declared dead; per
            # model, because the same weights run at very different speeds on
            # different hardware (2026-09-07: 28 tok/s on Void for the 122B; the TITAN ran the 27B at 12.8)
            timeout=float(cfg.get("timeout", 300)),
        )
    raise ValueError(f"Unknown provider: {kind!r}")
