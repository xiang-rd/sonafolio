"""Explicit model acquisition; inference never downloads models implicitly."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .paths import writable_path, write_json, model_path

REPO = "ggerganov/whisper.cpp"
FILENAME = "ggml-large-v3-turbo-q5_0.bin"


def download_baseline() -> Path:
    return download_model(REPO, FILENAME, ".models/whisper.cpp")


def download_vad() -> Path:
    return download_model("ggml-org/whisper-vad", "ggml-silero-v6.2.0.bin", ".models/whisper-vad")


def download_model(repository: str, filename: str, destination: str) -> Path:
    from huggingface_hub import HfApi, hf_hub_download
    directory = model_path(destination)
    directory.mkdir(parents=True, exist_ok=True)
    info = HfApi().model_info(repository, files_metadata=True, token=False)
    entry = next(item for item in info.siblings if item.rfilename == filename)
    expected = entry.lfs.sha256 if entry.lfs else None
    if not info.sha or not expected:
        raise RuntimeError("Model registry did not provide a revision and SHA-256")
    model = Path(hf_hub_download(
        repo_id=repository, filename=filename, revision=info.sha,
        local_dir=str(directory), token=False,
    ))
    with model.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected:
        raise RuntimeError("Model SHA-256 verification failed; inference was not started")
    write_json(directory / "model.json", {
        "repository": repository, "revision": info.sha, "filename": filename,
        "sha256": actual, "size_bytes": model.stat().st_size,
        "source": f"https://huggingface.co/{repository}/blob/{info.sha}/{filename}",
    })
    return model
