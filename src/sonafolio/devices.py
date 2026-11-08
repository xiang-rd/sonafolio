"""Probe optional acceleration in a short-lived process, keeping IPC lightweight."""
from functools import lru_cache
import json
import os
import subprocess
import sys
import tomllib
from .paths import workspace_root


@lru_cache(maxsize=1)
def mps_available() -> bool:
    if sys.platform != 'darwin':return False
    env=dict(os.environ)
    for key in ('HF_TOKEN','HUGGING_FACE_HUB_TOKEN','DEEPSEEK_API_KEY'):env.pop(key,None)
    try:
        result=subprocess.run([sys.executable,'-B','-c',
            'import json,torch; print(json.dumps({"mps":torch.backends.mps.is_available()}))'],
            cwd=workspace_root(),env=env,capture_output=True,text=True,timeout=30,check=False)
        return result.returncode==0 and json.loads(result.stdout.strip())['mps'] is True
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):return False


def speaker_device(requested: str) -> str:
    if requested not in {'auto','cpu','mps','cuda'}:raise ValueError('Unknown speaker device')
    return ('mps' if mps_available() else 'cpu') if requested=='auto' else requested


def configured_speaker_device() -> str:
    path=workspace_root()/'sonafolio.toml'
    value=tomllib.loads(path.read_text()).get('processing',{}).get('speaker_device','auto') if path.is_file() else 'auto'
    if value not in {'auto','cpu','mps'}:raise ValueError('Invalid processing.speaker_device')
    return value
