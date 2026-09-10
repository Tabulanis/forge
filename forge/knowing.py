"""Knowing that she does not know — measured, not felt.

The thing that matters most is not knowledge, it is calibration: a confident
wrong answer is worse than "I'd have to look". Her own record says so in
numbers — 35 bounces for claiming success against a failure, 29 for saying it
worked without running it — and the reviewer cannot catch a fact she never
checked.

THE MECHANISM. Ask her own brain the same narrow question several times,
independently, and compare the specific answer. What she knows comes back
identical. What she is inventing comes back different every time. Measured
2026-09-09 on this machine:

    boiling point of water        1 distinct answer in 5   knows
    author of Hamlet              1 in 5                   knows
    chemical symbol for gold      1 in 5                   knows
    paperclip inventor's name     4 in 5   (Johan / Johannes / Samuel)
    Amelia Earhart's first reg.   5 in 5   (NX163 / NX7904 / NX7970 / ...)

IT IS A ONE-WAY TEST, and the exception proves why. The 1923 FA Cup attendance
came back identical five times — 126047 — and is still wrong to state as exact,
because the real figure is disputed and far higher. A memorised error is perfectly
consistent. So:

    varied      -> she does NOT know. Certain. Go and look it up.
    consistent  -> not proof of anything. It is the absence of one kind of
                   error, not the presence of truth.

Never report consistency as verification. That would trade a loud failure for a
quiet one.
"""
from __future__ import annotations

import json
import re
import urllib.request

from .config import load_config

SAMPLES = 5
TEMP = 0.7
_TERSE = ("Answer with ONLY the specific fact asked for. No sentence, no hedging, "
          "no explanation. If you do not know, reply exactly: UNKNOWN")


def _brain() -> tuple[str, str]:
    cfg = load_config()
    m = (cfg.get("models") or {}).get(cfg.get("active_model") or "") or {}
    return (m.get("base_url") or "http://127.0.0.1:8087/v1").rstrip("/"), m.get("model", "big122")


def _one(url: str, model: str, q: str, timeout: float) -> str:
    body = json.dumps({"model": model, "max_tokens": 60, "temperature": TEMP,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "system", "content": _TERSE},
                                    {"role": "user", "content": q}]}).encode()
    r = urllib.request.Request(url + "/chat/completions", data=body,
                               headers={"Content-Type": "application/json"})
    try:
        out = json.load(urllib.request.urlopen(r, timeout=timeout))["choices"][0]["message"]["content"]
        return " ".join(str(out).split())[:120]
    except Exception as e:
        return f"(error {type(e).__name__})"


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())[:28]


def do_i_know(question: str, samples: int = SAMPLES) -> str:
    """Do I actually know this, or am I about to make it up?"""
    q = " ".join(str(question or "").split())
    if len(q) < 6:
        return ("Ask it the narrow thing you are about to assert — the name, the "
                "number, the date — not the whole question.")
    url, model = _brain()
    n = max(3, min(int(samples), 9))
    outs = [_one(url, model, q, timeout=120) for _ in range(n)]
    if all(o.startswith("(error") for o in outs):
        return f"Could not ask: {outs[0]}"
    said_unknown = sum(1 for o in outs if _key(o).startswith("unknown"))
    keys = [_key(o) for o in outs if not o.startswith("(error")]
    distinct = len(set(keys))

    if said_unknown >= max(2, n // 2):
        return (f"You said UNKNOWN {said_unknown} of {n} times. You do not know this. "
                f"Look it up before it goes in an answer, or say you would have to.")
    if distinct > 1:
        seen = sorted({o[:40] for o in outs})
        return ("YOU DO NOT KNOW THIS — and that is a certain result, not a guess.\n"
                f"Asked {n} times, you gave {distinct} different answers:\n"
                + "\n".join(f"    · {s}" for s in seen[:6])
                + "\nA fact you hold comes back the same every time. Look it up "
                  "(web_search, or the right verifier) before stating it, or say "
                  "plainly that you would have to check.")
    return (f"Consistent across {n} samples: {outs[0][:80]}\n"
            "That is NOT verification. It rules out ONE kind of error — making it "
            "up fresh — and nothing else. A memorised mistake is perfectly "
            "consistent: measured here, the 1923 FA Cup attendance came back "
            "identical five times and is still wrong to state as exact. If this "
            "is specific and it matters, check it anyway.")
