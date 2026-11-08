"""Editable, versioned declarative scenes and cancellable keyword drafting."""
from __future__ import annotations
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from jinja2 import nodes, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from .paths import workspace_root, writable_path, write_json
from .processes import CancelledError, run_stage_process
from .tasks import file_lock, now


class SceneDefinition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$',max_length=100)
    name: str = Field(min_length=1,max_length=100)
    description: str = Field(min_length=1,max_length=4000)
    keywords: list[str] = Field(default_factory=list,max_length=30)
    use_when: str = Field(default='',max_length=6000)
    not_when: str = Field(default='',max_length=6000)
    parent: str | None = None
    enabled: bool = True
    prompt: str = Field(min_length=1,max_length=30000)
    output_schema: dict = Field(alias="schema")
    template: str = Field(min_length=1,max_length=24000)
    example: dict | None = None


def scene_root() -> Path:
    root=writable_path('skills');root.mkdir(parents=True,exist_ok=True)
    ignore=writable_path(root/'.gitignore')
    if not ignore.exists():ignore.write_text('# Workspace scene definitions and drafts are private by default\n*\n')
    return root


def example_for(schema: dict, root=None, depth=0):
    if depth>12:raise ValueError('Recursive scene schema needs an explicit example')
    root=root or schema
    if '$ref' in schema:
        ref=schema['$ref']
        if not ref.startswith('#/'):raise ValueError('Only local schema references are supported')
        target=root
        for key in ref[2:].split('/'):target=target[key.replace('~1','/').replace('~0','~')]
        return example_for(target,root,depth+1)
    if 'const' in schema:return schema['const']
    if 'enum' in schema:return schema['enum'][0]
    if 'anyOf' in schema or 'oneOf' in schema:return example_for((schema.get('anyOf') or schema['oneOf'])[0],root,depth+1)
    kind=schema.get('type','object')
    if isinstance(kind,list):kind=next((k for k in kind if k!='null'),'null')
    if kind=='object':return {key:example_for(value,root,depth+1) for key,value in schema.get('properties',{}).items()}
    if kind=='array':return [example_for(schema.get('items',{}),root,depth+1) for _ in range(min(schema.get('minItems',0),10))]
    if kind=='string':return '示例内容'*max(1,(schema.get('minLength',1)+3)//4)
    if kind=='boolean':return False
    if kind in {'integer','number'}:return schema.get('minimum',0)
    return None


def validate_schema_refs(value):
    if isinstance(value,dict):
        if any(key in value and not value[key].startswith('#') for key in ('$ref','$dynamicRef','$recursiveRef')):raise ValueError('Remote schema references are not allowed')
        for child in value.values():validate_schema_refs(child)
    elif isinstance(value,list):
        for child in value:validate_schema_refs(child)


def bounded_render(template: str, result: dict, metadata: dict, *, limit=2*1024*1024) -> str:
    environment=SandboxedEnvironment(undefined=StrictUndefined,autoescape=False)
    environment.globals.clear()
    environment.globals.update(refs=lambda ids:'、'.join(f'[{item}](sources.md#{item})' for item in ids),
                               role_name=lambda role:{'candidate':'候选人','interviewer':'面试官','unknown':'未知','doctor':'医生','patient':'患者','courier':'快递员','recipient':'收件人','other':'其他'}.get(role,role))
    ast=environment.parse(template)
    for call in ast.find_all(nodes.Call):
        if not isinstance(call.node,nodes.Name) or call.node.name not in {'refs','role_name'}:
            raise ValueError('Templates may only call refs() and role_name()')
    chunks=[];size=0
    for chunk in environment.from_string(template).generate(result=result,metadata=metadata):
        size+=len(chunk)
        if size>limit:raise ValueError('Rendered scene output exceeds the size limit')
        chunks.append(chunk)
    return ''.join(chunks).strip()+'\n'


def validate_scene(value: dict) -> tuple[SceneDefinition,str]:
    scene=SceneDefinition.model_validate(value)
    if scene.id=='general':raise ValueError('The base scene cannot be edited')
    if scene.id=='unknown' and not scene.enabled:raise ValueError('The unknown fallback must stay enabled')
    if any(not word.strip() or len(word)>100 for word in scene.keywords):raise ValueError('Invalid scene keyword')
    if scene.parent==scene.id:raise ValueError('A scene cannot be its own parent')
    Draft202012Validator.check_schema(scene.output_schema);validate_schema_refs(scene.output_schema)
    if scene.output_schema.get('type')!='object':raise ValueError('Scene schema must describe an object')
    sample=scene.example if scene.example is not None else example_for(scene.output_schema)
    Draft202012Validator(scene.output_schema).validate(sample)
    preview=bounded_render(scene.template,sample,{'skill_id':scene.id,'skill_version':'preview','source_sha256':'example','chunks':1})
    return scene,preview


def as_definition(skill) -> dict:
    return {'id':skill.id,'name':skill.name or skill.id,'description':skill.description,'keywords':list(skill.keywords),
            'use_when':skill.use_when,'not_when':skill.not_when,'parent':skill.parent,'enabled':skill.enabled,
            'prompt':skill.prompt,'schema':skill.schema,'template':skill.template,'example':skill.example}


def list_scenes() -> dict:
    from .skills import SkillRegistry
    registry=SkillRegistry(workspace_root()/'skills')
    items=[]
    for skill in registry.loaded.values():
        if skill.id=='general':continue
        items.append({**as_definition(skill),'version':skill.version,'digest':skill.digest})
    drafts=[]
    for path in sorted((workspace_root()/'skills/.drafts').glob('*/draft.json')):
        artifact=json.loads(writable_path(path).read_text())
        if artifact.get('published_version'):continue
        drafts.append({'draft_id':path.parent.name,'name':artifact['scene']['name'],'updated_at':artifact['created_at']})
    return {'items':items,'drafts':drafts}


def save_scene(value: dict, *, expected_version: str | None = None) -> dict:
    from .skills import SkillRegistry
    scene,preview=validate_scene(value)
    root=scene_root()
    with file_lock(root/'.scene.lock'):
        registry=SkillRegistry(root)
        current=registry.loaded.get(scene.id)
        if current and expected_version!=current.version:raise ValueError('Scene version changed; reload before saving')
        if not current and expected_version is not None:raise ValueError('Scene no longer exists')
        if scene.parent and scene.parent not in registry.loaded:raise ValueError('Parent scene is missing')
        parent=scene.parent;seen={scene.id}
        while parent:
            if parent in seen:raise ValueError('Cyclic scene hierarchy')
            seen.add(parent);parent=registry.get(parent).parent
        directory=writable_path(root/scene.id)
        version='1.0.0'
        if current:
            match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)',current.version)
            if match:
                patches=[int(match[3])]
                for previous in (directory/'versions').glob(f'{match[1]}.{match[2]}.*.json'):
                    if previous.stem.split('.')[-1].isdigit():patches.append(int(previous.stem.split('.')[-1]))
                version=f'{match[1]}.{match[2]}.{max(patches)+1}'
            else:version='1.0.0-'+uuid.uuid4().hex[:8]
            if re.fullmatch(r'[A-Za-z0-9._-]+',current.version) and not (directory/'versions'/(current.version+'.json')).exists():
                write_json(directory/'versions'/(current.version+'.json'),{'protocol_version':2,'version':current.version,'created_at':now(),'scene':as_definition(current),'digest':current.digest})
        artifact={'protocol_version':2,'version':version,'created_at':now(),'scene':scene.model_dump(by_alias=True)}
        directory=writable_path(root/scene.id)
        # A full resolved definition is independent of later parent edits.
        digest=hashlib.sha256(json.dumps(artifact['scene'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        artifact['digest']=digest
        write_json(directory/'versions'/f'{version}.json',artifact)
        write_json(directory/'scene.json',artifact)
        for draft_path in (root/'.drafts').glob('*/draft.json'):
            draft_path=writable_path(draft_path)
            draft=json.loads(draft_path.read_text())
            if draft.get('scene',{}).get('id')==scene.id:
                draft.update(published_version=version,published_at=now());write_json(draft_path,draft)
    return {**scene.model_dump(by_alias=True),'version':version,'digest':digest,'preview':preview}


def preview_scene(value: dict) -> dict:
    _,preview=validate_scene(value)
    return {'preview':preview}


def draft_request(keywords: list[str], *, call=None) -> dict:
    from .llm import request_json,load_settings
    from .skills import SkillRegistry
    if not keywords or len(keywords)>30 or any(not k.strip() or len(k)>100 for k in keywords):raise ValueError('Provide 1–30 short keywords')
    registry=SkillRegistry(workspace_root()/'skills')
    schema={'type':'object','additionalProperties':False,'properties':{
        'id':{'type':'string','pattern':r'^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$'},
        'name':{'type':'string'},'description':{'type':'string'},'use_when':{'type':'string'},'not_when':{'type':'string'},
        'prompt':{'type':'string'},'template':{'type':'string'},'base_scene':{'type':'string','enum':sorted(registry.loaded)},
    },'required':['id','name','description','use_when','not_when','prompt','template','base_scene']}
    content={'keywords':keywords,'bases':{key:{'description':s.description,'schema':s.schema,'template':s.template} for key,s in registry.loaded.items()}}
    instructions='根据关键词起草录音整理场景。关键词是意图，不是分类硬匹配规则。写明适用和排除情境，Prompt 区分事实、推断与建议并要求来源编号。选择最接近的 base_scene，其输出 Schema 将完整复用；模板只访问该 Schema 的 result 字段和 metadata，允许 refs(segment_ids)、role_name(role)，不要调用其他函数或生成执行代码。新 ID 不要覆盖现有场景。生成简体中文。'
    result,metadata=(call or request_json)(instructions,json.dumps(content,ensure_ascii=False),schema,settings=load_settings())
    Draft202012Validator(schema).validate(result)
    base=registry.get(result['base_scene'])
    identifier=result['id']
    if identifier in registry.loaded:identifier+='-custom-'+uuid.uuid4().hex[:6]
    value={'id':identifier,'name':result['name'],'description':result['description'],'keywords':keywords,
           'use_when':result['use_when'],'not_when':result['not_when'],'parent':base.id if base.id!='general' else None,
           'enabled':False,'prompt':result['prompt'],'schema':base.schema,'template':result['template'],'example':None}
    scene,preview=validate_scene(value)
    return {'scene':scene.model_dump(by_alias=True),'preview':preview,'metadata':metadata,'created_at':now()}


def draft_scene(keywords: list[str], *, cancelled=lambda:False, call=None) -> dict:
    if cancelled():raise CancelledError('Draft cancelled')
    directory=writable_path(scene_root()/'.drafts'/uuid.uuid4().hex)
    write_json(directory/'request.json',{'keywords':keywords})
    if call is not None:
        result=draft_request(keywords,call=call)
    else:
        run_stage_process([sys.executable,'-B','-m','sonafolio._scene_worker',str(directory/'request.json'),str(directory/'draft.json')],directory/'engine.log',cancelled)
        result=json.loads((directory/'draft.json').read_text())
    if cancelled():raise CancelledError('Draft cancelled')
    write_json(directory/'draft.json',result)
    return {'state':'completed','draft_id':directory.name,**result}


def load_draft(draft_id: str) -> dict:
    if not re.fullmatch('[a-f0-9]{32}',draft_id):raise ValueError('Invalid draft ID')
    return json.loads(writable_path(scene_root()/'.drafts'/draft_id/'draft.json').read_text())


def scene_history(identifier: str) -> dict:
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*',identifier):raise ValueError('Invalid scene ID')
    versions=[]
    for path in (workspace_root()/'skills'/identifier/'versions').glob('*.json'):
        value=json.loads(writable_path(path).read_text())
        versions.append({'version':value['version'],'created_at':value['created_at'],'enabled':value['scene']['enabled']})
    versions.sort(key=lambda item:item['created_at'],reverse=True)
    return {'items':versions}
