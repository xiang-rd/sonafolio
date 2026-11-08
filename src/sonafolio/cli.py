"""Command line entry point for the first local audio baseline."""

from __future__ import annotations

import json
import getpass
from pathlib import Path

import typer

from .audio import record_input
from .paths import writable_path

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Sonafolio: local audio processing and speaker-aware records.")


@app.command()
def probe(audio: Path = typer.Argument(..., exists=True, dir_okay=False)):
    """Import a verified managed copy and report media metadata; original is unchanged."""
    metadata, directory = record_input(audio)
    typer.echo(json.dumps({**metadata, "run_directory": str(directory)}, ensure_ascii=False, indent=2))


@app.command()
def transcribe(
    audio: Path = typer.Argument(..., exists=True, dir_okay=False),
    model: Path = typer.Option(..., exists=True, dir_okay=False, help="Explicit local whisper.cpp model file"),
    start: float = typer.Option(0, min=0),
    duration: float | None = typer.Option(None, min=0.01, help="Seconds to process; omit for the rest of the file"),
    threads: int = typer.Option(4, min=1),
    device: str = typer.Option("auto", help="auto (including GPU when available) or cpu"),
    vad_model: Path = typer.Option(Path(".models/whisper-vad/ggml-silero-v6.2.0.bin"), exists=True, dir_okay=False, help="Local speech-activity detector to skip silence"),
):
    """Generate a Chinese ASR baseline. Speakers remain pending until diarization."""
    from .transcribe import transcribe_local
    result = transcribe_local(audio, model=model, start=start, duration=duration, threads=threads, device=device, vad_model=vad_model)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("download-asr")
def download_asr():
    """Explicitly download the public baseline model into the workspace."""
    from .models import download_baseline, download_vad
    typer.echo(json.dumps({"asr": str(download_baseline()), "vad": str(download_vad())}, indent=2))


@app.command("configure-hf")
def configure_hf():
    """Prompt for a token without echoing it; save only in this workspace."""
    token = getpass.getpass("Hugging Face read token (hidden): ").strip()
    if not token.startswith("hf_") or len(token) < 20 or any(character.isspace() for character in token):
        raise ValueError("Expected a Hugging Face hf_ token without whitespace")
    path = writable_path(".runtime/secrets/huggingface-token")
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(token)
    path.chmod(0o600)
    typer.echo("Saved token in the workspace. Model access conditions must be accepted separately.")


@app.command("configure-deepseek")
def configure_deepseek():
    """Save a DeepSeek key locally without echoing it or making API requests."""
    key = getpass.getpass("DeepSeek API key (hidden): ").strip()
    if len(key) < 16 or any(character.isspace() for character in key):
        raise ValueError("Expected a complete API key without whitespace")
    path = writable_path(".runtime/secrets/deepseek-api-key")
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(key)
    path.chmod(0o600)
    typer.echo("Saved DeepSeek API key in the workspace. No API request was made.")


@app.command("check-llm")
def check_llm():
    """Make a tiny synthetic JSON request to verify the configured LLM; no recording is sent."""
    from typing import Literal
    from pydantic import BaseModel, ConfigDict
    from .llm import request_json

    class Check(BaseModel):
        model_config = ConfigDict(extra="forbid")
        ok: Literal[True]

    _, metadata = request_json('Return exactly {"ok": true}.', "Connection test; no recording content.", Check)
    typer.echo(json.dumps({"connection": "ok", **metadata}, ensure_ascii=False, indent=2))


@app.command("download-diarization")
def download_diarization():
    """Download the gated community-1 model after accepting its conditions."""
    from .diarization import download_diarization as download
    typer.echo(str(download()))


@app.command()
def diarize(
    audio: Path = typer.Argument(..., exists=True, dir_okay=False),
    start: float = typer.Option(0, min=0),
    duration: float | None = typer.Option(None, min=0.01),
    device: str = typer.Option("cpu", help="cpu, mps or cuda; device support must be verified"),
    num_speakers: int | None = typer.Option(None, min=1, help="Omit to estimate any number of speakers"),
):
    """Cluster speakers across the whole selected range; role inference is separate."""
    from .diarization import diarize_local
    result = diarize_local(audio, start=start, duration=duration, device=device, num_speakers=num_speakers)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def annotate(
    transcript: Path = typer.Argument(..., exists=True, dir_okay=False),
    speakers: Path = typer.Argument(..., exists=True, dir_okay=False),
):
    """Conservatively attach speakers; mixed utterances remain explicitly unresolved."""
    from .annotation import annotate_recording
    typer.echo(json.dumps(annotate_recording(transcript, speakers), ensure_ascii=False, indent=2))


@app.command("download-alignment")
def download_alignment():
    """Download and verify the public Chinese CTC model into the workspace."""
    from .refinement import download_alignment as download
    typer.echo(str(download()))


@app.command()
def refine(
    transcript: Path = typer.Argument(..., exists=True, dir_okay=False),
    speakers: Path = typer.Argument(..., exists=True, dir_okay=False),
    device: str = typer.Option("cpu"),
    limit: int | None = typer.Option(None, min=1, help="Limit ambiguous source segments for a smoke test"),
):
    """Estimate Chinese character timings for ambiguous speaker assignments."""
    from .refinement import refine_recording
    typer.echo(json.dumps(refine_recording(transcript, speakers, device=device, limit=limit), ensure_ascii=False, indent=2))


@app.command("list-skills")
def list_skills(skills_dir: Path | None = typer.Option(None, exists=True, file_okay=False)):
    """List configured scene categories; no model requests."""
    from .skills import SkillRegistry
    from .paths import workspace_root
    typer.echo(json.dumps(SkillRegistry(skills_dir or workspace_root() / "skills").routes(), ensure_ascii=False, indent=2))


@app.command()
def analyze(
    transcript: Path = typer.Argument(..., exists=True, dir_okay=False),
    skill: str | None = typer.Option(None, help="Override automatic scene selection"),
    skills_dir: Path | None = typer.Option(None, exists=True, file_okay=False),
    prompt: Path | None = typer.Option(None, exists=True, dir_okay=False),
    force: bool = typer.Option(False, help="Regenerate existing LLM checkpoints"),
    dry_run: bool = typer.Option(False, help="Write a local outbound-text preview without any API calls"),
    merge_max_tokens: int = typer.Option(16384, min=1, max=32768, help="Final summary output limit; extraction checkpoints are reusable"),
):
    """Send transcript text to DeepSeek, classify all chunks, extract and merge a Skill summary."""
    from .analysis import analyze_recording
    result = analyze_recording(transcript, skill_id=skill, skills_dir=skills_dir, prompt_path=prompt, force=force, dry_run=dry_run, merge_max_tokens=merge_max_tokens)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command()
def run(
    audio: Path = typer.Argument(..., exists=True, dir_okay=False),
    model: Path = typer.Option(Path(".models/whisper.cpp/ggml-large-v3-turbo-q5_0.bin")),
    vad_model: Path = typer.Option(Path(".models/whisper-vad/ggml-silero-v6.2.0.bin")),
    start: float = typer.Option(0, min=0),
    duration: float | None = typer.Option(None, min=0.01),
    threads: int = typer.Option(4, min=1),
    asr_device: str = typer.Option("auto"),
    speaker_device: str = typer.Option("cpu"),
    num_speakers: int | None = typer.Option(None, min=1),
    refine: bool = typer.Option(True, "--refine/--no-refine"),
    alignment_device: str = typer.Option("cpu"),
    analysis: str = typer.Option("preview", help="preview: local outbound preview (default); cloud: send text to DeepSeek; none: transcription only"),
    skill: str | None = typer.Option(None),
    skills_dir: Path | None = typer.Option(None, exists=True, file_okay=False),
    prompt: Path | None = typer.Option(None, exists=True, dir_okay=False),
    merge_max_tokens: int = typer.Option(16384, min=1, max=32768),
    force: bool = typer.Option(False, help="New stage attempts; retain all previous results"),
):
    """Import one recording and run/resume ASR, speakers, alignment and analysis."""
    from .pipeline import RunOptions, run_pipeline
    options = RunOptions(model=str(model), vad_model=str(vad_model), start=start, duration=duration, threads=threads,
                         asr_device=asr_device, speaker_device=speaker_device, num_speakers=num_speakers,
                         refine=refine, alignment_device=alignment_device, analysis=analysis, skill=skill,
                         skills_dir=str(skills_dir) if skills_dir else None, prompt=str(prompt) if prompt else None,
                         merge_max_tokens=merge_max_tokens)
    result = run_pipeline(audio, options=options, force=force, on_event=_progress)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


def _progress(event):
    typer.echo(json.dumps(event, ensure_ascii=False), err=True)


@app.command()
def resume(task_id: str, analysis: str | None = typer.Option(None, help="Override none/preview/cloud; omit to keep saved mode")):
    """Resume a task with its saved options; verify results before reusing."""
    from .pipeline import resume_pipeline
    typer.echo(json.dumps(resume_pipeline(task_id, analysis=analysis, on_event=_progress), ensure_ascii=False, indent=2))


@app.command("rename-task")
def rename(task_id: str, title: str):
    """Change the display title; preserve source filename, task ID and references."""
    from .tasks import rename_task
    typer.echo(json.dumps(rename_task(task_id, title), ensure_ascii=False, indent=2))


@app.command("rebuild-index")
def rebuild():
    """Rebuild managed analysis indexes; retain legacy entries."""
    from .tasks import rebuild_index
    typer.echo(json.dumps(rebuild_index(), ensure_ascii=False, indent=2))


@app.command("migrate-run")
def migrate(legacy: Path = typer.Argument(..., exists=True, file_okay=False), apply: bool = typer.Option(False, help="Copy and validate; default only previews the migration")):
    """Copy a legacy run and its results into one managed task; retain originals."""
    from .migration import migrate_legacy
    typer.echo(json.dumps(migrate_legacy(legacy, apply=apply), ensure_ascii=False, indent=2))


def main():
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("Sonafolio: cancelled; completed stages are retained", err=True)
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, EOFError) as error:
        typer.echo(f"Sonafolio: {error}", err=True)
        raise SystemExit(1) from None
