"""Validated storage changes; copy first, verify, then switch the configuration."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
import uuid
from .paths import workspace_root, writable_path, workspace_path
from .storage_config import storage_roots
from .tasks import file_lock
from .processes import CancelledError


def files_under(root: Path):
    if not root.exists(): return []
    files = []
    for path in root.rglob('*'):
        if path.is_symlink(): raise ValueError('Storage migration does not follow symlinks')
        if path.is_file(): files.append(path)
    return files


def plan_storage(directories: dict, mode: str) -> dict:
    if mode not in {'existing', 'copy'}: raise ValueError('Unknown storage mode')
    old = storage_roots(workspace_root()); new = storage_roots(workspace_root(), directories)
    items = []
    for key in old:
        if old[key] == new[key]: continue
        # A new root cannot contain or be contained by any previous root.
        for source in old.values():
            if new[key] == source or source in new[key].parents or new[key] in source.parents:
                raise ValueError('New storage must not overlap an existing storage root')
        files = files_under(old[key]) if mode == 'copy' else []
        if mode == 'copy' and new[key].exists() and any(new[key].iterdir()):
            raise ValueError('Copy destination must be empty; use an existing directory instead')
        items.append({'key': key, 'source': str(old[key]), 'target': str(new[key]),
                      'files': len(files), 'bytes': sum(path.stat().st_size for path in files)})
    return {'mode': mode, 'items': items, 'bytes': sum(item['bytes'] for item in items),
            'directories': {key: path.relative_to(workspace_root()).as_posix() for key, path in new.items()}}


def apply_storage(directories: dict, mode: str, *, cancelled=lambda: False, on_event=lambda event: None) -> dict:
    from .desktop_settings import save_settings
    with file_lock(writable_path('.runtime/locks/storage.lock')):
        plan = plan_storage(directories, mode); staged = []; published = []; copied = 0
        config=writable_path('sonafolio.toml');original=config.read_bytes() if config.exists() else None
        try:
            for item in plan['items']:
                if cancelled(): raise CancelledError('Storage change cancelled')
                target = writable_path(item['target'])
                if mode == 'existing': continue
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = writable_path(target.parent / ('.sonafolio-copy-' + uuid.uuid4().hex))
                stage.mkdir(); staged.append(stage)
                (stage/'.gitignore').write_text('# Sonafolio private storage\n*\n')
                source = Path(item['source'])
                for path in files_under(source):
                    path = workspace_path(path)
                    output = workspace_path(stage / path.relative_to(source))
                    if not output.is_relative_to(stage):raise ValueError('Copy escaped its staging directory')
                    output.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with path.open('rb') as incoming, output.open('wb') as outgoing:
                        while data := incoming.read(1024 * 1024):
                            if cancelled(): raise CancelledError('Storage copy cancelled')
                            digest.update(data); outgoing.write(data); copied += len(data)
                            on_event({'stage': 'storage', 'state': 'copying', 'bytes': copied, 'total_bytes': plan['bytes']})
                    with output.open('rb') as stream: actual = hashlib.file_digest(stream, 'sha256').digest()
                    if actual != digest.digest(): raise RuntimeError('Storage copy verification failed')
                    shutil.copystat(path, output)
                # Sources stay intact. Only publish into a still-empty destination.
                if target.exists():
                    if any(target.iterdir()): raise ValueError('Destination changed during migration')
                    target.rmdir()
                stage.rename(target); published.append(target)
            if cancelled(): raise CancelledError('Storage change cancelled')
            result = save_settings(**plan['directories'])
            return {'state': 'completed', 'settings': result, 'copied_bytes': copied}
        except BaseException:
            if original is None:
                if config.exists():config.unlink()
            else:
                temporary=writable_path(config.with_suffix('.rollback'));temporary.write_bytes(original);temporary.replace(config)
            from .paths import apply_cache_environment
            apply_cache_environment()
            # Only our new copies are removed; original files are never deleted.
            for path in reversed(published): shutil.rmtree(path)
            raise
        finally:
            for path in staged:
                if path.exists(): shutil.rmtree(path)
