"""
Conversations that outlive a single HTTP request.

The CLI keeps one Agent alive in a loop and asks permission by blocking on
input(). A browser can't do that — requests come and go, and the phone that
started a task might be in someone's pocket when the agent needs an answer.

So a session here owns:
  * an Agent (and therefore the conversation history)
  * the workspace it's working in
  * a queue of events the browser is streaming
  * a pending-permission slot the agent thread blocks on until the browser
    answers, or until it times out

The permission round-trip is the interesting part. The agent runs on its own
thread; when it wants to write a file it drops a request into `pending` and
waits on an Event. The browser sees a "permission" event in its stream, shows
buttons, and POSTs the answer, which sets the Event and unblocks the agent.
If nobody answers, it times out as a refusal — a phone left face-down should
never leave a half-finished edit hanging forever.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .agent import Agent
from .config import active_model_config, load_config
from .media import load_media_config
from .providers import build_provider
from .tools import Workspace, build_media_tools, build_tools

# How long the agent waits for a human to tap allow/deny before giving up.
PERMISSION_TIMEOUT = 300


@dataclass
class PendingPermission:
    id: str
    tool: str
    args: dict
    summary: str
    answered: threading.Event = field(default_factory=threading.Event)
    allowed: bool = False


class Session:
    """One conversation, one workspace, one agent."""

    def __init__(self, session_id: str, workspace: Path, cfg: dict):
        self.id = session_id
        self.workspace = Path(workspace)
        self.created = time.time()
        self.last_used = time.time()
        # One append-only log, and a condition clients wait on. A client
        # tracks how much it has seen; there is no second copy anywhere for
        # an event to be delivered from twice.
        self.log: list[dict] = []
        self.log_base = 0          # how many were dropped off the front
        self.cond = threading.Condition()
        self.pending: PendingPermission | None = None
        self.busy = False
        self.lock = threading.Lock()
        self._build_agent(cfg)

    def _build_agent(self, cfg: dict) -> None:
        provider = build_provider(active_model_config(cfg))
        ws = Workspace(self.workspace)
        mc = load_media_config(cfg)
        self.model_name = cfg.get("active_model", "?")
        self.agent = Agent(
            provider=provider,
            tools=build_tools(ws) + build_media_tools(ws, mc),
            max_steps=int(cfg["agent"].get("max_steps", 40)),
            permission_mode=cfg["agent"].get("permission_mode", "ask"),
            notes_path=ws.root / "FORGE-NOTES.md",
        )

    def reload_model(self, cfg: dict) -> None:
        """Swap the model but keep the conversation."""
        history = self.agent.history
        self._build_agent(cfg)
        self.agent.history = history

    # -- the permission round-trip ------------------------------------

    def ask_permission(self, tool: str, args: dict, summary: str) -> bool:
        """
        Called on the agent's thread. Blocks until the browser answers.

        Returns False on timeout — an unanswered prompt is a refusal, never
        an approval. Silence must never be read as consent when the thing
        on the other end can edit files and run commands.
        """
        req = PendingPermission(id=uuid.uuid4().hex[:8], tool=tool,
                                args=args, summary=summary)
        self.pending = req
        self.emit("permission", {"id": req.id, "tool": tool,
                                 "summary": summary, "args": args})
        answered = req.answered.wait(timeout=PERMISSION_TIMEOUT)
        self.pending = None
        if not answered:
            self.emit("note", {"text": "No answer in time — treating that as no."})
            return False
        return req.allowed

    def answer_permission(self, request_id: str, allowed: bool) -> bool:
        req = self.pending
        if not req or req.id != request_id:
            return False
        req.allowed = allowed
        req.answered.set()
        return True

    # -- events -------------------------------------------------------

    def emit(self, kind: str, data: dict) -> None:
        with self.cond:
            seq = self.log_base + len(self.log)
            self.log.append({"kind": kind, **data, "t": time.time(), "seq": seq})
            # keep memory bounded on a long-lived session
            if len(self.log) > 500:
                dropped = 250
                del self.log[:dropped]
                self.log_base += dropped
            self.cond.notify_all()

    def since(self, cursor: int, timeout: float = 20.0) -> tuple[list[dict], int]:
        """
        Everything after `cursor`, waiting up to `timeout` for something new.

        Returns (items, new_cursor). An empty list means nothing happened and
        the caller should send a keepalive and ask again.
        """
        with self.cond:
            start = max(cursor, self.log_base)
            if start >= self.log_base + len(self.log):
                self.cond.wait(timeout)
            start = max(cursor, self.log_base)
            items = self.log[start - self.log_base:]
            return list(items), self.log_base + len(self.log)

    # -- running a turn -----------------------------------------------

    def run_message(self, text: str) -> None:
        """Drive one user message to completion. Runs on its own thread."""
        with self.lock:
            if self.busy:
                self.emit("note", {"text": "Still working on the last one."})
                return
            self.busy = True
        self.last_used = time.time()
        try:
            for ev in self.agent.run(text, ask=self.ask_permission):
                if ev.kind == "text" and ev.text.strip():
                    self.emit("text", {"text": ev.text})
                elif ev.kind == "tool_request":
                    if not ev.will_ask:
                        self.emit("tool", {"tool": ev.tool, "summary": ev.summary})
                elif ev.kind == "tool_result":
                    self.emit("result", {"tool": ev.tool,
                                         "text": (ev.text or "")[:2000]})
                elif ev.kind == "note":
                    self.emit("note", {"text": ev.text})
                elif ev.kind == "error":
                    self.emit("error", {"text": ev.text})
                elif ev.kind == "done":
                    self.emit("done", {"usage": ev.usage or {}})
        except Exception as e:
            self.emit("error", {"text": f"{type(e).__name__}: {e}"})
        finally:
            self.busy = False
            self.last_used = time.time()


class SessionStore:
    """All live conversations, keyed by id."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def get_or_create(self, session_id: str | None, workspace: str | None) -> Session:
        cfg = load_config()
        with self.lock:
            if session_id and session_id in self.sessions:
                s = self.sessions[session_id]
                # a model switched in the dashboard should land here too
                if s.model_name != cfg.get("active_model"):
                    s.reload_model(cfg)
                return s
            sid = session_id or uuid.uuid4().hex[:12]
            ws = Path(workspace or Path.home()).expanduser()
            if not ws.is_dir():
                ws = Path.home()
            s = Session(sid, ws, cfg)
            self.sessions[sid] = s
            return s

    def get(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def listing(self) -> list[dict]:
        return [{
            "id": s.id,
            "workspace": str(s.workspace),
            "model": s.model_name,
            "busy": s.busy,
            "messages": len(s.agent.history),
            "last_used": s.last_used,
        } for s in sorted(self.sessions.values(),
                          key=lambda x: -x.last_used)]

    def drop(self, session_id: str) -> bool:
        with self.lock:
            return self.sessions.pop(session_id, None) is not None
