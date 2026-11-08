"""Versioned local JSON Lines bridge for a trusted desktop parent process."""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import redirect_stdout
from dataclasses import fields
import json
from pathlib import Path, PurePosixPath
import signal
import sys
import threading
from typing import Literal, get_type_hints
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError, TypeAdapter, create_model

from . import __version__
from .paths import workspace_path
from .pipeline import RunOptions, run_pipeline, resume_pipeline
from .processes import CancelledError
from .tasks import data_root, load_manifest, rename_task, task_directory

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 128 * 1024


class Params(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


_option_types = get_type_hints(RunOptions)
_option_constraints = {
    'start':{'ge':0,'allow_inf_nan':False}, 'duration':{'gt':0,'allow_inf_nan':False},
    'threads':{'ge':1}, 'num_speakers':{'ge':1}, 'merge_max_tokens':{'ge':1,'le':32768},
    'model':{'min_length':1}, 'vad_model':{'min_length':1},
}
PipelineOptions = create_model('PipelineOptions', __base__=Params,
    **{field.name: (_option_types[field.name], Field(default=field.default, **_option_constraints.get(field.name,{}))) for field in fields(RunOptions)})


class Request(Params):
    protocol_version: Literal[1]
    id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')
    method: str = Field(min_length=1, max_length=80)
    params: dict = Field(default_factory=dict)


class ListParams(Params):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class TaskParams(Params):
    task_id: str = Field(pattern=r'^[a-f0-9]{64}$')


class RunParams(Params):
    audio: str = Field(min_length=1)
    options: PipelineOptions = Field(default_factory=PipelineOptions)
    force: bool = False


class ResumeParams(TaskParams):
    analysis: Literal['none', 'preview', 'cloud'] | None = None


class RenameParams(TaskParams):
    title: str = Field(min_length=1, max_length=200)


class OperationParams(Params):
    operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class ReadParams(TaskParams):
    path: str = Field(min_length=1, max_length=1024)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=65536, ge=1, le=MAX_ARTIFACT_BYTES)


METHODS = {
    'hello': Params, 'tasks.list': ListParams, 'tasks.get': TaskParams,
    'tasks.run': RunParams, 'tasks.resume': ResumeParams,
    'tasks.rename': RenameParams, 'operations.get': OperationParams,
    'operations.cancel': OperationParams, 'artifacts.read': ReadParams,
}


class ErrorBody(Params):
    code: str
    message: str


class SuccessResponse(Params):
    protocol_version: Literal[1]
    seq: int = Field(ge=1)
    type: Literal['response']
    id: str
    ok: Literal[True]
    result: dict


class FailureResponse(Params):
    protocol_version: Literal[1]
    seq: int = Field(ge=1)
    type: Literal['response']
    id: str | None
    ok: Literal[False]
    error: ErrorBody


class Event(Params):
    protocol_version: Literal[1]
    seq: int = Field(ge=1)
    type: Literal['event']
    event: Literal['server.ready','task.progress','operation.finished']
    operation_id: str | None = None
    data: dict


def protocol_schema() -> dict:
    return {'protocol_version': PROTOCOL_VERSION, 'request': Request.model_json_schema(),
            'outbound': TypeAdapter(SuccessResponse | FailureResponse | Event).json_schema(),
            'params': {name: model.model_json_schema() for name, model in METHODS.items()},
            'limits': {'line_bytes':MAX_LINE_BYTES, 'artifact_bytes':MAX_ARTIFACT_BYTES,
                       'active_operations':1, 'recent_operations':128, 'recent_request_ids':2048}}


class RPCError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


def task_view(task: Path) -> dict:
    manifest = load_manifest(task)
    if any(not isinstance(manifest.get(key),str) for key in ('title','state','created_at','updated_at')):
        raise ValueError('Task summary contains invalid display fields')
    view = {key: manifest.get(key) for key in (
        'task_id','title','state','current_stage','created_at','updated_at','latest_result','analysis_versions')}
    view['input'] = {key:manifest['input'].get(key) for key in ('sha256','duration_seconds','size_bytes','format','sample_rate','channels')}
    view['stages'] = {name:{'state':attempts[-1]['state'],'attempts':len(attempts)}
                      for name,attempts in manifest.get('stages',{}).items() if attempts}
    return view



class DesktopServer:
    def __init__(self, output, *, run=run_pipeline, resume=resume_pipeline, diagnostics=None):
        self.output = output
        self.diagnostics = diagnostics if diagnostics is not None else sys.stderr
        self.run, self.resume = run, resume
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._sequence = 0
        self._jobs = OrderedDict()
        self._request_ids = OrderedDict()
        self._closed = False

    def capabilities(self):
        return {'app_version':__version__, 'platform':sys.platform, 'protocol_version':PROTOCOL_VERSION,
                'methods':list(METHODS), 'limits':protocol_schema()['limits'],
                'default_analysis':'preview', 'transport':'stdio-jsonl'}

    def send(self, value):
        with self._write_lock:
            self._sequence += 1
            envelope = {'protocol_version':PROTOCOL_VERSION, 'seq':self._sequence, **value}
            try:
                self.output.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False) + '\n')
                self.output.flush()
            except (BrokenPipeError, OSError, ValueError):
                # A closed parent must not leave paid requests or native workers running.
                with self._state_lock:
                    for job in self._jobs.values():
                        job['cancel'].set()
                return False
        return True

    def ready(self):
        self.send({'type':'event','event':'server.ready','data':self.capabilities()})

    def process_line(self, line: str):
        identifier = None
        launch = None
        try:
            def reject_constant(_):
                raise ValueError('Nonfinite JSON number')
            raw = json.loads(line, parse_constant=reject_constant)
            if isinstance(raw, dict):
                # Correlate invalid-version requests without echoing arbitrary input.
                candidate = raw.get('id')
                if isinstance(candidate, str) and len(candidate) <= 128 and candidate.isascii() and all(c.isalnum() or c in '._:-' for c in candidate):
                    identifier = candidate
                if type(raw.get('protocol_version')) is not int or raw.get('protocol_version') != PROTOCOL_VERSION:
                    raise RPCError('UNSUPPORTED_VERSION','Expected protocol_version 1')
            request = Request.model_validate(raw)
            identifier = request.id
            if identifier in self._request_ids:
                raise RPCError('DUPLICATE_REQUEST','Use a new request id')
            self._request_ids[identifier] = None
            if len(self._request_ids) > 2048:
                self._request_ids.popitem(last=False)
            model = METHODS.get(request.method)
            if model is None:
                raise RPCError('METHOD_NOT_FOUND','Unknown method')
            params = model.model_validate(request.params)
            result, launch = self.dispatch(request.method, params)
            response = {'type':'response','id':identifier,'ok':True,'result':result}
        except RPCError as error:
            response = {'type':'response','id':identifier,'ok':False,'error':{'code':error.code,'message':error.message}}
        except (json.JSONDecodeError, UnicodeError, ValueError, ValidationError, TypeError):
            response = {'type':'response','id':identifier,'ok':False,'error':{'code':'INVALID_REQUEST','message':'Malformed JSON or invalid request parameters'}}
        except FileNotFoundError:
            response = {'type':'response','id':identifier,'ok':False,'error':{'code':'NOT_FOUND','message':'Task or artifact not found'}}
        except RuntimeError:
            response = {'type':'response','id':identifier,'ok':False,'error':{'code':'CONFLICT','message':'Resource is busy or unavailable'}}
        except Exception as error:
            self.diagnostics.write(f'IPC request failed: {type(error).__name__}\n')
            response = {'type':'response','id':identifier,'ok':False,'error':{'code':'INTERNAL_ERROR','message':'Request failed; inspect local diagnostics'}}
        delivered = self.send(response)
        if launch is not None:
            if delivered:
                launch()
            else:
                # The thread has not started, so there are no core side effects.
                with self._state_lock:
                    for job in self._jobs.values():
                        if job['state'] == 'queued':
                            job['state'] = 'cancelled'

    def dispatch(self, method, params):
        if method == 'hello':
            return self.capabilities(), None
        if method == 'tasks.list':
            paths = sorted((data_root()/'runs').glob('*/manifest.json'))
            views, unreadable = [], 0
            for path in paths:
                try:
                    views.append(task_view(path.parent))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    unreadable += 1
            views.sort(key=lambda item: (item.get('updated_at') or '',item['task_id']), reverse=True)
            return {'items':views[params.offset:params.offset+params.limit], 'total':len(views), 'unreadable':unreadable}, None
        if method == 'tasks.get':
            return task_view(task_directory(params.task_id)), None
        if method == 'tasks.rename':
            return rename_task(params.task_id, params.title), None
        if method == 'artifacts.read':
            relative = PurePosixPath(params.path)
            if relative.is_absolute() or '..' in relative.parts or '\\' in params.path:
                raise RPCError('INVALID_PATH','Use a task-relative artifact path')
            task = task_directory(params.task_id)
            load_manifest(task)
            path = workspace_path(task / params.path)
            if not path.is_relative_to(task) or path.is_relative_to(task/'input') or path.suffix not in {'.json','.md'}:
                raise RPCError('INVALID_PATH','Only JSON and Markdown artifacts inside this task can be read')
            with path.open('rb') as stream:
                stream.seek(params.offset)
                data = stream.read(params.limit)
                next_offset = params.offset + len(data)
                eof = not stream.read(1)
            return {'path':params.path,'encoding':'base64','data':base64.b64encode(data).decode('ascii'),
                    'offset':params.offset,'next_offset':next_offset,'eof':eof}, None
        if method in {'operations.get','operations.cancel'}:
            with self._state_lock:
                job = self._jobs.get(params.operation_id)
                if job is None:
                    raise RPCError('NOT_FOUND','Operation is not retained by this server')
                if method == 'operations.cancel' and job['state'] in {'queued','running','cancelling'}:
                    job['cancel'].set()
                    job['state'] = 'cancelling'
                return self.job_view(job), None
        if method == 'tasks.run':
            options = RunOptions(**params.options.model_dump())
            options.validate()
            audio = Path(params.audio)
            if not audio.is_absolute():
                # Resolve relative input against workspace, independently of host cwd.
                audio = workspace_path(audio)
            if not audio.is_file():
                raise FileNotFoundError(audio)
            target = lambda job: self.run(audio, options=options, force=params.force,
                cancelled=job['cancel'].is_set, on_event=lambda event:self.progress(job,event))
        else:
            task = task_directory(params.task_id)
            load_manifest(task)
            target = lambda job: self.resume(params.task_id, analysis=params.analysis,
                cancelled=job['cancel'].is_set, on_event=lambda event:self.progress(job,event))
        with self._state_lock:
            if self._closed:
                raise RPCError('SHUTTING_DOWN','Server is shutting down')
            if any(job['state'] in {'queued','running','cancelling'} for job in self._jobs.values()):
                raise RPCError('BUSY','One processing operation is already active')
            job = {'operation_id':uuid.uuid4().hex,'task_id':getattr(params,'task_id',None),
                   'state':'queued','cancel':threading.Event(),'result':None,'error':None}
            thread = threading.Thread(target=self.work, args=(job,target), name='sonafolio-operation')
            job['thread'] = thread
            self._jobs[job['operation_id']] = job
            while len(self._jobs)>128:
                self._jobs.popitem(last=False)
            return self.job_view(job), thread.start

    @staticmethod
    def job_view(job):
        return {key:job.get(key) for key in ('operation_id','task_id','state','result','error')}

    def progress(self, job, event):
        with self._state_lock:
            job['task_id'] = event.get('task_id',job['task_id'])
        self.send({'type':'event','event':'task.progress','operation_id':job['operation_id'], 'data':event})

    def work(self, job, target):
        with self._state_lock:
            if not job['cancel'].is_set():
                job['state'] = 'running'
        try:
            if job['cancel'].is_set():
                raise CancelledError('Cancelled before starting')
            result = target(job)
            with self._state_lock:
                job.update(state=result.get('state','completed'), result=result, task_id=result.get('task_id',job['task_id']))
        except CancelledError:
            with self._state_lock:
                job['state'] = 'cancelled'
        except Exception as error:
            self.diagnostics.write(f'IPC operation failed: {type(error).__name__}\n')
            with self._state_lock:
                job.update(state='failed',error={'code':'PROCESSING_FAILED','message':'Processing failed; task state and stage logs are retained','error_type':type(error).__name__})
        finally:
            with self._state_lock:
                final = self.job_view(job)
            self.send({'type':'event','event':'operation.finished','operation_id':job['operation_id'],'data':final})

    def close(self):
        with self._state_lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for job in jobs:
                if job['state'] in {'queued','running','cancelling'}:
                    job['cancel'].set()
        for job in jobs:
            if job['thread'].ident is not None:
                job['thread'].join()


def serve_stdio():
    output = sys.stdout
    server = DesktopServer(output)
    previous = signal.getsignal(signal.SIGTERM)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    try:
        # The protocol writer retains the original stream; Python diagnostics
        # and adapter print statements use stderr. Native inference uses logs.
        with redirect_stdout(sys.stderr):
            server.ready()
            stream = sys.stdin.buffer
            while True:
                line = stream.readline(MAX_LINE_BYTES+1)
                if not line:
                    break
                if len(line)>MAX_LINE_BYTES:
                    while line and not line.endswith(b'\n'):
                        line=stream.readline(MAX_LINE_BYTES+1)
                    server.send({'type':'response','id':None,'ok':False,'error':{'code':'REQUEST_TOO_LARGE','message':'JSON line exceeds 1 MiB'}})
                    continue
                try:
                    server.process_line(line.decode('utf-8'))
                except UnicodeError:
                    server.send({'type':'response','id':None,'ok':False,'error':{'code':'INVALID_REQUEST','message':'Expected UTF-8 JSON'}})
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        signal.signal(signal.SIGTERM,previous)
