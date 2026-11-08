"""Workspace-owned desktop preferences; credentials are write-only over IPC."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import tomllib

from .llm import LLMSettings, load_settings
from .paths import workspace_root, writable_path, apply_cache_environment
from .storage_config import storage_roots
from .tasks import data_root
from .devices import configured_speaker_device


def get_settings():
    root=workspace_root()
    key=writable_path('.runtime/secrets/deepseek-api-key')
    hf=writable_path('.runtime/secrets/huggingface-token')
    roots=storage_roots(root)
    from .model_manager import model_catalog
    catalog=model_catalog()
    ready={item['id']:item['ready'] for item in catalog['items']}
    return {'workspace':str(root),'llm':load_settings().model_dump(),'api_key_configured':key.is_file() and key.stat().st_size>0,
            'hf_token_configured':hf.is_file() and hf.stat().st_size>0,
            'model_files':{'asr':ready['asr'] and ready['vad'],'diarization':ready['diarization'],'alignment':ready['alignment']},
            'processing':{'speaker_device':configured_speaker_device()},
            'storage':{name:path.relative_to(root).as_posix() for name,path in roots.items()},
            'appearance':tomllib.loads(writable_path('sonafolio.toml').read_text(encoding='utf-8')).get('desktop',{}).get('appearance','dark') if writable_path('sonafolio.toml').is_file() else 'dark',
            'paths':{name.removesuffix('_dir'):str(path) for name,path in roots.items()}}



def _table(name, values):
    lines=['['+name+']']
    for key,value in values.items():
        if not re.fullmatch('[A-Za-z0-9_-]+',key) or not isinstance(value,(str,int,float,bool)):
            raise ValueError('Unsupported configuration value')
        lines.append(key+' = '+json.dumps(value,ensure_ascii=False,allow_nan=False))
    return '\n'.join(lines)+'\n\n'


def save_settings(*, llm=None, data_dir=None, api_key=None, appearance=None, models_dir=None, cache_dir=None, hf_token=None, speaker_device=None):
    path=writable_path('sonafolio.toml')
    original=path.read_text(encoding='utf-8') if path.exists() else ''
    tables=tomllib.loads(original)
    updates={}
    if llm is not None:
        settings=LLMSettings.model_validate(llm)
        if settings.provider!='deepseek':raise ValueError('Only DeepSeek is currently available')
        updates['llm']={**tables.get('llm',{}),**settings.model_dump()}
    directories={key:value for key,value in {'data_dir':data_dir,'models_dir':models_dir,'cache_dir':cache_dir}.items() if value is not None}
    if directories:
        storage_roots(workspace_root(),directories)
        updates['storage']={**tables.get('storage',{}),**directories}
    if speaker_device is not None:
        if speaker_device not in {'auto','cpu','mps'}:raise ValueError('Unsupported speaker device')
        updates['processing']={**tables.get('processing',{}),'speaker_device':speaker_device}
    if appearance is not None:
        if appearance not in {'light','dark','system'}:raise ValueError('Unknown appearance')
        updates['desktop']={**tables.get('desktop',{}),'appearance':appearance}
    if api_key is not None and (len(api_key)<16 or any(c.isspace() for c in api_key)):
        raise ValueError('Expected a complete API key without whitespace')
    if hf_token is not None and (not hf_token.startswith('hf_') or len(hf_token)<16 or any(c.isspace() for c in hf_token)):
        raise ValueError('Expected a Hugging Face read token')
    for name in ('deepseek-api-key','huggingface-token'):
        if writable_path('.runtime/secrets/'+name)!=workspace_root()/'.runtime/secrets'/name:
            raise ValueError('Credential path must not be redirected through symlinks')
    secret=writable_path('.runtime/secrets/deepseek-api-key')
    if secret != workspace_root()/'.runtime/secrets/deepseek-api-key':
        raise ValueError('Credential path must not be redirected through symlinks')
    updated=original
    for name,values in updates.items():
        replacement=_table(name,values)
        pattern=re.compile(r'^\['+name+r'\][^\n]*\n.*?(?=^\[|\Z)',re.M|re.S)
        if pattern.search(updated):updated=pattern.sub(lambda _:replacement,updated,count=1)
        else:updated+='\n'+replacement
    tomllib.loads(updated)
    temporary=writable_path(path.with_suffix('.toml.tmp'))
    temporary.write_text(updated,encoding='utf-8');temporary.replace(path)
    for name,value in (('deepseek-api-key',api_key),('huggingface-token',hf_token)):
        if value is None: continue
        secret=writable_path('.runtime/secrets/'+name)
        secret.parent.mkdir(parents=True,exist_ok=True)
        temporary=writable_path(secret.with_name(secret.name+'.pending'))
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(value)
        temporary.chmod(0o600);temporary.replace(secret)
    if directories:
        apply_cache_environment()
        for name,root in storage_roots(workspace_root()).items():
            if name=='data_dir':continue
            root.mkdir(parents=True,exist_ok=True)
            ignore=writable_path(root/'.gitignore')
            if not ignore.exists():ignore.write_text('# Sonafolio private storage\n*\n')
    return get_settings()


def scene_catalog():
    from .scenes import list_scenes
    return list_scenes()
