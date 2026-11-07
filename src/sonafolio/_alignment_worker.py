"""Offline Chinese character alignment, confined to ambiguous source segments."""

import json
from pathlib import Path
import sys
import unicodedata

import numpy as np

from .ctc import align_tokens


def main():
    import torch
    from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
    model_dir, samples_path, segments_path, result_path, progress_path, device = sys.argv[1:]
    torch.set_num_threads(4)
    processor = Wav2Vec2Processor.from_pretrained(model_dir, local_files_only=True)
    model = Wav2Vec2ForCTC.from_pretrained(model_dir, local_files_only=True, attn_implementation="eager").eval().to(device)
    vocab = processor.tokenizer.get_vocab()
    waveform = np.load(samples_path, mmap_mode="r", allow_pickle=False)
    segments = json.loads(Path(segments_path).read_text(encoding="utf-8"))
    results = {}
    for index, segment in enumerate(segments):
        text = segment["text"]
        positions, tokens = [], []
        for position, character in enumerate(text):
            normalized = unicodedata.normalize("NFKC", character)
            token = next((vocab[value] for value in (normalized.upper(), normalized.lower(), normalized) if value in vocab), None)
            if token is not None and not character.isspace():
                positions.append(position)
                tokens.append(token)
        start = max(0, segment["start"] - 0.25)
        end = min(len(waveform) / 16000, segment["end"] + 0.25)
        characters = [None] * len(text)
        if tokens and 0.1 <= end - start <= 45:
            audio = np.array(waveform[round(start * 16000):round(end * 16000)], copy=True)
            inputs = processor(audio, sampling_rate=16000, return_tensors="pt")
            with torch.inference_mode():
                logits = model(**{key: value.to(device) for key, value in inputs.items()}).logits[0]
                emissions = logits.log_softmax(dim=-1).cpu().numpy()
            aligned = align_tokens(emissions, tokens, model.config.pad_token_id)
            if aligned:
                frame_seconds = len(audio) / 16000 / len(emissions)
                for position, item in zip(positions, aligned):
                    characters[position] = {
                        "start": round(start + item["first_frame"] * frame_seconds, 4),
                        "end": round(start + item["last_frame"] * frame_seconds, 4),
                        "score": round(item["score"], 4),
                    }
        results[segment["id"]] = characters
        if index % 10 == 0 or index + 1 == len(segments):
            Path(progress_path).write_text(json.dumps({"processed": index + 1, "total": len(segments)}))
    Path(result_path).write_text(json.dumps(results), encoding="utf-8")


if __name__ == "__main__":
    main()
