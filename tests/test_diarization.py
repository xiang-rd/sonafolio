"""Verify arbitrary speaker counts and credential boundaries without model access."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sonafolio.diarization import diarize_local, download_diarization, normalize_speakers, read_token

ROOT = Path(__file__).resolve().parents[1]


class SpeakerTests(unittest.TestCase):
    def test_preserves_three_speakers_and_overlapping_turns(self):
        turns = [
            {"start": 4, "end": 6, "speaker_id": "original-A"},
            {"start": 0, "end": 3, "speaker_id": "original-C"},
            {"start": 2, "end": 4.5, "speaker_id": "original-B"},
        ]
        exclusive = [{"start": 4, "end": 6, "speaker_id": "original-A"}]
        normalized, solo, speakers = normalize_speakers(turns, exclusive)
        self.assertEqual(speakers, ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"])
        self.assertEqual(normalized[0]["end"], 3)
        self.assertEqual(normalized[1]["start"], 2)
        self.assertEqual(solo[0]["speaker_id"], "SPEAKER_02")
        self.assertEqual(turns[0]["speaker_id"], "original-A")

    def test_label_order_does_not_override_first_appearance(self):
        turns = [{"start": 10, "end": 11, "speaker_id": "A"},
                 {"start": 0, "end": 1, "speaker_id": "Z"}]
        result, _, _ = normalize_speakers(turns, [])
        self.assertEqual(result[0]["speaker_id"], "SPEAKER_00")

    def test_missing_credentials_or_model_fail_without_network(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            with patch.dict(os.environ, {"SONAFOLIO_WORKSPACE": temporary, "HF_TOKEN": ""}):
                with self.assertRaisesRegex(RuntimeError, "configure-hf"):
                    read_token()
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    diarize_local(Path("unused.wav"))

    def test_access_error_explains_account_authorization_without_echoing_token(self):
        import httpx
        from huggingface_hub.errors import HfHubHTTPError
        (ROOT / ".tmp").mkdir(exist_ok=True)
        response = httpx.Response(403, headers={"x-error-message": "you are not in the authorized list"}, request=httpx.Request("GET", "https://huggingface.co/test"))
        error = HfHubHTTPError("private-message", response=response)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as temporary:
            with patch.dict(os.environ, {"SONAFOLIO_WORKSPACE": temporary, "HF_TOKEN": "test-secret-never-print"}):
                with patch("huggingface_hub.HfApi.model_info", side_effect=error):
                    with self.assertRaises(RuntimeError) as caught:
                        download_diarization()
        self.assertIn("same account", str(caught.exception))
        self.assertNotIn("test-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
