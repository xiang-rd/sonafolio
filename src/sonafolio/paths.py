"""Resolve writable paths without trusting the caller's working directory."""

from __future__ import annotations

import json
import os
from pathlib import Path


def workspace_root() -> Path:
    configured = os.environ.get("SONAFOLIO_WORKSPACE")
    if not configured:
        raise RuntimeError("Use scripts/workspace.py run -- sonafolio ... to set the workspace")
    return Path(configured).resolve(strict=True)


def writable_path(relative: str | Path) -> Path:
    root = workspace_root()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Output path must remain inside the workspace: {relative}")
    # Keep all generated media/results separate from the read-only inputs.
    inputs = (root / "data/input").resolve()
    if candidate == inputs or inputs in candidate.parents:
        raise ValueError("Generated files must not overwrite the input directory")
    return candidate


def write_json(path: Path, value: dict | list) -> None:
    path = writable_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    # Resolve the temporary output as well, rejecting pre-existing escaping symlinks.
    pending = writable_path(pending)
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pending.replace(path)
