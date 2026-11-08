"""Run desktop tooling with all mutable toolchains and caches inside the workspace."""
from pathlib import Path
import os
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def environment():
    env=os.environ.copy()
    paths={'RUSTUP_HOME':'.runtime/rustup','CARGO_HOME':'.runtime/cargo','CARGO_TARGET_DIR':'.cache/cargo-target',
           'npm_config_cache':'.cache/npm','npm_config_userconfig':'.runtime/config/npmrc',
           'CLANG_MODULE_CACHE_PATH':'.cache/clang','TMPDIR':'.tmp'}
    for key,relative in paths.items():
        path=(ROOT/relative).resolve()
        if not path.is_relative_to(ROOT):raise ValueError('Desktop path escaped workspace')
        if key=='npm_config_userconfig':
            path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():path.write_text('audit=false\nfund=false\n')
        else:path.mkdir(parents=True,exist_ok=True)
        env[key]=str(path)
    env['PATH']=str(ROOT/'.runtime/cargo/bin')+os.pathsep+env.get('PATH','')
    return env


if __name__=='__main__':
    command=sys.argv[1:]
    if command[:1]==['--']:command=command[1:]
    if not command:raise SystemExit('Usage: python3 scripts/desktop.py -- npm ...')
    raise SystemExit(subprocess.run(command,cwd=ROOT/'desktop',env=environment(),check=False).returncode)
