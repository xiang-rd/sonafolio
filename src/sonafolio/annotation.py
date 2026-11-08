"""Conservatively attach speaker IDs without guessing within mixed utterances."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path

from .paths import writable_path, write_json
from .transcribe import timestamp


def interval_union_duration(intervals: list[tuple[float, float]]) -> float:
    total = 0.0
    current = None
    for start, end in sorted(intervals):
        if current is None:
            current = (start, end)
        elif start <= current[1]:
            current = (current[0], max(current[1], end))
        else:
            total += current[1] - current[0]
            current = (start, end)
    return total + (current[1] - current[0] if current else 0.0)


def annotate_segments(segments: list[dict], turns: list[dict]) -> list[dict]:
    annotated = []
    for segment in segments:
        start, end = segment["start"], segment["end"]
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("Transcript contains an invalid or empty time interval")
        intersections = defaultdict(list)
        for turn in turns:
            lower, upper = max(start, turn["start"]), min(end, turn["end"])
            if upper - lower > 0.001:
                intersections[turn["speaker_id"]].append((lower, upper))
        overlaps = {speaker: interval_union_duration(items) for speaker, items in intersections.items()}
        # Never choose the majority speaker when an ASR segment touches multiple
        # speakers: its text cannot safely be split without finer timestamps.
        speaker = next(iter(overlaps)) if len(overlaps) == 1 else None
        if speaker and overlaps[speaker] / (end - start) < 0.8:
            speaker = None
        annotated.append({
            **segment, "speaker_id": speaker, "role": "unknown",
            "speaker_candidates": sorted(overlaps),
            "speaker_overlap_seconds": {key: round(value, 3) for key, value in overlaps.items()},
            "speaker_assignment": "single_speaker_segment" if speaker else "needs_finer_alignment",
        })
    return annotated


def annotate_recording(transcript_path: Path, speakers_path: Path, *, output_directory: Path | None = None) -> dict:
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    speakers = json.loads(speakers_path.read_text(encoding="utf-8"))
    if transcript.get("source_sha256") != speakers.get("source_sha256"):
        raise ValueError("Transcript and speaker intervals belong to different audio inputs")
    if not transcript.get("source_sha256"):
        raise ValueError("Input fingerprint is missing")
    if speakers.get("state") != "completed":
        raise ValueError("Speaker diarization has not completed")
    if (transcript["range"]["start"] < speakers["range"]["start"] - 0.05 or
            transcript["range"]["end"] > speakers["range"]["end"] + 0.05):
        raise ValueError("Speaker intervals do not cover the selected transcript range")
    # Derive the run folder from our existing transcript artifact. Output path
    # validation still rejects artifacts or symlinks that escape the workspace.
    from .tasks import task_for_artifact
    directory = task_for_artifact(transcript_path)
    digest = hashlib.sha256(transcript_path.read_bytes() + speakers_path.read_bytes()).hexdigest()[:12]
    stage = "alignment" if (directory / "manifest.json").is_file() else "annotated"
    output = writable_path(output_directory or directory / stage / ("annotated-" + digest))
    output.mkdir(parents=True, exist_ok=True)
    segments = annotate_segments(transcript["segments"], speakers["turns"])
    unresolved = sum(item["speaker_id"] is None for item in segments)
    artifact = {
        **transcript, "stage": "speaker_annotation",
        "state": "completed_with_unresolved_segments" if unresolved else "completed",
        "diarization": {"state": "completed", "speaker_ids": speakers["speaker_ids"]},
        "alignment": {"method": "conservative_segment_overlap", "precision": "segment",
                      "unresolved_segments": unresolved, "total_segments": len(segments)},
        "roles": "not_inferred", "segments": segments,
    }
    write_json(output / "transcript.json", artifact)
    markdown = (
        "# 带说话人编号的转写草稿\n\n"
        "编号来自整段录音的自动聚类，尚未人工核对；角色尚未推断。\n\n"
        "这里只进行片段级保守归属：跨人发言或语音覆盖不足的片段标记待对齐，"
        "不能视为逐词精确对齐。\n\n"
        f"共 {len(segments)} 个片段，其中 {unresolved} 个待进一步对齐。\n\n"
    )
    for item in segments:
        label = item["speaker_id"] or "待对齐（" + ", ".join(item["speaker_candidates"]) + "）"
        markdown += f"[{timestamp(item['start'])} → {timestamp(item['end'])}] **{label}**\n\n{item['text']}\n\n"
    writable_path(output / "transcript.md").write_text(markdown, encoding="utf-8")
    return {"output_directory": str(output), "segments": len(segments), "unresolved_segments": unresolved,
            "precision": "segment", "roles": "not_inferred"}
