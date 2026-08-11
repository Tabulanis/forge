"""
Mid-term memory: recall, and the librarian.

Merge's memory comes in three speeds: the context window (short — lives on
the graphics card, lossy when full), the saved sessions on disk (mid —
every word ever exchanged, verbatim), and the notebook + project files
(long). This module opens the middle tier:

  * search() — plain-code lookup across every saved conversation. No model
    involved: finding text doesn't need a brain, and the OS file cache
    (spare RAM) makes repeat lookups instant.
  * remember_turn() — drops the finished exchange in the librarian's
    in-tray (a queue folder). Instant; nobody waits.
  * process_queue_forever() — the librarian himself: the dashboard (always
    running) works the tray on a background thread, asking the little 3B
    to distill each exchange into a one-line index card. Cards catch
    searches where the wording differs from the memory. Little model down
    = the tray just waits; search still finds every verbatim word.

The tray exists because the first version used a thread in the CLI — which
dies with the CLI, so one-shot chats lost their cards to a librarian who
was still reading when the lights went out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .config import load_config

SESS_DIR = Path.home() / ".forge" / "sessions"
CARDS_PATH = Path.home() / ".forge" / "memory-cards.jsonl"
QUEUE_DIR = Path.home() / ".forge" / "card-queue"

# Short prompt on purpose: the 3B reads ~24 tokens/sec on the CPU, so the
# card job must stay a few hundred tokens or the librarian falls behind.
CARD_PROMPT = ("One line, 25 words max: what happened in this exchange? "
               "Name the concrete things — names, files, decisions, numbers. "
               "No preamble, no quotes.")
EXCHANGE_LIMIT = 1200      # chars of (user + answer) the librarian reads


def _words(query: str) -> list[str]:
    return [w for w in "".join(c.lower() if c.isalnum() else " "
                               for c in query).split() if len(w) >= 3]


def _snippet(text: str, words: list[str], width: int = 150) -> str:
    low = text.lower()
    at = min((low.find(w) for w in words if w in low), default=0)
    start = max(0, at - width // 3)
    piece = text[start:start + width].replace("\n", " ").strip()
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def search(query: str, workspace: str = "", limit: int = 8) -> str:
    """Every hit is something that was actually said, with when and where."""
    words = _words(query)
    if not words:
        return "Give recall a few concrete words to look for."
    hits: list[tuple[float, float, str]] = []   # (score, when, line)

    # The librarian's index cards: short and dense, so they rank well.
    if CARDS_PATH.exists():
        for raw in CARDS_PATH.read_text(encoding="utf-8").splitlines():
            try:
                c = json.loads(raw)
            except Exception:
                continue
            gist = c.get("gist", "")
            low = gist.lower()
            score = sum(1.0 for w in words if w in low)
            if score:
                if c.get("workspace") == workspace:
                    score += 0.5
                when = c.get("t", 0)
                day = time.strftime("%b %d", time.localtime(when))
                folder = Path(c.get("workspace", "")).name or "?"
                hits.append((score, when,
                             f"[{day} · {folder} · index card] {gist}"))

    # The full transcripts: verbatim, both voices.
    for f in SESS_DIR.glob("*.json") if SESS_DIR.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        folder = Path(d.get("workspace", "")).name or "?"
        boost = 0.5 if d.get("workspace") == workspace else 0.0
        for e in d.get("log", []):
            if e.get("kind") not in ("user", "text"):
                continue
            text = e.get("text", "")
            low = text.lower()
            score = sum(1.0 for w in words if w in low)
            if not score:
                continue
            who = "user" if e["kind"] == "user" else "Merge"
            when = e.get("t", 0)
            day = time.strftime("%b %d", time.localtime(when))
            hits.append((score + boost, when,
                         f"[{day} · {folder} · {who}] "
                         f"{_snippet(text, words)}"))

    if not hits:
        return (f"Nothing in past conversations matches {query!r}. "
                "It may never have been said, or said in other words — "
                "try different ones.")
    hits.sort(key=lambda h: (-h[0], -h[1]))
    return "\n".join(line for _, _, line in hits[:limit])


def remember_turn(user_text: str, answer_text: str,
                  workspace: str, session_id: str) -> None:
    """Drop the finished exchange in the librarian's tray. Instant — one
    small file write; the slow distilling happens elsewhere, later."""
    if not (user_text or "").strip() or not (answer_text or "").strip():
        return
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"t": time.time(), "workspace": workspace,
                 "session": session_id,
                 "user": user_text[:EXCHANGE_LIMIT // 2],
                 "answer": answer_text[:EXCHANGE_LIMIT // 2]}
        (QUEUE_DIR / f"{time.time_ns()}.json").write_text(
            json.dumps(entry), encoding="utf-8")
    except Exception:
        pass   # a missing card must never trouble a finished conversation


def _distill(entry: dict) -> str | None:
    """One exchange → one line, via the little model.

    Returns None when the little model is unreachable (leave the tray
    alone, try again later) and "" when it answered uselessly (drop the
    entry — retrying the same input won't get smarter)."""
    cfg = load_config()
    little = cfg.get("models", {}).get("little")
    if not little or not little.get("base_url"):
        return None
    exchange = f"User: {entry.get('user', '')}\nMerge: {entry.get('answer', '')}"
    try:
        r = httpx.post(
            little["base_url"].rstrip("/") + "/chat/completions",
            json={"model": little.get("model", "little"),
                  "messages": [{"role": "system", "content": CARD_PROMPT},
                               {"role": "user", "content": exchange}],
                  "max_tokens": 60, "temperature": 0.2},
            timeout=120.0)
        return r.json()["choices"][0]["message"]["content"].strip()
    except httpx.HTTPError:
        return None
    except Exception:
        return ""


def process_queue_once() -> int:
    """Work the tray. Returns how many cards were filed."""
    filed = 0
    for f in sorted(QUEUE_DIR.glob("*.json")) if QUEUE_DIR.is_dir() else []:
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            f.unlink(missing_ok=True)
            continue
        gist = _distill(entry)
        if gist is None:
            return filed          # librarian's asleep — leave the tray be
        if gist:
            with CARDS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "t": entry.get("t", time.time()),
                    "workspace": entry.get("workspace", ""),
                    "session": entry.get("session", ""),
                    "gist": gist.splitlines()[0][:200]}) + "\n")
            filed += 1
        f.unlink(missing_ok=True)
    return filed


def process_queue_forever(interval: float = 30.0) -> None:
    """The dashboard runs this on a daemon thread: it's the one process
    that's always up, so cards get filed no matter which door the
    conversation came through."""
    while True:
        try:
            process_queue_once()
        except Exception:
            pass
        time.sleep(interval)
