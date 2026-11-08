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


def workspace_path(relative: str | Path) -> Path:
    root = workspace_root()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Output path must remain inside the workspace: {relative}")
    return candidate


def writable_path(relative: str | Path) -> Path:
    root = workspace_root()
    candidate = workspace_path(relative)
    # Keep all generated media/results separate from the read-only inputs.
    inputs = (root / "data/input").resolve()
    if candidate == inputs or inputs in candidate.parents:
        raise ValueError("Generated files must not overwrite the input directory")
    # Managed original audio is immutable through every generated-file API.
    for parent in candidate.parents:
        if parent.name == "input" and (parent.parent / "manifest.json").is_file():
            raise ValueError("Generated files must not overwrite managed task inputs")
    return candidate


def write_json(path: Path, value: dict | list) -> None:
    path = writable_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".tmp")
    # Resolve the temporary output as well, rejecting pre-existing escaping symlinks.
    pending = writable_path(pending)
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    pending.replace(path)


def model_path(relative: str | Path) -> Path:
    """Resolve conventional .models paths through the active storage setting."""
    from .storage_config import storage_roots
    path = Path(relative)
    if not path.is_absolute() and path.parts and path.parts[0] == '.models':
        path = storage_roots(workspace_root())['models_dir'] / Path(*path.parts[1:])
    return writable_path(path)


def apply_cache_environment() -> None:
    from .storage_config import storage_roots, CACHE_ENV
    roots = storage_roots(workspace_root())
    for key, relative in CACHE_ENV.items():
        path = writable_path(roots['cache_dir'] / relative)
        path.mkdir(parents=True, exist_ok=True); os.environ[key] = str(path)
    os.environ['SONAFOLIO_MODELS_DIR'] = str(roots['models_dir'])
    os.environ['SONAFOLIO_DATA_DIR'] = str(roots['data_dir'])
