from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from . import safety


def create_workspace(runs_root: Path, label: str = "") -> Path:
    """Create a brand-new private workspace. An existing path (dirty or not) is refused, never reused."""
    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = label or f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:10]}"
    if "/" in name or name in (".", ".."):
        raise safety.SafetyError("invalid workspace name")
    ws = runs_root / name
    try:
        os.mkdir(ws, 0o700)
    except FileExistsError:
        raise safety.SafetyError(f"workspace already exists, refusing to reuse: {ws}")
    safety.write_marker(ws)
    for sub in ("src", "results", "tmp"):
        os.mkdir(ws / sub, 0o700)
    return ws
