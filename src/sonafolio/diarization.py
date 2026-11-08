"""Whole-recording speaker clustering, independent of inferred business roles."""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

from .audio import decode_audio, record_input
from .paths import writable_path, write_json, model_path

REPOSITORY = "pyannote/speaker-diarization-community-1"
MODEL_DIRECTORY = ".models/pyannote-community-1"


def read_token() -> str:
    token = os.environ.get("HF_TOKEN", "").strip()
    path = writable_path(".runtime/secrets/huggingface-token")
    if not token and path.is_file():
        token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("Configure a read token with sonafolio configure-hf after accepting community-1 access conditions")
    return token


def download_diarization() -> Path:
    import httpx
    from huggingface_hub import HfApi, snapshot_download
    from huggingface_hub.errors import HfHubHTTPError
    token = read_token()
    directory = model_path(MODEL_DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        info = HfApi().model_info(REPOSITORY, token=token, files_metadata=True)
        if not info.sha:
            raise RuntimeError("Model repository did not provide a revision")
        snapshot_download(REPOSITORY, revision=info.sha, token=token, local_dir=str(directory))
    except HfHubHTTPError as error:
        response = error.response
        status = response.status_code
        reason = response.headers.get("x-error-message", "")
        # Classify errors without exposing credentials or raw request objects.
        if status == 403 and "authorized list" in reason:
            message = "HF HTTP 403: this account has not been granted model access. Submit the community-1 access form using the same account that created the token"
        elif status == 403:
            message = "HF HTTP 403: verify token permissions and accepted community-1 access conditions"
        elif status == 401:
            message = "HF HTTP 401: token authentication was rejected; configure the read token again"
        else:
            message = f"HF HTTP {status}: model download failed; retry when the service is available"
        raise RuntimeError(message) from None
    except httpx.RequestError:
        raise RuntimeError("Could not connect to the model service; check network access and retry") from None
    checked_files = []
    for entry in info.siblings:
        if not entry.lfs:
            continue
        path = writable_path(directory / entry.rfilename)
        with path.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != entry.lfs.sha256:
            raise RuntimeError(f"Model checksum failed: {entry.rfilename}")
        checked_files.append({"name": entry.rfilename, "sha256": actual})
    write_json(directory / "sonafolio-model.json", {
        "repository": REPOSITORY, "revision": info.sha,
        "license": "CC-BY-4.0", "checked_weight_files": checked_files,
    })
    return directory


def normalize_speakers(turns: list[dict], exclusive: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Use first appearance to number speakers; do not infer a two-person limit."""
    first_seen = {}
    for item in [*turns, *exclusive]:
        speaker = item["speaker_id"]
        first_seen[speaker] = min(item["start"], first_seen.get(speaker, float("inf")))
    labels = sorted(first_seen, key=lambda label: (first_seen[label], label))
    mapping = {label: f"SPEAKER_{index:02d}" for index, label in enumerate(labels)}

    def rename(items):
        return sorted([
            {**item, "speaker_id": mapping[item["speaker_id"]]}
            for item in items
        ], key=lambda item: (item["start"], item["end"], item["speaker_id"]))

    return rename(turns), rename(exclusive), list(mapping.values())


def diarize_local(audio: Path, *, start: float = 0, duration: float | None = None, device: str = "cpu", num_speakers: int | None = None, output_directory: Path | None = None) -> dict:
    if device not in {"cpu", "mps", "cuda"}:
        raise ValueError("device must be cpu, mps or cuda")
    if num_speakers is not None and num_speakers < 1:
        raise ValueError("num_speakers must be positive when provided")
    model = model_path(MODEL_DIRECTORY)
    manifest_path = model / "sonafolio-model.json"
    if not manifest_path.is_file():
        raise RuntimeError("Local diarization model is missing; run download-diarization first")
    try:
        backend_version = version("pyannote.audio")
    except PackageNotFoundError:
        raise RuntimeError("Diarization dependencies are missing; run scripts/bootstrap.py --asr --diarization") from None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata, directory = record_input(audio)
    audio = Path(metadata["source"])
    samples = decode_audio(audio, start=start, duration=duration)
    settings = {
        "revision": manifest["revision"], "device": device,
        "num_speakers": num_speakers, "backend_version": backend_version,
    }
    key = hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]
    end = start + len(samples) / 16000
    output = writable_path(directory / "diarization" / f"{round(start*1000)}-{round(end*1000)}ms--{key}")
    if output_directory is not None:
        output = writable_path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "status.json", {"stage": "diarization", "state": "running"})
    began = time.monotonic()
    temporary_root = writable_path(".tmp")
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="diarization-", dir=temporary_root) as temporary:
            waveform = Path(temporary) / "samples.npy"
            raw_path = Path(temporary) / "turns.json"
            np.save(waveform, samples, allow_pickle=False)
            env = dict(os.environ, HF_HUB_OFFLINE="1", PYANNOTE_METRICS_ENABLED="0")
            # Offline inference uses local files and needs no API credential.
            env.pop("HF_TOKEN", None)
            env.pop("HUGGING_FACE_HUB_TOKEN", None)
            with (output / "engine.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "sonafolio._diarization_worker",
                     str(model), str(waveform), str(raw_path), device,
                     str(num_speakers) if num_speakers else "auto"],
                    env=env, stdout=log, stderr=subprocess.STDOUT, check=False,
                )
            if result.returncode:
                raise RuntimeError(f"Diarization worker exited with code {result.returncode}; see {output / 'engine.log'}")
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
        for key in ("turns", "exclusive_turns"):
            for item in raw[key]:
                item["start"] = round(start + item["start"], 3)
                item["end"] = round(start + item["end"], 3)
        turns, exclusive, speakers = normalize_speakers(raw["turns"], raw["exclusive_turns"])
        artifact = {
            "schema_version": 1, "state": "completed", "stage": "diarization",
            "source_sha256": metadata["sha256"], "settings": settings,
            "range": {"start": start, "end": end},
            "speaker_count": len(speakers), "speaker_ids": speakers,
            "turns": turns, "exclusive_turns": exclusive,
            "text_alignment": "pending", "roles": "not_inferred",
            "elapsed_seconds": round(time.monotonic() - began, 3),
        }
        write_json(output / "speakers.json", artifact)
        write_json(output / "status.json", {"stage": "diarization", "state": "completed", "text_alignment": "pending"})
        return {"output_directory": str(output), "speaker_count": len(speakers),
                "elapsed_seconds": artifact["elapsed_seconds"], "text_alignment": "pending"}
    except Exception as error:
        write_json(output / "status.json", {"stage": "diarization", "state": "failed", "error_type": type(error).__name__})
        raise
