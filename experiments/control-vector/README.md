# Control-vector experiment — "answer-the-question" (ARCHIVED, not in use)

An attempt to fix Merge's trained-in habit of answering *"what do experts
believe?"* when asked *"what's the hard evidence?"* (the `jesus-hard-evidence`
probe) using **activation steering** — a control vector applied per-token at
inference, tunable and reversible, unlike abliteration.

## Files
- `answer-the-question.gguf` — the steering vector, built 2026-08-12 from 28
  contrastive pairs (straight-answer vs consensus-dodge).
- `answer-the-question.recipe.py` — the dataset builder that generated the
  positive/negative pairs the vector was distilled from.

## Why it's archived (the honest result)
Titrated 2026-08-13. With a **short** system prompt, a dose of ~1.0 flips her
cleanly to an honest "No." But under her **full ~1386-word production prompt**,
no coherent dose works: at 1.0–1.5 she still says "Yes"; at 2.0+ she degenerates
into empty output. Conclusion: **a steering vector can't overcome a prompt that
long.** The real lever is prompt architecture (a short, prominent honesty
directive), not this vector.

## To revive it (for experiments)
`llama-server`'s loader still works:
`--control-vector-scaled answer-the-question.gguf:1.0` (optionally with
`--control-vector-layer-range`). Note the builder tool
(`llama-cvector-generator`) needed an off-by-one patch for this model
(captured 64 l_out tensors, wanted 63) — see the merge project log for the
rebuild recipe.

Kept because the *idea* is sound and the dataset is good; it just lost to a very
long prompt. Regression story lives in the merge project memory.
