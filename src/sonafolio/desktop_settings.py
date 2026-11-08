"""Workspace-owned desktop preferences; credentials are write-only over IPC."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import tomllib

from .llm import LLMSettings, load_settings
from .paths import workspace_root, writable_path
from .tasks import data_root


def get_settings():
    root=workspace_root()
    key=writable_path('.runtime/secrets/deepseek-api-key')
    return {'workspace':str(root),'llm':load_settings().model_dump(),'api_key_configured':key.is_file() and key.stat().st_size>0,
            'model_files':{'asr':writable_path('.models/whisper.cpp/ggml-large-v3-turbo-q5_0.bin').is_file(),
                           'diarization':writable_path('.models/pyannote-community-1/sonafolio-model.json').is_file(),
                           'alignment':writable_path('.models/chinese-alignment/sonafolio-model.json').is_file()},
            'storage':{'data_dir':str(data_root().relative_to(root)),'models_dir':'.models','cache_dir':'.cache'},
            'appearance':tomllib.loads(writable_path('sonafolio.toml').read_text(encoding='utf-8')).get('desktop',{}).get('appearance','dark') if writable_path('sonafolio.toml').is_file() else 'dark',
            'paths':{'data':str(data_root()),'models':str(root/'.models'),'cache':str(root/'.cache')}}


def _table(name, values):
    lines=['['+name+']']
    for key,value in values.items():
        if not re.fullmatch('[A-Za-z0-9_-]+',key) or not isinstance(value,(str,int,float,bool)):
            raise ValueError('Unsupported configuration value')
        lines.append(key+' = '+json.dumps(value,ensure_ascii=False,allow_nan=False))
    return '\n'.join(lines)+'\n\n'


def save_settings(*, llm=None, data_dir=None, api_key=None, appearance=None):
    path=writable_path('sonafolio.toml')
    original=path.read_text(encoding='utf-8') if path.exists() else ''
    tables=tomllib.loads(original)
    updates={}
    if llm is not None:
        settings=LLMSettings.model_validate(llm)
        if settings.provider!='deepseek':raise ValueError('Only DeepSeek is currently available')
        updates['llm']={**tables.get('llm',{}),**settings.model_dump()}
    if data_dir is not None:
        data_root(data_dir)
        updates['storage']={**tables.get('storage',{}),'data_dir':data_dir}
    if appearance is not None:
        if appearance not in {'light','dark','system'}:raise ValueError('Unknown appearance')
        updates['desktop']={**tables.get('desktop',{}),'appearance':appearance}
    if api_key is not None and (len(api_key)<16 or any(c.isspace() for c in api_key)):
        raise ValueError('Expected a complete API key without whitespace')
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
    if api_key is not None:
        secret.parent.mkdir(parents=True,exist_ok=True)
        temporary=writable_path(secret.with_name(secret.name+'.pending'))
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:stream.write(api_key)
        temporary.chmod(0o600);temporary.replace(secret)
    return get_settings()


def scene_catalog():
    from .scenes import list_scenes
    return list_scenes()
