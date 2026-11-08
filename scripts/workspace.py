"""Configure project-local runtime directories before starting child processes.

Standard library only; this is an environment launcher, not a filesystem sandbox.
"""

from __future__ import annotations

import argparse
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]

DIRECTORIES = {
    "SONAFOLIO_WORKSPACE": ".",
    "SONAFOLIO_MODELS_DIR": ".models",
    "SONAFOLIO_DATA_DIR": "data",
    "UV_PROJECT_ENVIRONMENT": ".venv",
    "UV_CACHE_DIR": ".cache/uv",
    "UV_PYTHON_INSTALL_DIR": ".runtime/python",
    "UV_PYTHON_CACHE_DIR": ".cache/python",
    "UV_PYTHON_BIN_DIR": ".runtime/bin",
    "UV_TOOL_DIR": ".runtime/tools",
    "UV_TOOL_BIN_DIR": ".runtime/bin",
    "PIP_CACHE_DIR": ".cache/pip",
    "HF_HOME": ".cache/huggingface",
    "HF_HUB_CACHE": ".cache/huggingface/hub",
    "HF_XET_CACHE": ".cache/huggingface/xet",
    "HF_ASSETS_CACHE": ".cache/huggingface/assets",
    "TORCH_HOME": ".cache/torch",
    "NUMBA_CACHE_DIR": ".cache/numba",
    "MPLCONFIGDIR": ".cache/matplotlib",
    "CLANG_MODULE_CACHE_PATH": ".cache/clang",
    "XDG_CACHE_HOME": ".cache",
    "XDG_CONFIG_HOME": ".runtime/config",
    "XDG_DATA_HOME": ".runtime/share",
    "XDG_STATE_HOME": ".runtime/state",
    "TMPDIR": ".tmp",
    "TMP": ".tmp",
    "TEMP": ".tmp",
}


def local_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Runtime path escapes workspace: {relative}")
    return target


def build_environment(root: Path, parent: dict[str, str], *, include_venv: bool = True) -> dict[str, str]:
    if sys.version_info < (3,11):
        raise ValueError('Workspace launcher requires Python 3.11+; the application bundles Python 3.12')
    env = parent.copy()
    for key in (
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX",
        "UV_PYTHON", "UV_CONFIG_FILE", "UV_WORKING_DIRECTORY", "UV_PROJECT",
        "UV_ENV_FILE", "UV_BREAK_SYSTEM_PACKAGES", "UV_SYSTEM_PYTHON",
        "UV_TARGET", "UV_PREFIX", "UV_PIP_TARGET", "UV_PIP_PREFIX",
        "PIP_TARGET", "PIP_PREFIX",
        "PIP_USER", "PIP_REQUIREMENT", "PIP_CONSTRAINT",
    ):
        env.pop(key, None)
    # Validate all paths before creating anything.
    contract = Path(__file__).resolve().parent / 'storage_config.py'
    if not contract.exists(): contract = ROOT / 'src/sonafolio/storage_config.py'
    spec = importlib.util.spec_from_file_location('sonafolio_storage_config', contract)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    storage = module.storage_roots(root)
    paths = {key: local_path(root, value) for key, value in DIRECTORIES.items()}
    for key, value in DIRECTORIES.items():
        if value == '.cache' or value.startswith('.cache/'):
            paths[key] = storage['cache_dir'] / Path(value).relative_to('.cache')
    paths['SONAFOLIO_MODELS_DIR'] = storage['models_dir']
    paths['SONAFOLIO_DATA_DIR'] = storage['data_dir']
    token = local_path(root, ".runtime/secrets/huggingface-token")
    venv_bin = local_path(root, ".venv/Scripts" if os.name == "nt" else ".venv/bin")
    for path in [*paths.values(), token.parent, *([venv_bin] if include_venv else [])]:
        path.mkdir(parents=True, exist_ok=True)
    for path in storage.values():
        path.mkdir(parents=True,exist_ok=True)
        ignore=path/'.gitignore'
        if not ignore.exists():ignore.write_text('# Sonafolio private storage\n*\n')
    env.update({key: str(path) for key, path in paths.items()})
    env.update({
        "HF_TOKEN_PATH": str(token),
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYANNOTE_METRICS_ENABLED": "0",
        "DO_NOT_TRACK": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_REQUIRE_VIRTUALENV": "1",
        "UV_NO_CONFIG": "1",
        "UV_MANAGED_PYTHON": "1",
        "UV_PYTHON_INSTALL_BIN": "0",
        "UV_PYTHON_DOWNLOADS": "manual",
    })
    env["PATH"] = os.pathsep.join([
        *([str(venv_bin)] if include_venv else []), str(paths["UV_TOOL_BIN_DIR"]), parent.get("PATH", "")
    ])
    return env


def resolve_command(root: Path, command: list[str]) -> list[str]:
    name, *args = command
    if name in {"python", "python3", "pip", "pip3"}:
        relative = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
        # Validate the symlink destination, but execute the venv path itself:
        # resolving it to the base interpreter would bypass the virtualenv.
        local_path(root, relative)
        executable = root.resolve() / relative
        if name in {"pip", "pip3"}:
            args = ["-m", "pip", *args]
    elif name in {"uv", "uv.exe"}:
        executable = local_path(root, ".runtime/bin/uv.exe" if os.name == "nt" else ".runtime/bin/uv")
    elif name == "sonafolio":
        return [*resolve_command(root, ["python"]), "-m", "sonafolio", *args]
    else:
        return command
    if not executable.is_file():
        raise ValueError(f"Workspace executable not installed: {executable}")
    return [str(executable), *args]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Create and show local runtime directories; no downloads")
    run = sub.add_parser("run", help="Launch a command with workspace-local environment")
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = getattr(args, "command", [])
    if command[:1] == ["--"]:
        command = command[1:]
    if args.action == "run" and not command:
        parser.error("run requires a command after --")
    try:
        env = build_environment(ROOT, dict(os.environ))
        if args.action == "doctor":
            report = {key: env[key] for key in DIRECTORIES}
            report["HF_TOKEN_PATH"] = env["HF_TOKEN_PATH"]
            print(json.dumps({
                "status": "environment-launcher-only",
                "downloads_performed": False,
                "paths": report,
            }, ensure_ascii=False, indent=2))
            return 0
        command = resolve_command(ROOT, command)
        return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    except (OSError, ValueError) as error:
        print(f"Sonafolio workspace: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
