"""Explicit, resumable model downloads with pinned revisions and verified publication."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys
import time
import uuid
from .paths import workspace_root, writable_path, model_path, write_json, apply_cache_environment
from .storage_config import storage_roots
from .tasks import file_lock, now
from .processes import CancelledError, run_stage_process

CATALOG = {
    'asr': {'name':'中文语音识别', 'repository':'ggerganov/whisper.cpp', 'directory':'whisper.cpp',
            'files':['ggml-large-v3-turbo-q5_0.bin'], 'license':'MIT', 'gated':False},
    'vad': {'name':'语音活动检测', 'repository':'ggml-org/whisper-vad', 'directory':'whisper-vad',
            'files':['ggml-silero-v6.2.0.bin'], 'license':'MIT', 'gated':False},
    'diarization': {'name':'多说话人区分', 'repository':'pyannote/speaker-diarization-community-1', 'directory':'pyannote-community-1',
            'files':['config.yaml','embedding/pytorch_model.bin','segmentation/pytorch_model.bin','plda/plda.npz','plda/xvec_transform.npz'], 'license':'CC-BY-4.0', 'gated':True},
    'alignment': {'name':'中文时间对齐', 'repository':'jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn', 'directory':'chinese-alignment',
            'files':['config.json','preprocessor_config.json','vocab.json','pytorch_model.bin','special_tokens_map.json'], 'license':'Apache-2.0', 'gated':False},
}

class ModelError(RuntimeError):
    def __init__(self, code): self.code=code; super().__init__(code)


def definition(identifier):
    if identifier not in CATALOG: raise ValueError('Unknown model')
    return CATALOG[identifier]


def directory_for(identifier): return model_path(Path('.models')/definition(identifier)['directory'])


def entries_for(directory: Path, identifier: str):
    installation=directory/'installation.json'
    if installation.is_file(): return json.loads(installation.read_text())['files']
    manifest=directory/('model.json' if identifier in {'asr','vad'} else 'sonafolio-model.json')
    if not manifest.is_file(): return []
    value=json.loads(manifest.read_text())
    if identifier in {'asr','vad'}: return [{'name':value['filename'],'sha256':value['sha256']}]
    return value.get('checked_weight_files',[])


def checked_path(directory, name):
    relative=PurePosixPath(name)
    if relative.is_absolute() or '..' in relative.parts or '\\' in name: raise ValueError('Unsafe model file path')
    path=writable_path(directory/name)
    if not path.is_relative_to(directory): raise ValueError('Model file escapes installation')
    return path


def model_catalog():
    items=[]
    for identifier, value in CATALOG.items():
        directory=directory_for(identifier);ready=False;verified=False
        try:
            entries=entries_for(directory,identifier)
            ready=bool(entries) and all(checked_path(directory,name).is_file() for name in value['files'])
            ready=ready and all(checked_path(directory,e['name']).is_file() for e in entries)
            receipt=directory/'verification.json'
            if ready and receipt.is_file():
                saved=json.loads(receipt.read_text())
                verified=all(saved['files'].get(e['name'])==[checked_path(directory,e['name']).stat().st_size,checked_path(directory,e['name']).stat().st_mtime_ns] for e in entries)
        except (OSError,ValueError,KeyError,TypeError): pass
        items.append({'id':identifier,**value,'path':str(directory),'ready':bool(ready),'verified':verified})
    return {'items':items}


def digest_file(path, entry, cancelled=lambda:False):
    algorithm='sha256' if entry.get('sha256') else 'sha1'
    digest=hashlib.new(algorithm)
    if algorithm=='sha1': digest.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as stream:
        while data:=stream.read(1024*1024):
            if cancelled():raise CancelledError('Verification cancelled')
            digest.update(data)
    return digest.hexdigest()==entry.get(algorithm)


def verify_model(identifier, *, cancelled=lambda:False, on_event=lambda event:None):
    with file_lock(writable_path('.runtime/locks/storage.lock')):
        directory=directory_for(identifier);entries=entries_for(directory,identifier)
        if not entries or any(not checked_path(directory,name).is_file() for name in definition(identifier)['files']):raise ModelError('MODEL_MISSING')
        for index,entry in enumerate(entries):
            path=checked_path(directory,entry['name'])
            if not path.is_file() or not digest_file(path,entry,cancelled):raise ModelError('CHECKSUM_MISMATCH')
            on_event({'stage':'model_verify','state':'running','model_id':identifier,'completed_files':index+1,'total_files':len(entries)})
        write_json(directory/'verification.json',{'checked_at':now(),'files':{e['name']:[checked_path(directory,e['name']).stat().st_size,checked_path(directory,e['name']).stat().st_mtime_ns] for e in entries}})
    return {'state':'completed','model_id':identifier}


def fetch_file(url, target, entry, *, token=None, cancelled=lambda:False, progress=lambda size:None):
    """The partial file is private cache; only verified bytes become installed models."""
    import httpx
    target=writable_path(target);target.parent.mkdir(parents=True,exist_ok=True)
    offset=target.stat().st_size if target.exists() else 0
    if offset and entry.get('size') is None and digest_file(target,entry,cancelled):return
    if entry.get('size') is not None and offset>entry['size']:
        target.unlink();offset=0
    if entry.get('size') is not None and offset==entry['size']:
        if digest_file(target,entry,cancelled):return
        target.unlink();offset=0
    headers={'Authorization':'Bearer '+token} if token else {}
    if offset:headers['Range']=f'bytes={offset}-'
    try:
        with httpx.stream('GET',url,headers=headers,follow_redirects=True,timeout=httpx.Timeout(45,connect=15)) as response:
            if response.status_code in {401,403}:raise ModelError('HF_ACCESS_DENIED' if response.status_code==403 else 'HF_TOKEN_REJECTED')
            response.raise_for_status()
            if offset and response.status_code==206:
                if not response.headers.get('Content-Range','').startswith(f'bytes {offset}-'):raise ModelError('INVALID_DOWNLOAD_RANGE')
            else:offset=0
            with target.open('ab' if offset else 'wb') as stream:
                for data in response.iter_bytes(1024*256):
                    if cancelled():raise CancelledError('Download cancelled')
                    stream.write(data);offset+=len(data);progress(offset)
        if entry.get('size') is not None and target.stat().st_size!=entry['size']:raise ModelError('INCOMPLETE_DOWNLOAD')
        if not digest_file(target,entry,cancelled):
            target.unlink();raise ModelError('CHECKSUM_MISMATCH')
    except httpx.RequestError:raise ModelError('NETWORK_ERROR') from None
    except httpx.HTTPStatusError:raise ModelError('DOWNLOAD_HTTP_ERROR') from None


def recover_publication(identifier):
    journal=writable_path('.runtime/model-jobs/publish-'+identifier+'.json')
    if not journal.exists():return
    value=json.loads(journal.read_text());directory=directory_for(identifier)
    backup=writable_path(value['backup'])
    if backup.parent!=directory.parent/'.history' or not backup.name.startswith(identifier+'-'):
        raise ValueError('Invalid model recovery journal')
    if not directory.exists() and backup.exists():backup.rename(directory)
    journal.unlink()


def download_in_worker(identifier, status:Path):
    import httpx
    from huggingface_hub import HfApi, hf_hub_url
    from huggingface_hub.errors import HfHubHTTPError
    apply_cache_environment();value=definition(identifier)
    token=None
    if value['gated']:
        from .diarization import read_token
        try:token=read_token()
        except RuntimeError:raise ModelError('HF_TOKEN_REQUIRED') from None
    with file_lock(writable_path('.runtime/locks/storage.lock')):
        recover_publication(identifier)
        try:info=HfApi().model_info(value['repository'],token=token or False,files_metadata=True)
        except HfHubHTTPError as error:
            raise ModelError('HF_ACCESS_DENIED' if error.response.status_code==403 else 'HF_TOKEN_REJECTED' if error.response.status_code==401 else 'MODEL_SERVICE_ERROR') from None
        except httpx.RequestError:raise ModelError('NETWORK_ERROR') from None
        if not info.sha:raise ModelError('REVISION_MISSING')
        indexed={entry.rfilename:entry for entry in info.siblings};entries=[]
        for name in value['files']:
            source=indexed.get(name)
            if source is None:raise ModelError('MODEL_FILE_MISSING')
            sha=source.lfs.sha256 if source.lfs else source.blob_id
            if not sha:raise ModelError('CHECKSUM_MISSING')
            entries.append({'name':name,'size':source.size,'sha256' if source.lfs else 'sha1':sha})
        cache=writable_path(storage_roots(workspace_root())['cache_dir']/'model-downloads'/identifier/info.sha)
        completed=0;total=sum(e['size'] or 0 for e in entries)
        for index,entry in enumerate(entries):
            last_progress=[0.0]
            def progress(size):
                if time.monotonic()-last_progress[0]<.2 and size!=entry['size']:return
                last_progress[0]=time.monotonic()
                write_json(status,{'stage':'model_download','state':'downloading','model_id':identifier,'file':entry['name'],'bytes':completed+size,'total_bytes':total,'completed_files':index,'total_files':len(entries)})
            write_json(status,{'stage':'model_download','state':'connecting','model_id':identifier,'completed_files':index,'total_files':len(entries)})
            target=checked_path(cache,entry['name']+'.part')
            fetch_file(hf_hub_url(value['repository'],entry['name'],revision=info.sha),target,entry,token=token,progress=progress)
            completed+=target.stat().st_size
        directory=directory_for(identifier);stage=writable_path(directory.parent/('.install-'+identifier+'-'+uuid.uuid4().hex));stage.mkdir(parents=True)
        backup=None
        try:
            for entry in entries:
                target=checked_path(stage,entry['name']);target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(checked_path(cache,entry['name']+'.part'),target)
                if not digest_file(target,entry):raise ModelError('CHECKSUM_MISMATCH')
            manifest={'repository':value['repository'],'revision':info.sha,'license':value['license'],'checked_weight_files':[e for e in entries if e.get('sha256')]}
            if identifier in {'asr','vad'}:
                manifest.update(filename=entries[0]['name'],sha256=entries[0]['sha256'],size_bytes=entries[0]['size'])
            write_json(stage/('model.json' if identifier in {'asr','vad'} else 'sonafolio-model.json'),manifest)
            write_json(stage/'installation.json',{'repository':value['repository'],'revision':info.sha,'files':entries})
            write_json(stage/'verification.json',{'checked_at':now(),'files':{e['name']:[checked_path(stage,e['name']).stat().st_size,checked_path(stage,e['name']).stat().st_mtime_ns] for e in entries}})
            if directory.exists():
                backup=writable_path(directory.parent/'.history'/(identifier+'-'+uuid.uuid4().hex));backup.parent.mkdir(parents=True,exist_ok=True)
                write_json(writable_path('.runtime/model-jobs/publish-'+identifier+'.json'),{'backup':str(backup)})
                directory.rename(backup)
            stage.rename(directory)
            journal=writable_path('.runtime/model-jobs/publish-'+identifier+'.json')
            if journal.exists():journal.unlink()
        except BaseException:
            if backup is not None and backup.exists() and not directory.exists():backup.rename(directory)
            raise
        finally:
            if stage.exists():shutil.rmtree(stage)
        write_json(status,{'stage':'model_download','state':'completed','model_id':identifier,'bytes':completed,'total_bytes':total})


def download_model(identifier, *, cancelled=lambda:False, on_event=lambda event:None):
    definition(identifier)
    job=writable_path('.runtime/model-jobs')/uuid.uuid4().hex;job.mkdir(parents=True)
    status=job/'status.json';seen=None
    def tick():
        nonlocal seen
        if status.exists():
            value=json.loads(status.read_text())
            if value!=seen:on_event(value);seen=value
    write_json(status,{'stage':'model_download','state':'queued','model_id':identifier})
    try:
        run_stage_process([sys.executable,'-B','-m','sonafolio._model_worker',identifier,str(status)],job/'worker.log',cancelled,on_tick=tick)
        tick();result=json.loads(status.read_text())
        if result.get('state')!='completed':raise ModelError(result.get('error_code','DOWNLOAD_FAILED'))
        return {'state':'completed','model_id':identifier}
    except CancelledError:
        write_json(status,{'stage':'model_download','state':'cancelled','model_id':identifier});raise
