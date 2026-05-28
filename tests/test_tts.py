"""Mock-first tests for the v5 Phase 2a TTS output adapter. No audio plays, no
network — subprocess (`say`/`afplay`) and the ElevenLabs HTTP are mocked."""
import subprocess
import unittest
from unittest import mock

from anvil import tts


class TestSpeakDispatch(unittest.TestCase):
    def test_empty_text_is_noop_success(self):
        r = tts.speak("   ", backend="say")
        self.assertTrue(r["ok"])
        self.assertIn("skipped", r)

    def test_unknown_backend_structured_error(self):
        r = tts.speak("hi", backend="robot")
        self.assertFalse(r["ok"])
        self.assertIn("unknown backend", r["error"])


class TestSayBackend(unittest.TestCase):
    def test_say_ok(self):
        with mock.patch.object(tts.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            r = tts.speak("hello build done", backend="say")
        self.assertTrue(r["ok"])
        self.assertEqual(r["backend"], "say")
        self.assertEqual(run.call_args.args[0][0], "say")  # invoked `say`

    def test_say_nonzero_structured_error(self):
        with mock.patch.object(tts.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="boom")):
            r = tts.speak("x", backend="say")
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["error"])

    def test_say_not_found_never_raises(self):
        with mock.patch.object(tts.subprocess, "run", side_effect=FileNotFoundError("no say")):
            r = tts.speak("x", backend="say")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_say_timeout_never_raises(self):
        with mock.patch.object(tts.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("say", 60)):
            r = tts.speak("x", backend="say")
        self.assertFalse(r["ok"])
        self.assertIn("timeout", r["error"])


class TestElevenLabsBackend(unittest.TestCase):
    def test_no_key_structured_error_not_raise(self):
        # Q-A3: no ElevenLabs key → structured "no key" error, never raises.
        with mock.patch.dict("os.environ", {}, clear=True):
            r = tts.speak("hi", backend="elevenlabs")
        self.assertFalse(r["ok"])
        self.assertEqual(r["backend"], "elevenlabs")
        self.assertIn("ELEVENLABS_API_KEY", r["error"])

    def test_with_key_fetches_and_plays(self):
        fake = mock.MagicMock()
        fake.read.return_value = b"ID3audiobytes"
        cm = mock.MagicMock(); cm.__enter__.return_value = fake
        with mock.patch.object(tts, "urlopen", return_value=cm), \
             mock.patch.object(tts.subprocess, "run") as run:
            r = tts.speak("hi", backend="elevenlabs", api_key="k", voice_id="v")
        self.assertTrue(r["ok"])
        self.assertEqual(r["bytes"], len(b"ID3audiobytes"))
        self.assertEqual(run.call_args.args[0][0], "afplay")  # played the audio

    def test_http_error_never_raises(self):
        from urllib.error import HTTPError
        with mock.patch.object(tts, "urlopen", side_effect=HTTPError("u", 401, "unauth", {}, None)):
            r = tts.speak("hi", backend="elevenlabs", api_key="k")
        self.assertFalse(r["ok"])
        self.assertIn("401", r["error"])


if __name__ == "__main__":
    unittest.main()
