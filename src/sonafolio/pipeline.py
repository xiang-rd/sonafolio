"""Single-recording orchestration with durable attempts and content-checked reuse."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import sys
import uuid
from typing import Literal

from .audio import fingerprint, record_input
from .paths import workspace_root, writable_path, write_json
from .processes import CancelledError, run_stage_process
from .tasks import file_lock, load_manifest, managed_input, now, save_manifest, task_directory

PIPELINE_VERSION = 1


@dataclass
class RunOptions:
    model: str = '.models/whisper.cpp/ggml-large-v3-turbo-q5_0.bin'
    vad_model: str | None = '.models/whisper-vad/ggml-silero-v6.2.0.bin'
    start: float = 0
    duration: float | None = None
    threads: int = 4
    asr_device: Literal['auto','cpu'] = 'auto'
    speaker_device: Literal['cpu','mps','cuda'] = 'cpu'
    num_speakers: int | None = None
    refine: bool = True
    alignment_device: Literal['cpu','mps','cuda'] = 'cpu'
    analysis: Literal['none','preview','cloud'] = 'preview'
    skill: str | None = None
    skills_dir: str | None = None
    prompt: str | None = None
    merge_max_tokens: int = 16384

    def validate(self):
        if not math.isfinite(self.start) or self.start < 0 or (self.duration is not None and (not math.isfinite(self.duration) or self.duration <= 0)):
            raise ValueError('Invalid audio range')
        if self.threads < 1 or (self.num_speakers is not None and self.num_speakers < 1):
            raise ValueError('Threads and speaker count must be positive')
        if self.asr_device not in {'auto','cpu'} or self.speaker_device not in {'cpu','mps','cuda'} or self.alignment_device not in {'cpu','mps','cuda'}:
            raise ValueError('Unsupported model device')
        if self.analysis not in {'none','preview','cloud'} or not 1 <= self.merge_max_tokens <= 32768:
            raise ValueError('Invalid analysis mode or token budget')


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return 'missing'


def file_digest(path: str | Path) -> str:
    path = Path(path)
    if not path.is_absolute():
        path = workspace_root() / path
    if not path.is_file():
        raise ValueError(f'Required local file is missing: {path}')
    return fingerprint(path)


def stage_settings(stage: str, options: RunOptions) -> dict:
    if stage == 'asr':
        return {'model': file_digest(writable_path(options.model)), 'vad': file_digest(writable_path(options.vad_model)) if options.vad_model else None,
                'threads': options.threads, 'device': options.asr_device, 'range': [options.start, options.duration], 'backend': package_version('pywhispercpp')}
    if stage == 'diarization':
        from .diarization import MODEL_DIRECTORY
        return {'model': file_digest(writable_path(Path(MODEL_DIRECTORY) / 'sonafolio-model.json')), 'range': [options.start, options.duration],
                'device': options.speaker_device, 'speakers': options.num_speakers, 'backend': package_version('pyannote.audio')}
    if stage == 'refinement':
        from .refinement import MODEL_DIR
        return {'model': file_digest(writable_path(Path(MODEL_DIR) / 'sonafolio-model.json')), 'device': options.alignment_device,
                'backend': package_version('transformers'), 'torch': package_version('torch')}
    if stage == 'analysis':
        from .llm import load_settings
        from .skills import SkillRegistry
        registry = SkillRegistry(Path(options.skills_dir) if options.skills_dir else workspace_root() / 'skills')
        return {'settings': load_settings().model_dump(), 'skills': {name: skill.digest for name, skill in registry.loaded.items()},
                'skill': options.skill, 'prompt': file_digest(options.prompt) if options.prompt else None,
                'mode': options.analysis, 'merge_max_tokens': options.merge_max_tokens}
    return {}


def default_executor(stage: str, inputs: dict, options: RunOptions, output: Path, cancelled, force: bool = False) -> dict:
    request = output / 'request.json'
    response = output / 'response.json'
    write_json(request, {'stage': stage, 'inputs': inputs, 'options': asdict(options), 'output': str(output), 'force': force})
    run_stage_process([sys.executable, '-B', '-m', 'sonafolio._pipeline_worker', str(request), str(response)], output / 'stage.log', cancelled)
    return json.loads(response.read_text(encoding='utf-8'))


def artifact_hashes(output: Path, task: Path) -> dict:
    # Requests and logs are diagnostic, not semantic stage results.
    return {path.relative_to(task).as_posix(): fingerprint(writable_path(path))
            for path in sorted(output.rglob('*')) if path.is_file() and path.name not in {'request.json','response.json','stage.log','engine.log'}}


def reusable(attempt: dict, task: Path) -> bool:
    if attempt['state'] != 'completed' or not attempt.get('artifacts'):
        return False
    for relative, digest in attempt['artifacts'].items():
        path = writable_path(task / relative)
        if not path.is_relative_to(task) or not path.is_file() or fingerprint(path) != digest:
            return False
    return True


def run_pipeline(audio: Path, *, options: RunOptions | None = None, force: bool = False,
                 cancelled=lambda: False, on_event=lambda event: None, executor=default_executor) -> dict:
    options = options or RunOptions()
    options.validate()
    _, task = record_input(audio)
    with file_lock(task / '.pipeline.lock'):
        manifest = load_manifest(task)
        manifest['state'] = 'running'
        manifest['options'] = asdict(options)
        manifest['error_type'] = None
        manifest['current_stage'] = None
        save_manifest(task, manifest)
        def emit(stage, state, **fields):
            on_event({'task_id': task.name, 'stage': stage, 'state': state, **fields})
        def stage(name, inputs, filename):
            if cancelled():
                raise CancelledError('Task cancelled')
            manifest['current_stage'] = name
            save_manifest(task, manifest)
            settings = stage_settings(name, options)
            identity = {'stage': name, 'version': PIPELINE_VERSION, 'settings': settings,
                        'inputs': {key: file_digest(value) for key, value in inputs.items()}}
            signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            attempts = manifest['stages'].setdefault(name, [])
            if not force:
                for attempt in reversed(attempts):
                    if attempt['signature'] == signature and reusable(attempt, task):
                        output = writable_path(task / attempt['directory'])
                        if not output.is_relative_to(task):
                            raise ValueError('Cached attempt escaped its task directory')
                        emit(name, 'reused')
                        return output / filename if filename else output
            base = {'annotation':'alignment','refinement':'alignment'}.get(name, name)
            output = writable_path(task / base / (name + '-' + signature[:16] + '-' + uuid.uuid4().hex[:12]))
            # Incomplete analysis owns durable per-chunk checkpoints. Continue it
            # instead of discarding already-paid calls; completed versions stay immutable.
            if name == 'analysis' and not force:
                for previous in reversed(attempts):
                    if previous['signature'] == signature and previous['state'] in {'failed','cancelled','running'}:
                        output = writable_path(task / previous['directory'])
                        if not output.is_relative_to(task / 'analysis'):
                            raise ValueError('Analysis attempt escaped its task directory')
                        break
            output.mkdir(parents=True, exist_ok=True)
            attempt = {'signature': signature, 'state': 'running', 'directory': output.relative_to(task).as_posix(), 'started_at': now(), 'identity': identity}
            attempts.append(attempt)
            manifest['current_stage'] = name
            save_manifest(task, manifest)
            emit(name, 'running')
            try:
                executor(name, inputs, options, output, cancelled, force)
                if cancelled():
                    raise CancelledError('Task cancelled')
                required = [filename] if filename else (['summary.json','summary.md','sources.md','scene.json'] if options.analysis=='cloud' else ['checkpoints/request-plan.json','checkpoints/outbound-text.md'])
                if not all(writable_path(output / item).is_file() for item in required):
                    raise RuntimeError('Stage did not produce its required artifacts')
                attempt.update(state='completed', completed_at=now(), artifacts=artifact_hashes(output, task))
                if name == 'analysis' and options.analysis == 'cloud':
                    manifest['analysis_versions'].append(output.relative_to(task).as_posix())
                save_manifest(task, manifest)
                emit(name, 'completed')
                return output / filename if filename else output
            except BaseException as error:
                attempt.update(state='cancelled' if isinstance(error, (CancelledError, KeyboardInterrupt)) else 'failed', error_type=type(error).__name__)
                save_manifest(task, manifest)
                raise
        try:
            source = str(managed_input(task))
            transcript = stage('asr', {'audio':source}, 'transcript.json')
            speakers = stage('diarization', {'audio':source}, 'speakers.json')
            transcript = stage('annotation', {'transcript':str(transcript), 'speakers':str(speakers)}, 'transcript.json')
            if options.refine:
                transcript = stage('refinement', {'transcript':str(transcript), 'speakers':str(speakers)}, 'transcript.json')
            result = stage('analysis', {'transcript':str(transcript)}, None) if options.analysis != 'none' else transcript.parent
            manifest.update(state='preview_only' if options.analysis=='preview' else 'completed', current_stage=None,
                            latest_result=result.relative_to(task).as_posix())
            save_manifest(task, manifest)
            emit('pipeline', manifest['state'])
            return {'task_id': task.name, 'state': manifest['state'], 'run_directory': str(task), 'output_directory': str(result)}
        except BaseException as error:
            manifest.update(state='cancelled' if isinstance(error, (CancelledError, KeyboardInterrupt)) else 'failed', error_type=type(error).__name__)
            save_manifest(task, manifest)
            emit('pipeline', manifest['state'])
            raise


def resume_pipeline(task_id: str, *, analysis: str | None = None, **kwargs) -> dict:
    task = task_directory(task_id)
    options = RunOptions(**load_manifest(task).get('options', {}))
    if analysis is not None:
        options.analysis = analysis
    return run_pipeline(managed_input(task), options=options, **kwargs)
