"""Private offline worker; inputs are PCM so system FFmpeg is unnecessary."""

import json
from pathlib import Path
import sys

import numpy as np


def main():
    import torch
    from pyannote.audio import Pipeline
    model, waveform, output, device, count = sys.argv[1:]
    torch.set_num_threads(4)
    pipeline = Pipeline.from_pretrained(model, token=False)
    if pipeline is None:
        raise RuntimeError("Could not load the local diarization pipeline")
    pipeline.to(torch.device(device))
    options = {} if count == "auto" else {"num_speakers": int(count)}
    pcm = torch.from_numpy(np.load(waveform, allow_pickle=False)).unsqueeze(0)
    result = pipeline({"waveform": pcm, "sample_rate": 16000}, **options)

    def turns(annotation):
        return [
            {"start": float(segment.start), "end": float(segment.end), "speaker_id": str(label)}
            for segment, _, label in annotation.itertracks(yield_label=True)
        ]

    Path(output).write_text(json.dumps({
        "turns": turns(result.speaker_diarization),
        "exclusive_turns": turns(result.exclusive_speaker_diarization),
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
