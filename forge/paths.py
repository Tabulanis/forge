"""Where Merge's own things live — one place, so they can move as one.

Her SOURCE is the package you are reading. Her SHELF is everything she has
accumulated: validated sims, cited datasets, the pulse feed, her research
renders. Those two were the same directory until 2026-09-08, which meant
adding a calculator to her shelf wrote into her own source tree, and a project
she was building could not be told apart from the builder.

She is the builder. MoneyLab, Storyweave, Hackbeat and the rest are what she
builds. Her shelf travels with her because it is hers; it does not belong
inside her code, and it does not belong to any one project either.

Everything reads these constants rather than composing its own path, so the
shelf can be moved again by editing this file alone.
"""
from __future__ import annotations

import os
from pathlib import Path

# Her state directory — settings, memory, and now the shelf.
STATE_DIR = Path(os.environ.get("FORGE_STATE_DIR", Path.home() / ".forge"))

# The shelf: what she has built and verified.
SHELF_DIR = STATE_DIR / "shelf"
SIMS_DIR = SHELF_DIR / "sims"
DATASETS_DIR = SHELF_DIR / "datasets"

# Where the shelf used to live, inside her source tree. Read-only fallback, so
# nothing she built before the move is lost if a copy was missed.
LEGACY_SIMS = Path.home() / "forge" / "sims"
LEGACY_DATASETS = Path.home() / "forge" / "datasets"


def shelf_path(new: Path, legacy: Path) -> Path:
    """The new location, unless only the old one has the file."""
    if new.exists():
        return new
    return legacy if legacy.exists() else new
