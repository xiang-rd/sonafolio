"""Read input audio with PyAV; no system ffmpeg executable is needed."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import av
import numpy as np



def fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def probe_audio(path: Path) -> dict:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("Input must be an audio file")
    with av.open(str(path), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("Input contains no audio stream")
        stream = container.streams.audio[0]
        duration = (
            float(stream.duration * stream.time_base)
            if stream.duration is not None and stream.time_base is not None
            else container.duration / av.time_base if container.duration is not None else None
        )
        return {
            "source": str(path), "size_bytes": path.stat().st_size,
            "sha256": fingerprint(path), "format": container.format.name,
            "duration_seconds": duration, "codec": stream.codec_context.name,
            "sample_rate": stream.codec_context.sample_rate,
            "channels": stream.codec_context.channels,
            "channel_layout": stream.codec_context.layout.name,
            "audio_streams": len(container.streams.audio),
        }


def run_directory(metadata: dict) -> Path:
    from .tasks import task_directory
    return task_directory(metadata["sha256"])


def record_input(path: Path) -> tuple[dict, Path]:
    from .tasks import import_recording
    metadata = probe_audio(path)
    return import_recording(path.resolve(strict=True), metadata)


def decode_audio(path: Path, *, start: float = 0, duration: float | None = None, rate: int = 16000) -> np.ndarray:
    """Return mono float32 PCM; preserve timing across resampling and seek offsets."""
    if not math.isfinite(start) or start < 0:
        raise ValueError("start must be finite and non-negative")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError("duration must be finite and positive")
    if rate <= 0:
        raise ValueError("sample rate must be positive")
    first = round(start * rate)
    last = None if duration is None else first + round(duration * rate)
    pieces = []
    cursor = 0
    with av.open(str(path.resolve(strict=True)), mode="r") as container:
        if not container.streams.audio:
            raise ValueError("Input contains no audio stream")
        stream = container.streams.audio[0]
        # Decode from the start: exact sample slicing avoids container-dependent
        # seek offsets and preserves absolute timestamps for the first baseline.
        resampler = av.AudioResampler(format="flt", layout="mono", rate=rate)

        def collect(frame) -> bool:
            nonlocal cursor
            samples = frame.to_ndarray().reshape(-1)
            end = cursor + samples.size
            lower, upper = max(first, cursor), min(end, last) if last is not None else end
            if upper > lower:
                pieces.append(samples[lower - cursor:upper - cursor].copy())
            cursor = end
            return last is not None and cursor >= last

        finished = False
        for frame in container.decode(stream):
            for pcm in resampler.resample(frame):
                if collect(pcm):
                    finished = True
                    break
            if finished:
                break
        if not finished:
            for pcm in resampler.resample(None):
                collect(pcm)
    if not pieces:
        raise ValueError("Selected range contains no decodable audio")
    return np.concatenate(pieces).astype(np.float32, copy=False)
