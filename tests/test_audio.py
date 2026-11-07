"""Exercise real decoding, precise ranges, read-only inputs and output boundaries."""

import os
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
import wave

import numpy as np

from sonafolio.audio import decode_audio, fingerprint, probe_audio
from sonafolio.paths import writable_path, write_json
from sonafolio.transcribe import timestamp, transcribe_local
from sonafolio.cli import app
from typer.testing import CliRunner


ROOT = Path(__file__).resolve().parents[1]


class AudioTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.environment = patch.dict(os.environ, {"SONAFOLIO_WORKSPACE": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.input = self.root / "data/input/中文 音频.wav"
        self.input.parent.mkdir(parents=True)
        # A 2-second stereo fixture, with a change halfway through. Both channels
        # differ so the test also exercises real stereo→mono resampling.
        samples = np.zeros((16000, 2), dtype="<i2")
        samples[:8000] = [8000, 4000]
        samples[8000:] = [-8000, -4000]
        with wave.open(str(self.input), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(samples.tobytes())

    def test_probe_and_decode_do_not_change_source(self):
        before = fingerprint(self.input)
        metadata = probe_audio(self.input)
        pcm = decode_audio(self.input)
        self.assertEqual(metadata["channels"], 2)
        self.assertAlmostEqual(metadata["duration_seconds"], 2)
        self.assertEqual(pcm.dtype, np.float32)
        self.assertEqual(len(pcm), 32000)
        self.assertEqual(fingerprint(self.input), before)

    def test_range_matches_full_decode_exactly(self):
        whole = decode_audio(self.input)
        excerpt = decode_audio(self.input, start=0.75, duration=0.5)
        np.testing.assert_array_equal(excerpt, whole[12000:20000])

    def test_range_past_end_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no decodable audio"):
            decode_audio(self.input, start=3)

    def test_nonfinite_and_invalid_ranges_are_rejected(self):
        for options in ({"start": float("nan")}, {"start": -1}, {"duration": 0}, {"duration": float("inf")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                decode_audio(self.input, **options)

    def test_writes_cannot_escape_or_overwrite_inputs(self):
        for path in (self.root.parent / "escaped.json", self.input):
            with self.subTest(path=path), self.assertRaises(ValueError):
                write_json(path, {"should_not_write": True})

    def test_output_symlink_escape_is_rejected(self):
        directory = self.root / "data/runs"
        directory.parent.mkdir(exist_ok=True)
        try:
            directory.symlink_to(self.root.parent, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks are unavailable")
        with self.assertRaises(ValueError):
            writable_path("data/runs/result.json")

    def test_timestamp_rolls_over_hours(self):
        self.assertEqual(timestamp(3661.125), "01:01:01.125")

    def test_native_crash_is_recorded_as_failed_stage(self):
        model = self.root / ".models/test.bin"
        model.parent.mkdir()
        model.write_bytes(b"dummy; worker is mocked")
        with patch("sonafolio.transcribe.subprocess.run", return_value=subprocess.CompletedProcess([], -11)):
            with self.assertRaisesRegex(RuntimeError, "worker exited"):
                transcribe_local(self.input, model=model, duration=1)
        statuses = list((self.root / "data/runs").glob("*/asr/*/status.json"))
        self.assertEqual(len(statuses), 1)
        self.assertEqual(json.loads(statuses[0].read_text())["state"], "failed")
        self.assertEqual(list((self.root / ".tmp").iterdir()), [])

    def test_asr_restores_global_time_and_keeps_speakers_pending(self):
        model = self.root / ".models/test.bin"
        model.parent.mkdir()
        model.write_bytes(b"dummy; worker is mocked")

        def fake_worker(command, **kwargs):
            Path(command[6]).write_text(json.dumps([{"t0": 10, "t1": 80, "text": "测试"}]))
            return subprocess.CompletedProcess(command, 0)

        with patch("sonafolio.transcribe.subprocess.run", side_effect=fake_worker):
            result = transcribe_local(self.input, model=model, start=0.5, duration=1)
        artifact = json.loads((Path(result["output_directory"]) / "transcript.json").read_text())
        self.assertEqual(artifact["segments"][0]["start"], 0.6)
        self.assertEqual(artifact["segments"][0]["end"], 1.3)
        self.assertIsNone(artifact["segments"][0]["speaker_id"])
        self.assertEqual(artifact["diarization"]["state"], "pending")

    def test_token_stays_local_and_is_not_echoed(self):
        token = "hf_test_sentinel_not_a_real_token"
        with patch("sonafolio.cli.getpass.getpass", return_value=token):
            result = CliRunner().invoke(app, ["configure-hf"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(token, result.output)
        path = self.root / ".runtime/secrets/huggingface-token"
        self.assertEqual(path.read_text(), token)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_deepseek_key_is_saved_without_a_network_request_or_echo(self):
        key = "test-deepseek-secret-sentinel"
        with patch("sonafolio.cli.getpass.getpass", return_value=key):
            with patch("httpx.Client.post", side_effect=AssertionError("Unexpected network request")):
                result = CliRunner().invoke(app, ["configure-deepseek"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn(key, result.output)
        path = self.root / ".runtime/secrets/deepseek-api-key"
        self.assertEqual(path.read_text(), key)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
