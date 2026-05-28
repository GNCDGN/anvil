"""Mock-first tests for the v5 Phase 2a voice input adapter. whisper-cli +
ffmpeg subprocesses are mocked — no binary install, no audio needed."""
import subprocess
import unittest
from unittest import mock

from anvil import voice_input


class TestTranscribe(unittest.TestCase):
    def test_ok_wav_skips_convert(self):
        # a .wav input skips ffmpeg; whisper-cli prints the transcript to stdout
        with mock.patch.object(voice_input.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="hello anvil\n", stderr="")) as run:
            r = voice_input.transcribe("/tmp/clip.wav")
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "hello anvil")
        # only whisper-cli ran (no ffmpeg convert for .wav)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "whisper-cli")

    def test_oga_converts_then_transcribes(self):
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd[0])
            if cmd[0] == "ffmpeg":
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="transcribed text\n", stderr="")
        with mock.patch.object(voice_input.subprocess, "run", side_effect=fake_run):
            r = voice_input.transcribe("/tmp/voice.oga")
        self.assertTrue(r["ok"])
        self.assertEqual(r["text"], "transcribed text")
        self.assertEqual(calls, ["ffmpeg", "whisper-cli"])  # convert then transcribe

    def test_binary_missing_structured_error(self):
        with mock.patch.object(voice_input.subprocess, "run", side_effect=FileNotFoundError("no whisper-cli")):
            r = voice_input.transcribe("/tmp/clip.wav")
        self.assertFalse(r["ok"])
        self.assertIn("brew install whisper-cpp", r["error"])

    def test_nonzero_structured_error(self):
        with mock.patch.object(voice_input.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="model load failed")):
            r = voice_input.transcribe("/tmp/clip.wav")
        self.assertFalse(r["ok"])
        self.assertIn("model load failed", r["error"])

    def test_empty_transcript_structured_error(self):
        with mock.patch.object(voice_input.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="   \n", stderr="")):
            r = voice_input.transcribe("/tmp/clip.wav")
        self.assertFalse(r["ok"])
        self.assertIn("empty transcript", r["error"])

    def test_timeout_never_raises(self):
        with mock.patch.object(voice_input.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("whisper-cli", 120)):
            r = voice_input.transcribe("/tmp/clip.wav")
        self.assertFalse(r["ok"])
        self.assertIn("timeout", r["error"])


if __name__ == "__main__":
    unittest.main()
