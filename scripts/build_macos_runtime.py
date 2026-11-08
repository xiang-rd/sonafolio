"""Build a relocatable, model-free macOS Python runtime from uv.lock."""
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def build():
    if sys.platform!='darwin' or platform.machine()!='arm64':
        raise SystemExit('This runtime build currently targets macOS arm64 only')
    if '--refresh-code' in sys.argv:
        destination=ROOT/'build/macos-runtime'
        manifest=json.loads((destination/'runtime.json').read_text())
        if manifest['lock_sha256']!=hashlib.sha256((ROOT/'uv.lock').read_bytes()).hexdigest():
            raise SystemExit('Dependency lock changed; rebuild the complete runtime')
        subprocess.run([str(ROOT/'.runtime/bin/uv'),'build','--wheel','--offline'],cwd=ROOT,check=True)
        wheel=next((ROOT/'dist').glob('sonafolio-0.1.0-*.whl'))
        subprocess.run([str(ROOT/'.runtime/bin/uv'),'pip','install','--python',str(destination/'python/bin/python3'),
            '--target',str(destination/'python/lib/python3.12/site-packages'),'--reinstall','--no-deps','--offline',str(wheel)],cwd=ROOT,check=True)
        for filename in ('direct_url.json','uv_cache.json'):
            for path in (destination/'python/lib/python3.12/site-packages').glob('*.dist-info/'+filename):path.unlink()
        for path in (destination/'python').rglob('.DS_Store'):path.unlink()
        for name,source in [('launch.py','runtime_launch.py'),('workspace_env.py','workspace.py')]:
            shutil.copy2(ROOT/'scripts'/source,destination/name)
        manifest['wheel_sha256']=hashlib.sha256(wheel.read_bytes()).hexdigest()
        (destination/'runtime.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print('Refreshed first-party code; dependency runtime unchanged')
        return
    base=Path(sys.base_prefix).resolve()
    if not base.is_relative_to(ROOT/'.runtime/python'):
        raise SystemExit('Use scripts/workspace.py run -- python scripts/build_macos_runtime.py')
    build_root=ROOT/'build'
    stage=build_root/'macos-runtime.pending'
    destination=build_root/'macos-runtime'
    if stage.exists():shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(base,stage/'python',symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.DS_Store'))
    python=stage/'python/bin/python3'
    site=stage/'python/lib/python3.12/site-packages'
    site.mkdir(parents=True,exist_ok=True)
    uv=ROOT/'.runtime/bin/uv'
    requirements=stage/'requirements.lock'
    subprocess.run([str(uv),'export','--all-extras','--no-dev','--no-emit-project','--locked','--offline','--format','requirements-txt','--output-file',str(requirements)],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
    subprocess.run([str(uv),'pip','install','--python',str(python),'--target',str(site),'--require-hashes','--no-compile-bytecode',* ([] if '--online' in sys.argv else ['--offline']),'-r',str(requirements)],cwd=ROOT,check=True)
    subprocess.run([str(uv),'build','--wheel','--offline'],cwd=ROOT,check=True)
    wheel=next((ROOT/'dist').glob('sonafolio-0.1.0-*.whl'))
    subprocess.run([str(uv),'pip','install','--python',str(python),'--target',str(site),'--no-deps','--offline',str(wheel)],cwd=ROOT,check=True)
    from macos_native import vendor_codec
    vendor_codec(site)
    for filename in ('direct_url.json','uv_cache.json'):
        for path in site.glob('*.dist-info/'+filename):path.unlink()
    for path in stage.rglob('.DS_Store'):path.unlink()
    shutil.copy2(ROOT/'scripts/workspace.py',stage/'workspace_env.py')
    shutil.copy2(ROOT/'scripts/runtime_launch.py',stage/'launch.py')
    (stage/'runtime.json').write_text(json.dumps({'schema_version':1,'platform':'macos-arm64',
        'python':platform.python_version(),'lock_sha256':hashlib.sha256((ROOT/'uv.lock').read_bytes()).hexdigest(),
        'app_version':'0.1.0','wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'models_bundled':False},indent=2)+'\n')
    # Reject symlinks tied to this build machine; internal relative links are portable.
    for path in stage.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(stage.resolve()):
            raise RuntimeError(f'Runtime symlink escapes bundle: {path.relative_to(stage)}')
    previous=build_root/'macos-runtime.previous'
    if previous.exists():shutil.rmtree(previous)
    if destination.exists():destination.rename(previous)
    stage.rename(destination)
    if previous.exists():shutil.rmtree(previous)
    print(f'Built macOS runtime: {destination}')


if __name__=='__main__':build()
