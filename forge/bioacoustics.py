"""Bioacoustics groundwork — is there STRUCTURE in animal sound?

Honest scope: this does NOT translate or decode meaning. It answers two
testable questions about a recording of animal vocalizations:

  1. REPERTOIRE — do the sounds break into a few recurring call-types, or is
     everything unique? (segment into units -> embed each -> cluster.)
  2. PROTO-SYNTAX — do those call-types come in NON-RANDOM order? Real language
     has grammar; if the sequence of calls is more predictable than shuffled
     versions of the same calls, that's a testable fingerprint of structure —
     tested against a null so we don't fool ourselves (the market-rig rule,
     pointed at animals).

Reuses the audio nerve's embedding for the per-call vectors. numpy + scipy +
PIL only. Render lets Merge SEE the repertoire, the call timeline, and the
transition "grammar".
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.signal import stft

from . import audio_nerve as AN

RENDERS = Path.home() / "forge" / "datasets" / "bioacoustics"
BG = (13, 16, 23)
INK = (230, 236, 245)
CYAN = (59, 214, 228)
# distinct call-type colors
PALETTE = [(95, 227, 136), (245, 174, 61), (225, 85, 154), (155, 141, 245),
           (59, 214, 228), (255, 138, 101), (124, 196, 84), (240, 200, 90),
           (200, 120, 255), (120, 200, 255), (255, 120, 120), (160, 220, 160)]


# ---- segmentation: split a recording into call units -------------------
def _segment(sig, sr, min_ms=60, gap_ms=80):
    """Find discrete vocalizations by energy. Returns [(start, end), ...] in
    samples. Frames above an adaptive threshold, merged across short gaps."""
    hop = int(sr * 0.010)
    win = int(sr * 0.025)
    n = 1 + (len(sig) - win) // hop if len(sig) > win else 0
    if n <= 0:
        return []
    energy = np.array([np.sqrt(np.mean(sig[i * hop:i * hop + win] ** 2))
                       for i in range(n)])
    thr = energy.mean() + 0.6 * energy.std()
    thr = max(thr, energy.max() * 0.12)
    active = energy > thr
    # merge across gaps
    gap = int(gap_ms / 10)
    units, i = [], 0
    while i < n:
        if active[i]:
            j = i
            silence = 0
            while j < n and silence <= gap:
                silence = silence + 1 if not active[j] else 0
                j += 1
            s, e = i * hop, min(len(sig), j * hop)
            if (e - s) >= sr * min_ms / 1000:
                units.append((s, e))
            i = j
        else:
            i += 1
    return units


# ---- call embedding: capture the TIME×FREQUENCY SHAPE ------------------
# The audio nerve's embedding is time-AVERAGED (built for sustained/musical
# sounds), so a rising chirp and a falling chirp look identical to it. Animal
# calls are distinguished by their CONTOUR, so a call fingerprint here is a
# small low-res log-spectrogram "sketch" (freq × time) that preserves shape —
# a sweep up vs a sweep down are visibly different sketches.
def _call_embed(sig, F=12, T=8):
    if len(sig) < 64:
        sig = np.pad(sig, (0, 64 - len(sig)))
    nper = int(np.clip(len(sig) // 8, 64, 256))
    _, _, Z = stft(sig, AN.SR, nperseg=nper, noverlap=nper // 2)
    S = np.log1p(np.abs(Z))                        # freq × frames
    fi = np.linspace(0, S.shape[0], F + 1).astype(int)
    ti = np.linspace(0, S.shape[1], T + 1).astype(int)
    grid = np.zeros((F, T))
    for a in range(F):
        for b in range(T):
            blk = S[fi[a]:fi[a + 1], ti[b]:ti[b + 1]]
            grid[a, b] = blk.mean() if blk.size else 0.0
    v = grid.ravel()
    v = v - v.mean()                               # contrast, not absolute level
    return (v / (np.linalg.norm(v) or 1)).astype(np.float32)


# ---- repertoire: cluster the units -------------------------------------
def _repertoire(vecs, max_types=12):
    """Agglomerative clustering with an automatic cut. Returns a label per
    unit (1..k). Falls back to 1 cluster if too few units."""
    if len(vecs) < 3:
        return np.ones(len(vecs), dtype=int), 1
    X = np.array(vecs)
    Z = linkage(X, method="ward")
    # auto cut: largest gap in merge distances (a simple, honest heuristic),
    # bounded to a sane number of types
    dists = Z[:, 2]
    if len(dists) >= 2:
        gaps = np.diff(dists)
        cut_at = len(dists) - 1 - int(np.argmax(gaps[-max_types:][::-1]))
        thresh = (dists[cut_at] + dists[min(cut_at + 1, len(dists) - 1)]) / 2
        labels = fcluster(Z, t=thresh, criterion="distance")
    else:
        labels = fcluster(Z, t=2, criterion="maxclust")
    # cap the number of types
    if labels.max() > max_types:
        labels = fcluster(Z, t=max_types, criterion="maxclust")
    return labels, int(labels.max())


# ---- sequence structure: is the order non-random? ----------------------
def _transition_matrix(seq, k):
    M = np.zeros((k, k))
    for a, b in zip(seq[:-1], seq[1:]):
        M[a - 1, b - 1] += 1
    return M


def _predictability(seq, k):
    """Mean conditional entropy of the next symbol given the current — LOW
    means order is predictable (structured). Bits."""
    M = _transition_matrix(seq, k)
    rows = M.sum(axis=1, keepdims=True)
    P = np.divide(M, rows, out=np.zeros_like(M), where=rows > 0)
    Ps = np.where(P > 0, P, 1.0)   # log2(1)=0, so zero-prob terms vanish
    ent = -(P * np.log2(Ps)).sum(axis=1)   # per-state entropy
    weight = rows.ravel() / (rows.sum() or 1)
    return float((ent * weight).sum())


def _structure_test(seq, k, trials=400, rng_seed_len=0):
    """Compare the real sequence's predictability to shuffled versions of the
    SAME call multiset. Lower real entropy than shuffles = real ordering
    structure. Returns (real, null_mean, p_value)."""
    if k < 2 or len(seq) < 8:
        return None
    real = _predictability(seq, k)
    arr = np.array(seq)
    # deterministic shuffles (no Math.random in this env at import; use a seeded
    # generator created HERE from the data length so runs are repeatable)
    rng = np.random.RandomState(1234 + len(seq))
    null = []
    for _ in range(trials):
        s = arr.copy(); rng.shuffle(s)
        null.append(_predictability(list(s), k))
    null = np.array(null)
    # p = fraction of shuffles at least as structured (as-low-entropy) as real
    p = float((null <= real).mean())
    return real, float(null.mean()), p


# ---- render: she sees the repertoire, timeline, and grammar ------------
def _render(sig, sr, units, labels, k, M, verdict, label):
    W, H = 900, 520
    img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
    d.text((12, 10), f"\U0001f9ec bioacoustics — {label}", fill=CYAN)

    # timeline of call units, colored by type
    d.text((12, 34), "CALL TIMELINE", fill=INK)
    d.text((12, 48), "each block = one call, colored by type", fill=(120, 132, 150))
    x0, x1, ty, th = 12, W - 12, 66, 42
    total = len(sig)
    d.rectangle([x0, ty, x1, ty + th], outline=(34, 42, 58))
    for (s, e), lab in zip(units, labels):
        a = x0 + (s / total) * (x1 - x0)
        b = x0 + (e / total) * (x1 - x0)
        col = PALETTE[(lab - 1) % len(PALETTE)]
        d.rectangle([a, ty, max(a + 1, b), ty + th], fill=col)

    # repertoire legend
    d.text((12, 124), f"REPERTOIRE — {k} recurring call-type(s), "
           f"{len(units)} calls total", fill=INK)
    counts = {i: int((labels == i).sum()) for i in range(1, k + 1)}
    lx = 12
    for i in range(1, k + 1):
        col = PALETTE[(i - 1) % len(PALETTE)]
        d.rectangle([lx, 144, lx + 14, 158], fill=col)
        d.text((lx + 20, 145), f"type {i} ×{counts[i]}", fill=(180, 190, 205))
        lx += 130
        if lx > W - 120:
            lx = 12

    # transition "grammar" graph
    d.text((12, 180), "SEQUENCE GRAMMAR", fill=INK)
    d.text((12, 194), "arrows = which call tends to follow which "
           "(thicker = more often)", fill=(120, 132, 150))
    gcx, gcy, gr = W // 2, 340, 110
    pos = {}
    for i in range(k):
        ang = -np.pi / 2 + i * 2 * np.pi / max(k, 1)
        pos[i + 1] = (gcx + gr * np.cos(ang), gcy + gr * np.sin(ang))
    if M is not None and M.sum() > 0:
        mx = M.max()
        for a in range(1, k + 1):
            for b in range(1, k + 1):
                w = M[a - 1, b - 1]
                if w <= 0:
                    continue
                pa, pb = pos[a], pos[b]
                width = 1 + int(3 * w / mx)
                d.line([pa, pb], fill=(40, 90, 110), width=width)
    for i in range(1, k + 1):
        px, py = pos[i]
        col = PALETTE[(i - 1) % len(PALETTE)]
        d.ellipse([px - 12, py - 12, px + 12, py + 12], fill=col)
        d.text((px - 3, py - 6), str(i), fill=(10, 14, 20))

    # verdict
    d.text((12, 470), verdict, fill=INK)
    RENDERS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", label)[:40]
    out = RENDERS / f"{safe}.png"
    img.save(out)
    return out


def study_calls(source: str) -> str:
    """Look for STRUCTURE in a recording of animal (or any) vocalizations:
    segment it into calls, cluster them into a repertoire of recurring types,
    and test whether the SEQUENCE of calls is non-random (a testable fingerprint
    of proto-syntax). This does NOT decode meaning — it finds whether there's a
    system there. `source` is an audio file path or a synth spec. Renders a
    picture (timeline + repertoire + grammar) and returns its path + verdict."""
    sig, label = AN._load(source, max_sec=30.0)
    if sig is None:
        return f"Couldn't hear that: {label}"
    sig = sig / (np.abs(sig).max() or 1)
    sr = AN.SR

    units = _segment(sig, sr)
    if len(units) < 3:
        return (f"Only found {len(units)} distinct call(s) in {label} — need "
                f"more vocalizations to look for structure. (Is this a "
                f"continuous sound rather than discrete calls?)")

    vecs = [_call_embed(sig[s:e]) for (s, e) in units]
    labels, k = _repertoire(vecs)
    seq = list(labels)
    M = _transition_matrix(seq, k)
    st = _structure_test(seq, k)

    if st is None:
        verdict = (f"{len(units)} calls, {k} type(s) — too few to test ordering "
                   "yet. Structure test needs ~8+ calls and 2+ types.")
        struct_line = verdict
    else:
        real, nullm, p = st
        ordered = p < 0.05
        struct_line = (
            f"Order is {'NON-RANDOM (structured)' if ordered else 'not distinguishable from random'}: "
            f"predictability {real:.2f} bits vs {nullm:.2f} shuffled, p={p:.3f}.")
        verdict = struct_line

    out = _render(sig, sr, units, labels, k, M, struct_line[:110], label)

    lines = [f"Studied {label}: {len(units)} calls → {k} recurring call-type(s)."]
    lines.append(struct_line)
    if st and st[2] < 0.05:
        lines.append("→ There IS non-random ordering here — a testable fingerprint "
                     "of structure (NOT meaning). Worth a closer look.")
    elif st:
        lines.append("→ No ordering structure beyond chance in this sample. Either "
                     "there's none, or we need more/cleaner calls.")
    lines.append(f"Rendered to {out} — look_at_image it to SEE the repertoire, the "
                 "call timeline, and the transition grammar.")
    lines.append("Honest limit: this finds STRUCTURE, never meaning. It cannot "
                 "tell you what a call says.")
    return "\n".join(lines)
