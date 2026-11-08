"""Durable batches reference independently managed tasks and frozen scene catalogs."""
from __future__ import annotations
from dataclasses import asdict,replace
import hashlib
import json
from pathlib import Path
import re
import uuid

from .audio import record_input
from .llm import load_settings
from .paths import workspace_path,writable_path,write_json,workspace_root
from .pipeline import RunOptions,run_pipeline
from .processes import CancelledError
from .skills import SkillRegistry
from .tasks import data_root,ensure_data_root,file_lock,load_manifest,managed_input,now,save_manifest,task_directory


def batch_path(identifier: str) -> Path:
    if not re.fullmatch('[a-f0-9]{32}',identifier):raise ValueError('Invalid batch ID')
    return writable_path(data_root()/'batches'/identifier)


def load_batch(identifier: str) -> dict:
    value=json.loads((batch_path(identifier)/'batch.json').read_text())
    if value.get('schema_version')!=1 or value.get('batch_id')!=identifier:raise ValueError('Unsupported batch manifest')
    return value


def save_batch(value: dict):
    value['updated_at']=now();write_json(batch_path(value['batch_id'])/'batch.json',value)


def list_batches() -> dict:
    root=data_root()/'batches';items=[]
    for path in root.glob('*/batch.json'):
        value=load_batch(path.parent.name)
        counts={}
        for item in value['items']:counts[item['state']]=counts.get(item['state'],0)+1
        items.append({key:value.get(key) for key in ('batch_id','title','state','created_at','updated_at')}|{'counts':counts,'total':len(value['items'])})
    items.sort(key=lambda item:item['updated_at'],reverse=True)
    return {'items':items}


def create_batch(files: list[str], options: RunOptions) -> dict:
    if not files or len(files)>500:raise ValueError('A batch requires 1–500 recordings')
    options=replace(options)
    options.validate();ensure_data_root()
    identifier=uuid.uuid4().hex;directory=batch_path(identifier)
    from .scenes import scene_root
    with file_lock(scene_root()/'.scene.lock'):
        registry=SkillRegistry(workspace_root()/'skills')
        catalog={'schema_version':1,'skills':{key:asdict(skill) for key,skill in registry.loaded.items()},'created_at':now()}
    write_json(directory/'catalog.json',catalog)
    options.catalog=str(directory/'catalog.json');options.llm_settings=load_settings().model_dump();options.review_unknown=True
    if options.prompt:
        source=Path(options.prompt)
        if not source.is_absolute():source=workspace_path(source)
        target=writable_path(directory/'prompt.md');target.write_text(source.read_text());options.prompt=str(target)
    created=now()
    value={'schema_version':1,'batch_id':identifier,'title':f'导入批次 · {created[:16].replace("T"," ")}',
           'created_at':created,'updated_at':created,'state':'queued','options':asdict(options),
           'catalog_sha256':hashlib.sha256((directory/'catalog.json').read_bytes()).hexdigest(),
           'items':[{'item_id':uuid.uuid4().hex,'source':str(Path(path).resolve() if Path(path).is_absolute() else workspace_path(path)),'filename':Path(path).name,'task_id':None,'state':'queued','error_type':None} for path in dict.fromkeys(files)]}
    save_batch(value);return value


def batch_view(identifier: str) -> dict:
    value=load_batch(identifier)
    # File paths remain private in the manifest, not needed in the UI list.
    result={key:child for key,child in value.items() if key not in {'options','catalog_sha256'}}
    result['analysis']=value['options']['analysis']
    result['items']=[{key:child for key,child in item.items() if key!='source'} for item in value['items']]
    catalog=json.loads((batch_path(identifier)/'catalog.json').read_text())
    result['scenes']=[{'id':s['id'],'name':s.get('name') or s['id'],'description':s['description']} for s in catalog['skills'].values() if s['enabled']]
    return result


def run_batch(identifier: str, *, cancelled=lambda:False, on_event=lambda event:None, runner=run_pipeline, analysis: str | None = None, item_id: str | None = None) -> dict:
    directory=batch_path(identifier)
    with file_lock(directory/'.batch.lock'):
        value=load_batch(identifier)
        catalog=directory/'catalog.json'
        if hashlib.sha256(catalog.read_bytes()).hexdigest()!=value['catalog_sha256']:raise ValueError('Batch catalog snapshot changed')
        # Resolve paths from the current data root after a workspace move.
        value['options']['catalog']=str(catalog)
        if (directory/'prompt.md').exists():value['options']['prompt']=str(directory/'prompt.md')
        selected=next((item for item in value['items'] if item['item_id']==item_id),None) if item_id else None
        if item_id and selected is None:raise ValueError('Batch item not found')
        if analysis is not None:
            if analysis not in {'none','preview','cloud'}:raise ValueError('Invalid analysis mode')
            if selected is not None:selected['analysis_override']=analysis
            else:value['options']['analysis']=analysis
            for item in value['items']:
                if (not item_id or item['item_id']==item_id) and item['state']=='preview_only':item['state']='queued'
        options=RunOptions(**value['options']);options.validate()
        value['state']='importing';save_batch(value)
        def emit(stage,state,item=None,**extra):
            on_event({'batch_id':identifier,'stage':stage,'state':state,'item_id':item['item_id'] if item else None,
                      'task_id':item.get('task_id') if item else None,**extra})
        try:
            # Import all available inputs before inference, so queued audio survives source moves.
            seen={item['task_id'] for item in value['items'] if item['task_id'] and item['state']!='duplicate'}
            for item in value['items']:
                if cancelled():raise CancelledError('Batch paused')
                if item['task_id']:continue
                try:
                    _,task=record_input(Path(item['source']));item['task_id']=task.name
                    if task.name in seen:item['state']='duplicate'
                    else:
                        seen.add(task.name);item['state']='queued'
                        with file_lock(task/'.pipeline.lock'):
                            manifest=load_manifest(task)
                            if identifier not in manifest['batch_ids']:manifest['batch_ids'].append(identifier)
                            save_manifest(task,manifest)
                    item['error_type']=None
                except Exception as error:item.update(state='failed',error_type=type(error).__name__)
                save_batch(value);emit('import',item['state'],item)
            value['state']='running';save_batch(value)
            for item in value['items']:
                if cancelled():raise CancelledError('Batch paused')
                if item_id and item['item_id']!=item_id:continue
                if item['state'] in {'completed','preview_only','duplicate','needs_review'} or not item['task_id']:continue
                item.update(state='running',error_type=None);save_batch(value)
                item_options=RunOptions(**asdict(options))
                if item.get('skill_override'):item_options.skill=item['skill_override']
                if item.get('analysis_override'):item_options.analysis=item['analysis_override']
                def progress(event):
                    on_event({**event,'batch_id':identifier,'item_id':item['item_id']})
                try:
                    result=runner(managed_input(task_directory(item['task_id'])),options=item_options,cancelled=cancelled,on_event=progress)
                    item['state']=result['state'];item['result']=result
                except CancelledError:
                    item['state']='cancelled';save_batch(value);raise
                except Exception as error:item.update(state='failed',error_type=type(error).__name__)
                save_batch(value);emit('item',item['state'],item)
            states={item['state'] for item in value['items']}
            value['state']='paused' if states & {'queued','running','cancelled'} else 'needs_review' if 'needs_review' in states else 'completed_with_errors' if 'failed' in states else 'completed'
            save_batch(value);emit('batch',value['state'])
            return {'state':value['state'],'batch_id':identifier}
        except CancelledError:
            value['state']='paused';save_batch(value);emit('batch','paused')
            return {'state':'paused','batch_id':identifier}
        except BaseException:
            value['state']='paused';save_batch(value);raise


def resolve_item(identifier: str, item_id: str, skill_id: str) -> dict:
    with file_lock(batch_path(identifier)/'.batch.lock'):
        value=load_batch(identifier)
        catalog=SkillRegistry.from_snapshot(json.loads((batch_path(identifier)/'catalog.json').read_text()))
        if skill_id not in {route['id'] for route in catalog.routes()}:raise ValueError('Choose an enabled scene from this batch snapshot')
        item=next((item for item in value['items'] if item['item_id']==item_id),None)
        if item is None or item['state']!='needs_review':raise ValueError('This item is not waiting for scene confirmation')
        item.update(skill_override=skill_id,state='queued');value['state']='queued';save_batch(value)
    return batch_view(identifier)
