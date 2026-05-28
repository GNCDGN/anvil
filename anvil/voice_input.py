"""ANVIL voice input adapter (v5 Phase 2a, item A).

Transcribes an audio file to text via **whisper.cpp** (the local ``whisper-cli``
binary). Backend decision (Step 0): Claude has no native audio input (Q-A1) and
no OpenAI key is provisioned for the Whisper-API fallback (Q-A3), so whisper.cpp
local is the key-free transcription path. A **subprocess shim** (Q-A2 lean — no
``whisper-cpp-python`` binding). Named ``voice_input.py``, NOT ``voice.py`` — the
latter is the writing-persona spec (Q-A4 collision).

Pipeline: Telegram delivers OGG/Opus (.oga); whisper.cpp wants 16 kHz mono WAV,
so transcribe converts via ``ffmpeg`` first (skipped if already .wav). The
Telegram-file *download* lives in ``telegram.py`` (where the PTB ``Bot`` async
context is); this module owns only the local-file → text step.

**Never-raises** (Contract 1): a missing binary / a failed run / an empty
transcript returns a structured ``{"ok": False, "error": …}``. The transcribed
text routes as a normal operator message (the Confirmation contract — no
auto-fire). **Setup task** (Q-A2, not committed code): ``brew install
whisper-cpp`` + ``brew install ffmpeg`` + a model download (``ggml-base.en.bin``
~140 MB) to ``WHISPER_MODEL``. Until installed, ``transcribe`` returns a
structured "binary not found" error (never raises).
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("anvil.voice_input")

_DEFAULT_MODEL = os.path.expanduser("~/.cache/whisper/ggml-base.en.bin")


def _binary() -> str:
    return os.environ.get("WHISPER_CLI") or "whisper-cli"


def _model() -> str:
    return os.environ.get("WHISPER_MODEL") or _DEFAULT_MODEL


def _to_wav16k(audio_path: str, *, timeout: int = 60) -> str | None:
    """Convert any audio to 16 kHz mono WAV via ffmpeg (whisper.cpp's required
    input). Returns the wav path, the input unchanged if already .wav, or None
    on failure. Never-raises."""
    if audio_path.lower().endswith(".wav"):
        return audio_path
    out = audio_path + ".16k.wav"
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", out],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        log.warning("ffmpeg not found; passing audio to whisper unconverted")
        return audio_path  # let whisper try; it may handle the format itself
    except Exception as e:  # never-raises
        log.warning("ffmpeg convert failed: %s", e)
        return None
    return out if r.returncode == 0 else None


def transcribe(audio_path: str, *, model: str | None = None, binary: str | None = None,
               timeout: int = 120) -> dict:
    """Transcribe `audio_path` to text via whisper.cpp's ``whisper-cli``.
    Never-raises; returns ``{"ok": True, "text": str}`` or
    ``{"ok": False, "error": str}``."""
    binary = binary or _binary()
    model = model or _model()
    wav = _to_wav16k(audio_path)
    if wav is None:
        return {"ok": False, "error": f"audio conversion failed for {audio_path}"}
    try:
        # `-nt` (no timestamps) prints the bare transcript to stdout.
        r = subprocess.run([binary, "-m", model, "-f", wav, "-nt"],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return {"ok": False, "error": f"whisper-cli not found ({e}); run: brew install whisper-cpp"}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": f"whisper timeout: {e}"}
    except Exception as e:  # never-raises
        return {"ok": False, "error": f"transcribe: {type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "").strip()[:300] or "whisper-cli nonzero exit"}
    text = (r.stdout or "").strip()
    if not text:
        return {"ok": False, "error": "empty transcript"}
    return {"ok": True, "text": text}
