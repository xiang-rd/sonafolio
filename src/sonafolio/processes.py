"""Run one isolated stage and terminate its entire process tree on cancellation."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time


class CancelledError(RuntimeError):
    pass


def run_stage_process(command: list[str], log: Path, cancelled=lambda: False) -> None:
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, **options)
        try:
            while process.poll() is None:
                if cancelled():
                    raise CancelledError('Task cancelled')
                time.sleep(0.05)
            if cancelled():
                raise CancelledError('Task cancelled')
            if process.returncode:
                raise RuntimeError(f'Stage exited with code {process.returncode}; see {log}')
        except BaseException:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    # A worker may exit before its native inference child.
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
            raise
