"""Private native-inference worker. Run only through the parent ASR stage."""

import json
from pathlib import Path
import sys

import numpy as np


def main():
    from pywhispercpp.model import Model
    model_path, samples_path, result_path, threads, device, vad_path = sys.argv[1:]
    # Explicit file paths prevent pywhispercpp's implicit model downloader.
    model = Path(model_path)
    if not model.is_file():
        raise ValueError("Local model is missing")
    engine = Model(
        str(model), models_dir=str(model.parent), n_threads=int(threads),
        context_params={"use_gpu": device != "cpu"},
        language="zh", translate=False, print_progress=False,
        print_realtime=False, print_timestamps=False,
        initial_prompt="以下为中文对话，请使用简体中文，保留英文专有名词。",
        vad=vad_path != "-", vad_model_path=vad_path if vad_path != "-" else None,
    )
    pcm = np.load(samples_path, allow_pickle=False)
    segments = engine.transcribe(pcm)
    Path(result_path).write_text(json.dumps([
        {"t0": segment.t0, "t1": segment.t1, "text": segment.text}
        for segment in segments
    ], ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
