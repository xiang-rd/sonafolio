"""Copy a user-provided uv into the workspace and build the local environment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from workspace import ROOT, build_environment, local_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, help="Existing uv executable (read-only source)")
    parser.add_argument("--asr", action="store_true", help="Install the local ASR adapter")
    parser.add_argument("--diarization", action="store_true", help="Install speaker diarization dependencies")
    parser.add_argument("--alignment", action="store_true", help="Install Chinese CTC alignment dependencies")
    args = parser.parse_args()
    env = build_environment(ROOT, dict(os.environ))
    source = args.uv or shutil.which("uv")
    if not source:
        parser.error("Install uv first or provide --uv /path/to/uv")
    source = Path(source).resolve(strict=True)
    uv = local_path(ROOT, ".runtime/bin/uv.exe" if sys.platform == "win32" else ".runtime/bin/uv")
    if source != uv:
        # Homebrew binaries can be read-only. Replace our copy atomically rather
        # than opening it for writing, leaving the user's installation untouched.
        pending = local_path(ROOT, ".runtime/bin/uv.bootstrap.tmp")
        shutil.copy2(source, pending)
        pending.replace(uv)
    version = subprocess.check_output([str(uv), "--version"], env=env, cwd=ROOT, text=True).strip()
    print(f"Workspace tool: {version}", flush=True)
    python_version = (ROOT / ".python-version").read_text().strip()
    commands = [
        ["python", "install", python_version, "--no-progress"],
        ["venv", "--allow-existing", "--python", python_version, str(ROOT / ".venv")],
        ["sync", "--python", python_version, "--no-progress", "--no-build-package", "pywhispercpp"],
    ]
    if args.asr:
        commands[-1] += ["--extra", "asr"]
    if args.diarization:
        commands[-1] += ["--extra", "diarization"]
    if args.alignment:
        commands[-1] += ["--extra", "alignment"]
    if (ROOT / "uv.lock").is_file():
        commands[-1].append("--locked")
    for command in commands:
        subprocess.run([str(uv), *command], cwd=ROOT, env=env, check=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Sonafolio bootstrap: {error}", file=sys.stderr)
        raise SystemExit(1)
