"""Private one-stage process entry; stdout/stderr are local diagnostic logs."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .paths import write_json


def execute(name: str, inputs: dict, options: dict, output: Path, force: bool = False) -> dict:
    if name == 'asr':
        from .transcribe import transcribe_local
        return transcribe_local(Path(inputs['audio']), model=Path(options['model']), vad_model=Path(options['vad_model']) if options['vad_model'] else None,
                                start=options['start'], duration=options['duration'], threads=options['threads'], device=options['asr_device'], output_directory=output)
    if name == 'diarization':
        from .diarization import diarize_local
        return diarize_local(Path(inputs['audio']), start=options['start'], duration=options['duration'], device=options['speaker_device'], num_speakers=options['num_speakers'], output_directory=output)
    if name == 'annotation':
        from .annotation import annotate_recording
        return annotate_recording(Path(inputs['transcript']), Path(inputs['speakers']), output_directory=output)
    if name == 'refinement':
        from .refinement import refine_recording
        return refine_recording(Path(inputs['transcript']), Path(inputs['speakers']), device=options['alignment_device'], output_directory=output)
    if name == 'analysis':
        from .analysis import analyze_recording
        return analyze_recording(Path(inputs['transcript']), skill_id=options['skill'], skills_dir=Path(options['skills_dir']) if options['skills_dir'] else None,
                                 prompt_path=Path(options['prompt']) if options['prompt'] else None, dry_run=options['analysis']=='preview', merge_max_tokens=options['merge_max_tokens'], analysis_directory=output, force=force)
    raise ValueError('Unknown pipeline stage')


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    result = execute(request['stage'], request['inputs'], request['options'], Path(request['output']), force=request.get('force', False))
    write_json(Path(sys.argv[2]), result)
