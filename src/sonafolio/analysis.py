"""Whole-recording classification and evidence-backed Skill summaries."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid
from dataclasses import asdict

from jsonschema import Draft202012Validator

from .llm import LLMOutputError, load_settings, request_json
from .paths import workspace_root, writable_path, write_json
from .skills import SkillRegistry, render_summary
from .transcribe import timestamp

MERGE_VERSION = 3


def source_records(segments: list[dict]) -> list[dict]:
    records = {}
    for segment in segments:
        identifier = segment.get("source_segment_id", segment["id"])
        if not re.fullmatch(r"[A-Za-z0-9._-]+", identifier):
            raise ValueError("Unsupported source segment ID")
        record = records.setdefault(identifier, {"id": identifier, "start": segment["start"], "end": segment["end"], "parts": []})
        record["start"] = min(record["start"], segment["start"])
        record["end"] = max(record["end"], segment["end"])
        label = segment.get("speaker_id") or "待确认"
        if record["parts"] and record["parts"][-1]["speaker"] == label:
            record["parts"][-1]["text"] += segment["text"]
        else:
            record["parts"].append({"speaker": label, "text": segment["text"]})
    return list(records.values())


def record_line(record: dict) -> str:
    text = " | ".join(f"{item['speaker']}: {item['text']}" for item in record["parts"])
    return f"[{record['id']} {record['start']:.2f}-{record['end']:.2f}s] {text}"


def make_chunks(records: list[dict], max_chars: int = 14000) -> list[list[dict]]:
    if max_chars < 512:
        raise ValueError("Chunk limit is too small")
    chunks, current, length = [], [], 0
    for record in records:
        size = len(record_line(record))
        if size > max_chars:
            raise ValueError("A source segment exceeds the chunk limit; increase chunk_chars rather than truncating text")
        if current and length + size + 1 > max_chars:
            chunks.append(current)
            current, length = [], 0
        current.append(record)
        length += size + 1
    if current:
        chunks.append(current)
    return chunks


def validate_references(value, allowed_ids: set[str], speaker_ids: set[str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("participants"), list):
            participant_ids = [person["speaker_id"] for person in value["participants"] if "speaker_id" in person]
            if len(participant_ids) != len(set(participant_ids)):
                raise ValueError("A participant cannot have duplicate role entries")
        if "segment_ids" in value:
            if not set(value["segment_ids"]).issubset(allowed_ids):
                raise ValueError("LLM returned unknown source segment IDs")
        if "speaker_id" in value and value["speaker_id"] not in speaker_ids:
            raise ValueError("LLM invented a speaker ID")
        if value.get("role") not in (None, "unknown") and "speaker_id" in value and not value.get("segment_ids"):
            raise ValueError("An inferred participant role requires source evidence")
        for child in value.values():
            validate_references(child, allowed_ids, speaker_ids)
    elif isinstance(value, list):
        for child in value:
            validate_references(child, allowed_ids, speaker_ids)


def choose_category(results: list[dict], enabled_ids: set[str]) -> str:
    if not results:
        return "unknown"
    counts = Counter(item["category"] for item in results)
    winner, count = counts.most_common(1)[0]
    if count / len(results) >= 0.6:
        return winner
    # Different interview subtypes may share a configured parent category.
    parents = sorted((value for value in enabled_ids if value != "unknown"), key=lambda value: -value.count("."))
    for parent in parents:
        support = sum(count for label, count in counts.items() if label == parent or label.startswith(parent + "."))
        if support / len(results) >= 0.6:
            return parent
    return "unknown"


def cached_call(path: Path, instructions: str, content: str, schema: dict, allowed_ids: set[str], speaker_ids: set[str], settings, call, force: bool) -> dict:
    identity = hashlib.sha256(json.dumps({"instructions": instructions, "content": content,
        "schema": schema, "settings": settings.model_dump()}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    checksum = writable_path(path.with_suffix(".sha256"))
    expected = identity + ":" + hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    if path.is_file() and not force and checksum.is_file() and checksum.read_text(encoding="utf-8").strip() == expected:
        artifact = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(artifact["result"])
        validate_references(artifact["result"], allowed_ids, speaker_ids)
        return artifact
    for attempt in range(2):
        try:
            result, metadata = call(instructions, content, schema, settings=settings)
        except LLMOutputError as error:
            write_json(path.with_suffix(".failed.json"), {"state": "invalid_model_output", "attempts": error.attempts})
            raise
        if hasattr(result, "model_dump"):
            result = result.model_dump()
        Draft202012Validator(schema).validate(result)
        try:
            validate_references(result, allowed_ids, speaker_ids)
        except ValueError:
            if attempt == 0:
                instructions += "\n上次返回的来源编号或角色依据不合法。只能引用本次输入中明确存在的编号；无法确认则留空或写未知，不得编造编号。"
                continue
            raise
        artifact = {"result": result, "metadata": metadata}
        write_json(path, artifact)
        checksum.write_text(identity + ":" + hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="utf-8")
        return artifact
    raise RuntimeError("No evidence-validated LLM result")


def analyze_recording(transcript_path: Path, *, skill_id: str | None = None, skills_dir: Path | None = None, prompt_path: Path | None = None, force: bool = False, dry_run: bool = False, chunk_chars: int = 14000, merge_max_tokens: int = 16384, call=request_json, analysis_directory: Path | None = None) -> dict:
    if not 1 <= merge_max_tokens <= 32768:
        raise ValueError("Final merge token limit must be between 1 and 32768")
    transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    if transcript.get("diarization", {}).get("state") != "completed":
        raise ValueError("Speaker diarization must complete before scene analysis")
    records = source_records(transcript["segments"])
    if not records:
        raise ValueError("Transcript contains no text")
    chunks = make_chunks(records, chunk_chars)
    speaker_ids = {part["speaker"] for record in records for part in record["parts"] if part["speaker"] != "待确认"}
    registry = SkillRegistry(skills_dir or (workspace_root() / "skills"))
    routes = registry.routes()
    enabled = {item["id"] for item in routes}
    if skill_id is not None and skill_id not in enabled:
        raise ValueError("Requested skill is not an enabled scene")
    settings = load_settings()
    override = prompt_path.read_text(encoding="utf-8") if prompt_path else None
    digest = hashlib.sha256(transcript_path.read_bytes() + json.dumps({
        "skills": {name: value.digest for name, value in registry.loaded.items()},
        "settings": settings.model_dump(), "skill_override": skill_id,
        "prompt_override": override, "chunk_chars": chunk_chars, "pipeline_version": 1,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    from .tasks import task_for_artifact, index_analysis
    run = task_for_artifact(transcript_path)
    output_key = f"{digest}-m{merge_max_tokens}-v{MERGE_VERSION}"
    if force:
        output_key += "-" + uuid.uuid4().hex[:12]
    version_directory = writable_path(analysis_directory or run / "analysis" / output_key)
    intermediate = writable_path(version_directory / "checkpoints")
    # Merge budget changes reuse validated extraction without sharing mutable files.
    if not intermediate.exists() and not force:
        for marker in sorted((run / "analysis").glob("*/checkpoints/request-identity.json")):
            try:
                marker_value = json.loads(writable_path(marker).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(marker_value, dict) or marker_value.get("digest") != digest:
                continue
            previous = marker.parent
            if previous == intermediate:
                continue
            for checkpoint in previous.glob("*.json"):
                if checkpoint.name.startswith(("route-", "notes-")) and not checkpoint.name.endswith(".failed.json"):
                    target = writable_path(intermediate / checkpoint.name)
                    checksum = writable_path(checkpoint.with_suffix(".sha256"))
                    if target.exists() or not checksum.is_file():
                        continue
                    if not checksum.read_text(encoding="utf-8").strip().endswith(":" + hashlib.sha256(writable_path(checkpoint).read_bytes()).hexdigest()):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(writable_path(checkpoint), target)
                    shutil.copy2(checksum, writable_path(target.with_suffix(".sha256")))
    intermediate.mkdir(parents=True, exist_ok=True)
    write_json(intermediate / "request-identity.json", {"digest": digest})
    write_json(version_directory / "catalog.json", {
        "schema_version": 1, "skills": {name: asdict(value) for name, value in registry.loaded.items()},
        "routes": routes, "prompt_override": override,
    })
    if dry_run:
        preview = "# 待发送给 DeepSeek 的文本预览\n\n此文件只在本地生成；本次没有发送录音文本。原始 M4A 不在请求中。\n\n"
        for index, chunk in enumerate(chunks):
            preview += f"## 分块 {index + 1}\n\n" + "\n\n".join(map(record_line, chunk)) + "\n\n"
        (intermediate / "outbound-text.md").write_text(preview, encoding="utf-8")
        plan = {"state": "preview_only", "provider": "deepseek", "endpoint": "https://api.deepseek.com/chat/completions",
                "model": settings.model, "source_transcript": str(transcript_path), "source_sha256": transcript["source_sha256"],
                "chunks": len(chunks), "source_segments": len(records), "logical_steps": len(chunks) * (1 if skill_id else 2) + 1,
                "extraction_max_tokens": settings.max_tokens, "merge_max_tokens": merge_max_tokens,
                "sent_fields": ["transcript text", "segment IDs", "timestamps", "speaker IDs and uncertainty markers", "Skill instructions and schemas"],
                "audio_uploaded": False, "routing_override": skill_id, "available_scenes": routes,
                "preview": str(intermediate / "outbound-text.md"), "api_requests_made": 0}
        write_json(intermediate / "request-plan.json", plan)
        return {"state": "preview_only", "plan": str(intermediate / "request-plan.json"), "preview": str(intermediate / "outbound-text.md"), "chunks": len(chunks), "api_requests_made": 0}
    all_ids = {record["id"] for record in records}
    status = intermediate / "status.json"
    write_json(status, {"state": "running", "stage": "classification", "chunks": len(chunks)})
    artifacts = []
    try:
        classifications = []
        if skill_id is None:
            schema = {"type": "object", "additionalProperties": False, "properties": {
                "category": {"type": "string", "enum": sorted(enabled)},
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                "reason": {"type": "string"},
                "segment_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 12},
            }, "required": ["category", "tags", "reason", "segment_ids"]}
            instruction = "对本段录音内容分类。只从下面配置的类目中选择，能确定子类目时选择子类目；不能确定则 unknown。给出片段编号依据和少量标签。说话人身份、声音人数不决定类目。\n" + json.dumps(routes, ensure_ascii=False)
            for index, chunk in enumerate(chunks):
                artifact = cached_call(intermediate / f"route-{index:03d}.json", instruction, "\n".join(map(record_line, chunk)), schema,
                                       {item["id"] for item in chunk}, speaker_ids, settings.model_copy(update={"max_tokens": min(settings.max_tokens, 1200)}), call, force)
                classifications.append(artifact["result"])
                artifacts.append(artifact)
                write_json(status, {"state": "running", "stage": "classification", "processed": index + 1, "total": len(chunks)})
            skill_id = choose_category(classifications, enabled)
        classification = {"category": skill_id, "tags": sorted({tag for item in classifications for tag in item["tags"]}),
                          "method": "configured_override" if not classifications else "all_chunks_hierarchical_vote", "chunks": classifications}
        write_json(intermediate / "classification.json", classification)
        skill = registry.get(skill_id)
        write_json(version_directory / "scene.json", {
            "schema_version": 1, "selected": asdict(skill), "routes": routes,
            "skill_digests": {name: value.digest for name, value in registry.loaded.items()},
            "prompt_override": override,
        })
        notes = []
        write_json(status, {"state": "running", "stage": "extraction", "processed": 0, "total": len(chunks)})
        instructions = (override if override is not None else skill.prompt)
        instructions += "\n本次只提取一个分块。所有字段符合 Schema；每条事实、问答、分析和行动都用 segment_ids 指向本次输入中真实存在的片段。speaker_id 只能用 " + json.dumps(sorted(speaker_ids)) + "。待确认文字不可推断为某个说话人的发言。participants 可为空；未知角色用 unknown。不要补充录音外的事实。"
        for index, chunk in enumerate(chunks):
            artifact = cached_call(intermediate / f"notes-{index:03d}.json", instructions, "\n".join(map(record_line, chunk)), skill.schema,
                                   {item["id"] for item in chunk}, speaker_ids, settings, call, force)
            notes.append(artifact["result"])
            artifacts.append(artifact)
            write_json(status, {"state": "running", "stage": "extraction", "processed": index + 1, "total": len(chunks)})
        final_instruction = (override if override is not None else skill.prompt)
        final_instruction += "\n你收到的是完整录音各分块的结构化提取。合并重复信息，统一参与者角色，按时间和主题整理。保留关键问答、事实、建议、行动和不确定性，不添加新事实或新的片段编号。区分面试官讲解与候选人实际回答；无法确认的角色保留 unknown。输出简体中文。"
        final_instruction += "\n这是总结，不是逐条重排所有笔记。关键问答控制在12-15条以内，facts、technical_topics、analysis、actions各优先保留最重要的8-12条，合并重复主题；每项只引用最相关的少量片段。不要重复在多个栏目描述同一事实。单块的角色标签可能出错，跨块角色票数只作线索，应结合自我介绍、经历讲述和原文样例判断；不能因为候选人反问就互换角色。"
        final_instruction += "\n特别注意：ASR 误识、断句或专有名词转写不清，是记录质量问题，不等于候选人表达或能力问题。不得据此给出负面表现评价。术语拼写、识别数字等疑问归入 uncertainties；相关行动只能建议核对录音与转写，不能自动归为面试准备缺陷。分块笔记未覆盖某个细节，不等于原录音没说；没有明确依据的缺失断言应写为待确认，而不是事实性扣分。不要把候选人的既往或当前雇主当成招聘方；招聘公司或目标岗位未明确时，不在标题中补造。"
        observations = Counter((person['speaker_id'], person['role']) for note in notes for person in note.get('participants', []))
        samples = []
        for speaker in sorted(speaker_ids):
            matching = [record for record in records if {part['speaker'] for part in record['parts'] if part['speaker'] != '待确认'} == {speaker} and sum(len(part['text']) for part in record['parts']) >= 20]
            for index in sorted({0, len(matching) // 2, len(matching) - 1}):
                if matching:
                    samples.append(record_line(matching[index]))
        merge_content = {"chunk_notes": notes, "role_observations": [{"speaker_id": speaker, "role": role, "chunks": count} for (speaker, role), count in observations.items()], "original_role_samples": samples}
        write_json(status, {"state": "running", "stage": "merge", "chunks": len(chunks)})
        merge_settings = settings.model_copy(update={"max_tokens": merge_max_tokens})
        merged = cached_call(intermediate / f"merged-m{merge_max_tokens}-v{MERGE_VERSION}.json", final_instruction, json.dumps(merge_content, ensure_ascii=False), skill.schema, all_ids, speaker_ids, merge_settings, call, force)
        artifacts.append(merged)
        output = version_directory
        output.mkdir(parents=True, exist_ok=True)
        metadata = {"skill_id": skill.id, "skill_version": skill.version, "skill_digest": skill.digest,
                    "source_sha256": transcript["source_sha256"], "input_transcript": transcript_path.resolve().relative_to(run).as_posix(),
                    "task_id": run.name,
                    "alignment": transcript.get("alignment", {}), "settings": settings.model_dump(),
                    "merge_settings": merge_settings.model_dump(), "merge_version": MERGE_VERSION,
                    "chunks": len(chunks), "llm_calls": [item["metadata"] for item in artifacts]}
        write_json(output / "summary.json", {"metadata": metadata, "classification": classification, "result": merged["result"]})
        (output / "summary.md").write_text(render_summary(skill, merged["result"], metadata), encoding="utf-8")
        sources = "# 转写与说话人来源\n\nASR 文字和声音归属含模型误差，待确认内容未强行归属。\n\n"
        for record in records:
            sources += f"<a id=\"{record['id']}\"></a>\n\n## {record['id']} · {timestamp(record['start'])} → {timestamp(record['end'])}\n\n"
            sources += "\n\n".join(f"**{part['speaker']}**：{part['text']}" for part in record["parts"]) + "\n\n"
        (output / "sources.md").write_text(sources, encoding="utf-8")
        index_analysis(run, output)
        write_json(status, {"state": "completed", "stage": "analysis", "category": skill.id, "output_directory": str(output)})
        return {"output_directory": str(output), "category": skill.id, "chunks": len(chunks), "source_segments": len(records)}
    except Exception as error:
        write_json(status, {"state": "failed", "error_type": type(error).__name__})
        raise
