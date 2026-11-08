"""Local ASR baseline, intentionally separate from speaker attribution."""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import math
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

from .audio import decode_audio, record_input
from .paths import writable_path, write_json


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remaining = divmod(milliseconds, 3600000)
    minutes, remaining = divmod(remaining, 60000)
    seconds, milliseconds = divmod(remaining, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def transcribe_local(audio: Path, *, model: Path, start: float = 0, duration: float | None = None, threads: int = 4, device: str = "auto", vad_model: Path | None = None, output_directory: Path | None = None) -> dict:
    try:
        backend_version = version("pywhispercpp")
    except PackageNotFoundError:
        raise RuntimeError("Local ASR adapter is missing; run scripts/bootstrap.py --asr") from None
    model = writable_path(model)
    if not model.is_file():
        raise ValueError("Model file is missing; use download-asr first")
    if not math.isfinite(start) or start < 0 or (duration is not None and (not math.isfinite(duration) or duration <= 0)):
        raise ValueError("Invalid audio range")
    if threads < 1:
        raise ValueError("threads must be positive")
    if device not in {"auto", "cpu"}:
        raise ValueError("device must be auto or cpu")
    if vad_model is not None:
        vad_model = writable_path(vad_model)
        if not vad_model.is_file():
            raise ValueError("VAD model is missing; use download-asr first")
    metadata, directory = record_input(audio)
    audio = Path(metadata["source"])
    pcm = decode_audio(audio, start=start, duration=duration)
    end = start + len(pcm) / 16000
    selection = f"{round(start * 1000)}-{round(end * 1000)}ms"
    with model.open("rb") as stream:
        model_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    vad_hash = None
    if vad_model:
        with vad_model.open("rb") as stream:
            vad_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    settings = {"model_sha256": model_hash, "vad_sha256": vad_hash,
                "threads": threads, "device": device, "language": "zh",
                "backend_version": backend_version, "prompt_version": 2}
    settings_hash = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]
    output = writable_path(directory / "asr" / f"{selection}--{settings_hash}")
    if output_directory is not None:
        output = writable_path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    write_json(output / "status.json", {"stage": "asr", "state": "running", "speakers": "pending"})
    try:
        # Native backends can terminate the interpreter on a device error.
        # Keep inference in a child so the parent records a failed stage reliably.
        temporary_root = writable_path(".tmp")
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="asr-", dir=temporary_root) as temporary:
            samples = Path(temporary) / "samples.npy"
            raw_output = Path(temporary) / "segments.json"
            np.save(samples, pcm, allow_pickle=False)
            with (output / "engine.log").open("w", encoding="utf-8") as log:
                completed = subprocess.run(
                    [sys.executable, "-B", "-m", "sonafolio._whisper_worker",
                     str(model), str(samples), str(raw_output), str(threads), device,
                     str(vad_model) if vad_model else "-"],
                    stdout=log, stderr=subprocess.STDOUT, check=False,
                )
            if completed.returncode:
                raise RuntimeError(f"ASR worker exited with code {completed.returncode}; see {output / 'engine.log'}")
            raw = json.loads(raw_output.read_text(encoding="utf-8"))
        segments = [
            {"id": f"seg-{index:06d}", "start": round(start + segment["t0"] / 100, 3),
             "end": round(start + segment["t1"] / 100, 3), "text": segment["text"].strip(),
             "speaker_id": None, "role": "unknown"}
            for index, segment in enumerate(raw) if segment["text"].strip()
        ]
        result = {
            "schema_version": 1, "stage": "asr", "state": "completed",
            "diarization": {"state": "pending", "reason": "ASR does not assign speakers"},
            "source_sha256": metadata["sha256"], "language": "zh",
            "range": {"start": start, "end": end},
            "settings": settings,
            "backend": {"name": "pywhispercpp", "version": backend_version,
                        "model": model.name, "model_sha256": model_hash, "threads": threads,
                        "device_requested": device},
            "elapsed_seconds": round(time.monotonic() - began, 3), "segments": segments,
        }
        write_json(output / "transcript.json", result)
        markdown = "# ASR 基线转写\n\n说话人区分尚未执行，以下内容不能用于判断候选人与面试官的归属。\n\n"
        markdown += "\n\n".join(
            f"[{timestamp(item['start'])} → {timestamp(item['end'])}] {item['text']}"
            for item in segments
        ) + "\n"
        writable_path(output / "transcript.md").write_text(markdown, encoding="utf-8")
        write_json(output / "status.json", {"stage": "asr", "state": "completed", "speakers": "pending"})
        return {"output_directory": str(output), "segments": len(segments),
                "audio_seconds": round(end - start, 3), "elapsed_seconds": result["elapsed_seconds"],
                "diarization": "pending"}
    except Exception as error:
        write_json(output / "status.json", {"stage": "asr", "state": "failed", "error_type": type(error).__name__, "speakers": "pending"})
        raise
