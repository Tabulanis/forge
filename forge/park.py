"""
Parking her chat's memory on disk (2026-10-27).

Her chat's slot state on the brain is ~7.6 GB; llama-server's RAM prompt
cache is 2 GB, so it was never kept (462 "exceeds cache size limit" skips in
one day) and every time the slot lost it she re-read 36k-65k tokens, 5-10
minutes. llama-server can write a slot's state to a file and read it back
(--slot-save-path, POST /slots/{id}?action=save|restore). So:

  * after a turn, `park_save` writes her chat slot to <session>.bin, in the
    background, with a small .json beside it naming the model it belongs to;
  * before a turn, `park_restore` reads it back IF the slot no longer holds
    this chat — seconds instead of minutes.

Only on her brain's server, only when slots are on (providers.slot_for), only
when the brain was started with --slot-save-path, never for an off-the-record
chat. config.yaml `park: false` (or `slots: false`) turns it off. Anything
that goes wrong here is quiet: the turn runs exactly as it did before, it just
re-reads.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

# Where the brain writes slot files: start-model.sh passes this same folder as
# --slot-save-path. If the two ever disagree, a save that does not show up
# here turns parking off (see park_save).
PARK_DIR = Path.home() / ".forge" / "slots"
# ~7.6 GB each; the newest PARK_KEEP chats are kept, by when last saved or used.
PARK_KEEP = 4
# Re-ask a server that said "no --slot-save-path" this often, so a brain
# restarted WITH the flag is picked up without restarting the dashboard.
_REPROBE_S = 600

_LIVE: dict = {}            # server root -> (True|False, when probed)
_DEAD = {"why": None}       # a save failed: off until the dashboard restarts
_SAID: set = set()          # log lines already printed once
# Which chat her chat slot holds right now, as far as we know: the session id,
# plus the slot's (id_task, n_prompt_tokens) when we last saw it hold that
# chat. Anything else that ran on the slot since changes id_task.
_HOLDER: dict = {"sid": None, "fp": None}
_LOCK = threading.Lock()    # one save/restore at a time
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _log(key: str, text: str) -> None:
    if key not in _SAID:
        _SAID.add(key)
        print(f"[park] {text}", file=sys.stderr, flush=True)


def _where() -> tuple[str, int] | None:
    """(server root, chat slot id) when parking applies, else None."""
    if _DEAD["why"]:
        return None
    try:
        from .config import active_model_config, load_config
        from .providers import _root, slot_for
        cfg = load_config()
        if cfg.get("park", True) is False:
            return None
        base = active_model_config(cfg).get("base_url", "")
    except BaseException:          # active_model_config raises SystemExit
        return None
    if not base:
        return None
    n = slot_for(base, "chat")     # None when slots are off or < 3 slots
    if n is None or not PARK_DIR.is_dir():
        return None
    root = _root(base)
    return (root, n) if _live(root) else None


def _live(root: str) -> bool:
    """Was the brain started with --slot-save-path? Asked once and cached
    (a 'no' is asked again every ten minutes). An unknown action costs the
    server nothing: it answers 'Invalid action' when the flag is on and
    'does not support slots action' when it is off."""
    ok, when = _LIVE.get(root, (None, 0.0))
    if ok or (ok is False and time.time() - when < _REPROBE_S):
        return bool(ok)
    try:
        import httpx
        r = httpx.post(f"{root}/slots/0?action=probe", json={}, timeout=5.0)
        ok = "slot-save-path" not in r.text and r.status_code in (200, 400)
    except Exception:
        return False               # not cached: the brain may be loading
    _LIVE[root] = (ok, time.time())
    if not ok:
        _log("noflag", "the brain has no --slot-save-path; parking is off until it does")
    return ok


def _slot(root: str, n: int) -> dict | None:
    try:
        import httpx
        r = httpx.get(f"{root}/slots", timeout=5.0)
        for s in r.json():
            if s.get("id") == n:
                return s
    except Exception:
        pass
    return None


def _fp(s: dict | None):
    return None if not s else (s.get("id_task"), s.get("n_prompt_tokens"))


def _same_fp(saved, now) -> bool:
    """A sidecar's saved fingerprint against the slot's own now. Only a
    real one counts: an id and a non-zero token count."""
    try:
        return (bool(saved) and len(saved) == 2 and now is not None
                and saved[0] is not None and bool(saved[1])
                and (saved[0], int(saved[1])) == (now[0], int(now[1] or 0)))
    except (TypeError, ValueError):
        return False


def holds(sid: str) -> bool:
    """Is this chat the last one known to have run on her chat slot?"""
    return _HOLDER["sid"] == sid


def _model(root: str) -> dict:
    """What a parked file belongs to. A file from another model, build or
    context size is not hers any more."""
    try:
        import httpx
        p = httpx.get(f"{root}/props", timeout=5.0).json()
        return {"model": p.get("model_path"), "build": p.get("build_info"),
                "n_ctx": (p.get("default_generation_settings") or {}).get("n_ctx")}
    except Exception:
        return {}


def _files(sid: str) -> tuple[Path, Path]:
    return PARK_DIR / f"{sid}.bin", PARK_DIR / f"{sid}.json"


def _drop(sid: str) -> None:
    for f in _files(sid):
        try:
            f.unlink()
        except OSError:
            pass


def prune(keep: int = PARK_KEEP) -> list[str]:
    """Keep the newest `keep` parked chats (by file time), delete the rest.
    Returns the session ids dropped."""
    try:
        bins = sorted(PARK_DIR.glob("*.bin"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []
    gone = []
    for f in bins[keep:]:
        _drop(f.stem)
        gone.append(f.stem)
    return gone


def begin_turn(sid: str, ephemeral: bool) -> dict | None:
    """Call before a turn's first model call. Restores this chat's parked
    state when the slot no longer holds it; returns {"n": tokens, "s": secs}
    when it did, else None. Off-the-record chats are never restored (they are
    never parked); the slot is simply marked as theirs."""
    try:
        if ephemeral or not _SAFE.match(sid or ""):
            return None
        return park_restore(sid)
    except Exception as e:
        _log("restore-crash", f"restore failed quietly: {type(e).__name__}: {e}")
        return None
    finally:
        # The turn about to run puts this chat on the slot. Its fingerprint
        # is unknown until the save after it (a skipped save must not make
        # the next turn "restore" an older file over a newer slot).
        with _LOCK:
            _HOLDER.update(sid=sid, fp=None)


def park_restore(sid: str) -> dict | None:
    w = _where()
    if not w:
        return None
    root, n = w
    binf, side = _files(sid)
    if not binf.exists():
        return None
    with _LOCK:
        s = _slot(root, n)
        if not s or s.get("is_processing"):
            return None            # another chat is mid-turn on it: let it be
        held = _HOLDER["sid"] == sid and _HOLDER["fp"] in (None, _fp(s))
        if held:
            return None            # still there: nothing to read
        try:
            meta = json.loads(side.read_text())
        except (OSError, ValueError):
            meta = {}
        # 2026-10-30: _HOLDER lives in memory and a dashboard restart forgets
        # it, so the slot that STILL held this chat (live, with checkpoints)
        # was erased and the file loaded over it — and the next turn re-read
        # ~47k tokens. The sidecar keeps the slot's (id_task, n_prompt_tokens)
        # from save time; anything run on the slot since changes id_task.
        if _same_fp(meta.get("fp"), _fp(s)):
            _HOLDER.update(sid=sid, fp=_fp(s))
            return None            # the slot never lost it: keep it, read nothing
        now = _model(root)
        if not now or any(meta.get(k) != now.get(k) for k in ("model", "build", "n_ctx")):
            _drop(sid)             # another brain's memory: stale, never load it
            _log(f"stale-{sid}", f"{sid}: parked file was for another model/build/context; deleted")
            return None
        import httpx
        t0 = time.time()
        try:
            # Erase first: a restore loads the main model's state only, not the
            # MTP draft's, which would keep the OTHER chat's positions and
            # refuse the next step's ("inconsistent sequence positions").
            # Erased, the draft starts clean from where her chat resumes.
            httpx.post(f"{root}/slots/{n}?action=erase", json={}, timeout=60.0)
            r = httpx.post(f"{root}/slots/{n}?action=restore",
                           json={"filename": binf.name}, timeout=300.0)
            d = r.json() if r.status_code == 200 else None
        except Exception as e:
            _log("restore-err", f"restore did not answer: {e}")
            return None
        if not d or not d.get("n_restored"):
            if "slot-save-path" in r.text:
                _LIVE.pop(root, None)
            else:
                _drop(sid)         # a file the server will not read is no use
            _log(f"restore-no-{sid}", f"restore refused ({r.status_code}): {r.text[:200]}")
            return None
        secs = time.time() - t0
        try:
            os.utime(binf)         # used = recent, for PARK_KEEP
        except OSError:
            pass
        return {"n": int(d["n_restored"]), "s": secs}


def park_save(sid: str, ephemeral: bool = False, still_mine=lambda: True) -> dict | None:
    """Call after a turn, off the turn's thread. Saves her chat slot to
    <sid>.bin when it still holds this chat. `still_mine()` False (a new turn
    on this chat already started) skips it: that turn will save after."""
    try:
        if ephemeral or not _SAFE.match(sid or ""):
            return None
        w = _where()
        if not w:
            return None
        root, n = w
        with _LOCK:
            if _HOLDER["sid"] != sid or not still_mine():
                return None        # the slot moved on to another chat
            s = _slot(root, n)
            if not s or s.get("is_processing") or not s.get("n_prompt_tokens"):
                return None
            meta = _model(root)
            if not meta:
                return None
            import httpx
            binf, side = _files(sid)
            t0 = time.time()
            r = httpx.post(f"{root}/slots/{n}?action=save",
                           json={"filename": binf.name}, timeout=300.0)
            if r.status_code != 200:
                if "slot-save-path" in r.text:
                    _LIVE.pop(root, None)
                else:
                    _DEAD["why"] = f"save refused ({r.status_code}): {r.text[:200]}"
                _log("save-no", f"parking off: save refused ({r.status_code}): {r.text[:200]}")
                return None
            d = r.json()
            if not binf.exists():
                _DEAD["why"] = f"saved file not in {PARK_DIR}"
                _log("save-where", f"parking off: the brain saved somewhere other than {PARK_DIR}")
                return None
            # fp: the slot as /slots showed it just before the save — what
            # park_restore compares (2026-10-30). n_saved counts generated
            # tokens too, n_prompt_tokens does not, so _HOLDER uses _fp(s).
            side.write_text(json.dumps({**meta, "n_tokens": d.get("n_saved"),
                                        "fp": list(_fp(s)),
                                        "saved": time.time()}))
            _HOLDER.update(sid=sid, fp=_fp(s))
        prune()
        return {"n": d.get("n_saved"), "bytes": d.get("n_written"), "s": time.time() - t0}
    except Exception as e:
        _log("save-crash", f"save failed quietly: {type(e).__name__}: {e}")
        return None
