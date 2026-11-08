"""Managed recording tasks, portable manifests and recoverable result indexes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tomllib
import uuid

from .paths import workspace_root, workspace_path, writable_path, write_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def data_root(relative: str | None = None) -> Path:
    from .storage_config import storage_roots
    return storage_roots(workspace_root(), {'data_dir': relative} if relative is not None else None)['data_dir']


def ensure_data_root() -> Path:
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    ignore = writable_path(root / '.gitignore')
    previous = ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    if '# Sonafolio private data' not in previous:
        ignore.write_text(previous + '\n# Sonafolio private data\n*\n', encoding='utf-8')
    return root


@contextmanager
def file_lock(path: Path):
    """OS releases this lock on crash; the persistent lock file is harmless."""
    path = writable_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('This task or data store is already in use; retry after it finishes') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def task_directory(source_hash: str) -> Path:
    if not re.fullmatch('[a-f0-9]{64}', source_hash):
        raise ValueError('A task requires a complete SHA-256 fingerprint')
    return writable_path(data_root() / 'runs' / source_hash)


def task_for_artifact(path: Path) -> Path:
    path = writable_path(path)
    for parent in path.parents:
        if not parent.is_relative_to(workspace_root()):
            break
        if (parent / 'manifest.json').is_file():
            load_manifest(parent)
            return parent
    # Legacy artifacts retain their original two-level stage/version layout.
    return writable_path(path.parents[2])


def load_manifest(task: Path) -> dict:
    task = writable_path(task)
    manifest = json.loads(workspace_path(task / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or manifest.get('task_id') != task.name or not isinstance(manifest.get('input'), dict) or manifest['input'].get('sha256') != task.name:
        raise ValueError('Unsupported or mismatched task manifest')
    source = workspace_path(task / manifest['input']['path'])
    if not source.is_relative_to(task / 'input'):
        raise ValueError('Task input must remain inside its input directory')
    return manifest


def save_manifest(task: Path, manifest: dict) -> None:
    manifest['updated_at'] = now()
    write_json(task / 'manifest.json', manifest)


def managed_input(task: Path) -> Path:
    return workspace_path(task / load_manifest(task)['input']['path'])


def import_recording(audio: Path, metadata: dict) -> tuple[dict, Path]:
    from .audio import fingerprint
    task = task_directory(metadata['sha256'])
    root = ensure_data_root()
    with file_lock(root / '.import.lock'):
        if (task / 'manifest.json').is_file():
            load_manifest(task)
            source = managed_input(task)
            if fingerprint(source) != metadata['sha256']:
                raise ValueError('Managed audio has changed; restore it from backup')
            return {**metadata, 'source': str(source)}, task
        if task.exists():
            raise ValueError('Task directory exists without a manifest; preserve it and inspect before retrying')
        stage = writable_path(root / 'runs' / ('.import-' + uuid.uuid4().hex))
        source = writable_path(stage / 'input' / ('recording' + audio.suffix.lower()))
        source.parent.mkdir(parents=True)
        try:
            shutil.copy2(audio, source)
            if fingerprint(source) != metadata['sha256']:
                raise ValueError('Audio changed while importing; original file was not modified')
            created = now()
            manifest = {'schema_version': 1, 'task_id': task.name,
                        'title': audio.stem, 'created_at': created, 'updated_at': created,
                        'state': 'imported', 'input': {**metadata, 'path': source.relative_to(stage).as_posix()},
                        'stages': {}, 'analysis_versions': [], 'batch_ids': []}
            # Absolute source is informational only; managed path is authoritative.
            write_json(stage / 'manifest.json', manifest)
            stage.rename(task)
        except BaseException:
            shutil.rmtree(stage)
            raise
    return {**metadata, 'source': str(managed_input(task))}, task


def rename_task(task_id: str, title: str) -> dict:
    if not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise ValueError('Title must contain 1–200 printable characters')
    task = task_directory(task_id)
    load_manifest(task)
    with file_lock(task / '.pipeline.lock'):
        manifest = load_manifest(task)
        manifest['title'] = title.strip()
        save_manifest(task, manifest)
    return {'task_id': task_id, 'title': manifest['title']}


def validate_analysis_output(task: Path, output: Path, source_hash: str) -> dict:
    from .analysis import validate_references
    summary = json.loads(writable_path(output / 'summary.json').read_text(encoding='utf-8'))
    if summary['metadata']['source_sha256'] != source_hash:
        raise ValueError('Analysis belongs to a different recording')
    for name in ('summary.md', 'sources.md', 'scene.json'):
        if not writable_path(output / name).is_file():
            raise ValueError('Analysis is incomplete; index was not changed')
    transcript = writable_path(task / summary['metadata']['input_transcript'])
    if not transcript.is_relative_to(task) or not transcript.is_file():
        raise ValueError('Analysis transcript reference is missing or outside its task')
    artifact = json.loads(transcript.read_text(encoding='utf-8'))
    if artifact['source_sha256'] != source_hash:
        raise ValueError('Analysis transcript fingerprint does not match')
    sources = (output / 'sources.md').read_text(encoding='utf-8')
    ids = set(re.findall(r'<a id="([A-Za-z0-9._-]+)"', sources))
    speakers = {item['speaker_id'] for item in artifact.get('segments', []) if item.get('speaker_id')}
    validate_references(summary.get('result', {}), ids, speakers)
    links = re.findall(r'sources\.md#([A-Za-z0-9._-]+)', (output / 'summary.md').read_text(encoding='utf-8'))
    if not set(links).issubset(ids):
        raise ValueError('Analysis has broken source links')
    return summary


def index_analysis(task: Path, output: Path) -> None:
    output = writable_path(output)
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    root = ensure_data_root()
    with sqlite3.connect(writable_path(root / 'index.sqlite')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS analyses (job_id TEXT PRIMARY KEY, source_sha256 TEXT, skill_id TEXT, output_path TEXT, state TEXT, created_at TEXT)')
        db.execute('INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?)',
                   (task.name + ':' + output.name, summary['metadata']['source_sha256'],
                    summary['metadata']['skill_id'], str(output), 'completed', now()))


def rebuild_index() -> dict:
    """Validate first, then atomically replace managed rows; keep legacy rows."""
    from .audio import fingerprint
    root = data_root()
    rows = []
    tasks = []
    for path in sorted((root / 'runs').glob('*/manifest.json')):
        task = writable_path(path.parent)
        manifest = load_manifest(task)
        if fingerprint(managed_input(task)) != manifest['input']['sha256']:
            raise ValueError('Task audio fingerprint mismatch during index rebuild')
        tasks.append(manifest)
        for output in sorted((task / 'analysis').glob('*/summary.json')):
            output = writable_path(output)
            summary = validate_analysis_output(task, output.parent, manifest['input']['sha256'])
            rows.append((task.name + ':' + output.parent.name, manifest['input']['sha256'],
                         summary['metadata']['skill_id'], str(output.parent), 'completed', manifest['created_at']))
    ensure_data_root()
    with sqlite3.connect(writable_path(root / 'index.sqlite')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS analyses (job_id TEXT PRIMARY KEY, source_sha256 TEXT, skill_id TEXT, output_path TEXT, state TEXT, created_at TEXT)')
        for (identifier,) in db.execute('SELECT job_id FROM analyses').fetchall():
            if re.fullmatch('[a-f0-9]{64}:.*', identifier):
                db.execute('DELETE FROM analyses WHERE job_id=?', (identifier,))
        db.executemany('INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?)', rows)
    return {'tasks': len(tasks), 'analyses': len(rows), 'legacy_rows_preserved': True}
