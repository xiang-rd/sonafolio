"""Explicit copy migration: old recordings and results remain untouched."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from .audio import fingerprint, record_input
from .paths import workspace_path, writable_path, write_json
from .tasks import file_lock, load_manifest, managed_input, now, rebuild_index, save_manifest, task_directory


def migration_plan(legacy: Path) -> dict:
    legacy = workspace_path(legacy)
    if (legacy / 'manifest.json').exists():
        raise ValueError('This is already a managed task, not a legacy run')
    metadata = json.loads((legacy / 'input.json').read_text(encoding='utf-8'))
    audio = Path(metadata['source']).resolve(strict=True)
    if fingerprint(audio) != metadata['sha256']:
        raise ValueError('Legacy input fingerprint mismatch; nothing was changed')
    summaries = []
    # Old layout always used workspace/data, even when the new data root differs.
    from .paths import workspace_root
    for path in sorted((workspace_root() / 'data/output').glob('**/summary.json')):
        path = workspace_path(path)
        summary = json.loads(path.read_text(encoding='utf-8'))
        transcript = Path(summary['metadata'].get('input_transcript', ''))
        belongs = transcript.is_absolute() and transcript.resolve().is_relative_to(legacy)
        if summary['metadata']['source_sha256'] == metadata['sha256'] and (belongs or path.parent.name.startswith(legacy.name + '--')):
            summaries.append(str(path.parent))
    summaries.extend(str(path.parent) for path in sorted((legacy/'analysis').glob('*/summary.json')))
    return {'legacy': str(legacy), 'task_id': metadata['sha256'], 'source': str(audio),
            'task_directory': str(task_directory(metadata['sha256'])), 'results': summaries,
            'operation': 'copy', 'originals_retained': True}


def rewrite(value, mappings):
    if isinstance(value, dict):
        return {key: rewrite(child, mappings) for key, child in value.items()}
    if isinstance(value, list):
        return [rewrite(child, mappings) for child in value]
    if isinstance(value, str):
        for old, new in sorted(mappings, key=lambda pair: -len(pair[0])):
            if value == old or value.startswith(old + '/') or value.startswith(old + '\\'):
                return new + value[len(old):]
    return value


def copy_tree(source: Path, destination: Path, mappings: list[tuple[str,str]]) -> None:
    from .paths import workspace_root
    for item in source.rglob('*'):
        # Refuse every symlink, including internal ones: migration must be self-contained.
        if item.is_symlink() or not item.resolve().is_relative_to(workspace_root()):
            raise ValueError('Legacy tree contains a symlink or an escaping path')
        target = writable_path(destination / item.relative_to(source))
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.suffix == '.json':
                write_json(target, rewrite(json.loads(item.read_text(encoding='utf-8')), mappings))
            else:
                shutil.copy2(item, target)
                if fingerprint(target) != fingerprint(item):
                    raise ValueError('Migration copy checksum mismatch')


def migrate_legacy(legacy: Path, *, apply: bool = False) -> dict:
    plan = migration_plan(legacy)
    if not apply:
        return {**plan, 'state': 'preview_only'}
    legacy = Path(plan['legacy'])
    _, task = record_input(Path(plan['source']))
    migration_id = hashlib.sha256(str(legacy).encode()).hexdigest()[:16]
    with file_lock(task / '.pipeline.lock'):
        manifest = load_manifest(task)
        if migration_id in manifest.get('migrations', {}):
            rebuild_index()
            return {**plan, 'state':'already_migrated'}
        staging = writable_path(task / ('.migration-' + uuid.uuid4().hex))
        staging.mkdir()
        published = []
        try:
            mappings = [(plan['source'], str(managed_input(task))), (str(legacy), str(task))]
            copies = []
            for name in ('asr','diarization','annotated','refined'):
                for version in sorted((legacy / name).glob('*')):
                    if not version.is_dir():
                        continue
                    stage = 'alignment' if name in {'annotated','refined'} else name
                    relative = Path(stage) / ('legacy-' + migration_id + '-' + name + '-' + version.name)
                    mappings.append((str(version), str(task / relative)))
                    copies.append((version,relative))
            for output_string in plan['results']:
                output = Path(output_string)
                relative = Path('analysis') / ('legacy-' + migration_id + '-' + output.name)
                mappings.append((str(output),str(task / relative)))
                copies.append((output,relative))
            for checkpoint in sorted((legacy / 'analysis').glob('*')):
                if checkpoint.is_dir():
                    mappings.append((str(checkpoint), str(task / 'analysis' / ('legacy-checkpoints-' + migration_id + '-' + checkpoint.name) / 'checkpoints')))
            for source, relative in copies:
                copy_tree(source, staging / relative, mappings)
            for output_string in plan['results']:
                source = Path(output_string)
                mapped = Path(rewrite(str(source), mappings)).relative_to(task)
                output = staging / mapped
                summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
                metadata = summary['metadata']
                transcript = Path(metadata['input_transcript'])
                if not transcript.is_absolute():
                    transcript = Path(rewrite(str(legacy / transcript), mappings))
                if not transcript.is_relative_to(task):
                    raise ValueError('Legacy summary references a transcript outside this task')
                transcript_relative = transcript.relative_to(task)
                if not (staging / transcript_relative).is_file():
                    raise ValueError('Legacy summary transcript was not copied')
                metadata['input_transcript'] = transcript_relative.as_posix()
                metadata['task_id'] = task.name
                write_json(output / 'summary.json', summary)
                if not all((output / name).is_file() for name in ('summary.md','sources.md')):
                    raise ValueError('Legacy summary is missing Markdown or source references')
                sources = (output / 'sources.md').read_text(encoding='utf-8')
                from .analysis import validate_references
                ids = set(re.findall(r'<a id="([A-Za-z0-9._-]+)"', sources))
                # Existing participant IDs are already in the checked legacy result.
                speakers = {p['speaker_id'] for p in summary['result'].get('participants', [])}
                validate_references(summary['result'], ids, speakers)
                links = re.findall(r'sources\.md#([A-Za-z0-9._-]+)', (output / 'summary.md').read_text(encoding='utf-8'))
                if not set(links).issubset(ids):
                    raise ValueError('Legacy summary has broken source links')
                checkpoint_name = source.name.removeprefix(legacy.name + '--').split('-m')[0]
                checkpoints = legacy / 'analysis' / checkpoint_name
                if checkpoints.is_dir():
                    copy_tree(checkpoints, output / 'checkpoints', mappings)
                # Historical definitions must never be replaced by today's Skill.
                write_json(output / 'scene.json', {'schema_version':1, 'state':'legacy_snapshot_unavailable',
                                                  'skill_id':metadata['skill_id'], 'skill_version':metadata.get('skill_version'),
                                                  'skill_digest':metadata.get('skill_digest')})
                manifest['analysis_versions'].append(mapped.as_posix())
            # Preserve orphan previews/failed checkpoints too, even without a summary.
            for old in sorted((legacy / 'analysis').glob('*')):
                if old.is_dir() and not (old / 'summary.json').is_file():
                    relative = Path('analysis') / ('legacy-checkpoints-' + migration_id + '-' + old.name)
                    copy_tree(old, staging / relative / 'checkpoints', mappings)
            for group in sorted(staging.iterdir()):
                destination = writable_path(task / group.name)
                destination.mkdir(exist_ok=True)
                for child in sorted(group.iterdir()):
                    target = writable_path(destination / child.name)
                    if target.exists():
                        raise ValueError('Migration target exists; no existing artifact will be overwritten')
                    child.rename(target)
                    published.append(target)
            # Index rebuild validates all content before its transaction.
            rebuild_index()
            manifest.setdefault('migrations', {})[migration_id] = {'source': str(legacy), 'completed_at': now(), 'originals_retained': True}
            save_manifest(task, manifest)
        except BaseException:
            for target in reversed(published):
                shutil.rmtree(target)
            # Restore managed rows if publication failed after index rebuilding.
            rebuild_index()
            raise
        finally:
            shutil.rmtree(staging)
    return {**plan, 'state':'migrated'}
