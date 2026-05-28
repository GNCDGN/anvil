"""Mock-first tests for the v5 Phase 2a Telegram voice-message ingestion
(_Upd.voice_file_id + _resolve_voice). No network, no audio — the download +
transcription seams are mocked."""
import unittest
from unittest import mock

from anvil.telegram import TelegramClient, _Upd


def _client():
    return TelegramClient("tok", "123")


class TestResolveVoice(unittest.TestCase):
    def _voice_upd(self):
        return _Upd(update_id=1, chat_id=123, message_id=5, text="", date=0,
                    voice_file_id="VOICE_FILE_ID")

    def test_voice_becomes_text(self):
        c = _client()
        with mock.patch.object(c, "_download_voice", return_value="/tmp/v.oga"), \
             mock.patch("anvil.voice_input.transcribe",
                        return_value={"ok": True, "text": "build the thing"}):
            out = c._resolve_voice([self._voice_upd()])
        self.assertEqual(out[0].text, "build the thing")  # routes as typed text

    def test_text_message_untouched(self):
        c = _client()
        typed = _Upd(update_id=2, chat_id=123, message_id=6, text="hi", date=0)
        with mock.patch.object(c, "_download_voice") as dl:
            out = c._resolve_voice([typed])
        self.assertEqual(out[0].text, "hi")
        dl.assert_not_called()  # no voice_file_id → no download

    def test_transcription_failure_leaves_text_empty_no_raise(self):
        c = _client()
        with mock.patch.object(c, "_download_voice", return_value="/tmp/v.oga"), \
             mock.patch("anvil.voice_input.transcribe",
                        return_value={"ok": False, "error": "whisper-cli not found"}):
            out = c._resolve_voice([self._voice_upd()])
        self.assertEqual(out[0].text, "")  # failed transcription → empty, ignored downstream

    def test_download_exception_never_raises(self):
        c = _client()
        with mock.patch.object(c, "_download_voice", side_effect=RuntimeError("net down")):
            out = c._resolve_voice([self._voice_upd()])  # must not raise
        self.assertEqual(out[0].text, "")


if __name__ == "__main__":
    unittest.main()
