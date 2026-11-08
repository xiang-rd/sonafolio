"""Refine ambiguous text conservatively, preserving every source character."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata

import numpy as np

from .annotation import annotate_segments
from .audio import decode_audio, fingerprint
from .paths import writable_path, write_json, model_path
from .transcribe import timestamp

MODEL_DIR = ".models/chinese-alignment"
MODEL_REPO = "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn"
MODEL_FILES = ["config.json", "preprocessor_config.json", "tokenizer_config.json", "vocab.json", "special_tokens_map.json", "pytorch_model.bin"]


def download_alignment() -> Path:
    from huggingface_hub import HfApi, snapshot_download
    directory = model_path(MODEL_DIR)
    info = HfApi().model_info(MODEL_REPO, files_metadata=True, token=False)
    snapshot_download(MODEL_REPO, revision=info.sha, allow_patterns=MODEL_FILES, local_dir=str(directory), token=False)
    checked = []
    for item in info.siblings:
        if item.rfilename in MODEL_FILES and item.lfs:
            actual = fingerprint(directory / item.rfilename)
            if actual != item.lfs.sha256:
                raise RuntimeError("Chinese alignment model checksum mismatch")
            checked.append({"name": item.rfilename, "sha256": actual})
    write_json(directory / "sonafolio-model.json", {"repository": MODEL_REPO, "revision": info.sha, "license": "Apache-2.0", "checked_weight_files": checked})
    return directory


def refine_segment(segment: dict, characters: list, turns: list[dict]) -> list[dict]:
    if len(characters) != len(segment["text"]):
        raise ValueError("Character alignment length does not match source text")
    if not any(item and item["score"] >= 0.15 for item in characters):
        return [{**segment, "source_segment_id": segment["id"], "refinement": "no_reliable_character_alignment"}]
    units, prefix = [], ""
    for character, timing in zip(segment["text"], characters):
        if not timing and (character.isspace() or unicodedata.category(character).startswith("P")):
            if units:
                units[-1]["text"] += character
            else:
                prefix += character
            continue
        if timing and timing["score"] >= 0.15 and timing["end"] > timing["start"]:
            unit = annotate_segments([{"text": prefix + character, "start": timing["start"], "end": timing["end"]}], turns)[0]
            unit["alignment_precision"] = "character_estimate"
        else:
            unit = {"text": prefix + character, "start": segment["start"], "end": segment["end"], "speaker_id": None,
                    "speaker_candidates": segment.get("speaker_candidates", []), "role": "unknown",
                    "speaker_assignment": "unaligned_character", "alignment_precision": "unavailable"}
        prefix = ""
        if units and units[-1]["speaker_id"] == unit["speaker_id"] and units[-1]["alignment_precision"] == unit["alignment_precision"]:
            units[-1]["text"] += unit["text"]
            units[-1]["start"] = min(units[-1]["start"], unit["start"])
            units[-1]["end"] = max(units[-1]["end"], unit["end"])
        else:
            units.append(unit)
    if prefix:
        if units:
            units[-1]["text"] += prefix
        else:
            return [segment]
    for index, unit in enumerate(units):
        unit.update({"id": f"{segment['id']}.part-{index:03d}", "source_segment_id": segment["id"]})
    if "".join(item["text"] for item in units) != segment["text"]:
        raise RuntimeError("Refinement changed source text; result rejected")
    return units


def refine_recording(transcript_path: Path, speakers_path: Path, *, device: str = "cpu", limit: int | None = None, output_directory: Path | None = None) -> dict:
    if device not in {"cpu", "mps", "cuda"}:
        raise ValueError("Unsupported alignment device")
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    speakers = json.loads(speakers_path.read_text(encoding="utf-8"))
    if transcript["source_sha256"] != speakers["source_sha256"]:
        raise ValueError("Alignment inputs belong to different recordings")
    from .tasks import task_for_artifact, managed_input
    run = task_for_artifact(transcript_path)
    audio = managed_input(run) if (run / "manifest.json").is_file() else Path(json.loads((run / "input.json").read_text(encoding="utf-8"))["source"])
    if fingerprint(audio) != transcript["source_sha256"]:
        raise ValueError("Source audio has changed")
    model = model_path(MODEL_DIR)
    manifest = json.loads((model / "sonafolio-model.json").read_text(encoding="utf-8"))
    settings = {"model_revision": manifest["revision"], "device": device, "limit": limit, "version": 1}
    key = hashlib.sha256(transcript_path.read_bytes() + speakers_path.read_bytes() + json.dumps(settings, sort_keys=True).encode()).hexdigest()[:12]
    stage = "alignment" if (run / "manifest.json").is_file() else "refined"
    output = writable_path(output_directory or run / stage / ("refined-" + key))
    output.mkdir(parents=True, exist_ok=True)
    pending = [item for item in transcript["segments"] if item.get("speaker_id") is None]
    selected = pending if limit is None else pending[:limit]
    began = time.monotonic()
    write_json(output / "status.json", {"stage": "refinement", "state": "running", "selected_segments": len(selected)})
    temporary_root = writable_path(".tmp")
    temporary_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="alignment-", dir=temporary_root) as temporary:
            samples = Path(temporary) / "samples.npy"
            segment_file, result_file = Path(temporary) / "segments.json", Path(temporary) / "result.json"
            np.save(samples, decode_audio(audio), allow_pickle=False)
            segment_file.write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
            env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "DEEPSEEK_API_KEY"):
                env.pop(name, None)
            with (output / "engine.log").open("w", encoding="utf-8") as log:
                result = subprocess.run([sys.executable, "-B", "-m", "sonafolio._alignment_worker", str(model), str(samples), str(segment_file), str(result_file), str(output / "progress.json"), device], env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
            if result.returncode:
                raise RuntimeError(f"Chinese alignment worker failed; see {output / 'engine.log'}")
            raw = json.loads(result_file.read_text(encoding="utf-8"))
        refined = []
        for segment in transcript["segments"]:
            if segment["id"] in raw:
                refined.extend(refine_segment(segment, raw[segment["id"]], speakers["turns"]))
            else:
                refined.append({**segment, "source_segment_id": segment.get("source_segment_id", segment["id"]), "alignment_precision": "segment"})
        unresolved_sources = {item["source_segment_id"] for item in refined if item.get("speaker_id") is None}
        artifact = {**transcript, "stage": "speaker_refinement", "state": "completed_with_unresolved_segments" if unresolved_sources else "completed",
                    "alignment": {"method": "ctc_character_with_segment_fallback", "precision": "mixed_estimates", "settings": settings,
                                  "original_unresolved_sources": len(pending), "processed_sources": len(selected), "unresolved_sources": len(unresolved_sources)},
                    "segments": refined, "elapsed_seconds": round(time.monotonic() - began, 3)}
        write_json(output / "transcript.json", artifact)
        write_json(output / "characters.json", raw)
        markdown = "# 说话人转写草稿（含字符级对齐估计）\n\n字符时间来自 CTC 模型估计，原始 ASR 文本保留；未可靠匹配的内容仍标记待确认，角色尚未推断。\n\n"
        for item in refined:
            label = item.get("speaker_id") or "待确认"
            markdown += f"[{timestamp(item['start'])} → {timestamp(item['end'])}] **{label}**\n\n{item['text']}\n\n"
        (output / "transcript.md").write_text(markdown, encoding="utf-8")
        write_json(output / "status.json", {"stage": "refinement", "state": "completed", "unresolved_sources": len(unresolved_sources)})
        return {"output_directory": str(output), "segments": len(refined), "unresolved_sources": len(unresolved_sources), "processed_sources": len(selected), "elapsed_seconds": artifact["elapsed_seconds"]}
    except Exception as error:
        write_json(output / "status.json", {"stage": "refinement", "state": "failed", "error_type": type(error).__name__})
        raise
