"""Standard-library storage contract shared by the launcher and application."""
from pathlib import Path
import tomllib

DEFAULTS = {'data_dir': 'data', 'models_dir': '.models', 'cache_dir': '.cache'}
PROTECTED = {'.runtime', '.venv', '.git', '.tmp', '.agents', '.codex', 'src', 'docs',
             'scripts', 'tests', 'skills', 'desktop', 'dist', 'build'}
CACHE_ENV = {'HF_HOME': 'huggingface', 'HF_HUB_CACHE': 'huggingface/hub',
             'HF_XET_CACHE': 'huggingface/xet', 'HF_ASSETS_CACHE': 'huggingface/assets',
             'TORCH_HOME': 'torch', 'NUMBA_CACHE_DIR': 'numba', 'MPLCONFIGDIR': 'matplotlib',
             'CLANG_MODULE_CACHE_PATH': 'clang', 'XDG_CACHE_HOME': '.'}


def storage_roots(root: Path, overrides: dict | None = None) -> dict[str, Path]:
    if overrides and set(overrides)-set(DEFAULTS):raise ValueError('Unknown storage setting')
    root = root.resolve(strict=True)
    config = (root / 'sonafolio.toml').resolve()
    if not config.is_relative_to(root):
        raise ValueError('Configuration must remain inside the workspace')
    values = {**DEFAULTS, **(tomllib.loads(config.read_text()).get('storage', {}) if config.is_file() else {}), **(overrides or {})}
    paths = {}
    for key, default in DEFAULTS.items():
        value = values[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{key} requires a workspace-relative directory')
        value = value.strip(); relative = Path(value)
        if relative.is_absolute() or not relative.parts or '..' in relative.parts or ':' in value or '\\' in value:
            raise ValueError(f'{key} must remain relative to the workspace')
        forbidden = PROTECTED | (set(DEFAULTS.values()) - {default})
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target == root or relative.parts[0] in forbidden:
            raise ValueError(f'{key} overlaps a protected directory or escapes the workspace')
        for protected in forbidden:
            other = (root / protected).resolve()
            if target == other or target in other.parents or other in target.parents:
                raise ValueError(f'{key} overlaps a protected directory')
        if target.exists() and not target.is_dir():
            raise ValueError(f'{key} must be a directory')
        paths[key] = target
    for key, path in paths.items():
        if any(path == other or path in other.parents or other in path.parents for name, other in paths.items() if key != name):
            raise ValueError('Data, model and cache directories must not overlap')
    return paths
