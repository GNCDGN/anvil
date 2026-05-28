"""ANVIL TTS output adapter (v5 Phase 2a, item B).

Converts ANVIL's outbound messages (step-completion, escalation) to speech.
Two backends behind one never-raises interface:

  - ``say``       — macOS ``say`` via subprocess (the default; always present,
                    free, robotic).
  - ``elevenlabs``— the ElevenLabs API via stdlib ``urllib`` (key-gated; human
                    voice; the audio is played with ``afplay``).

**Per-session opt-in** (the ``copilot.py`` opt-in discipline carried): TTS is
OFF by default; the orchestrator speaks only when the session flag is set
(``config.tts_enabled``). Telegram stays the primary channel — TTS is *additive*
(Phase 2 design F3), never a replacement.

**Never-raises** (connector-pattern Contract 1): a missing key, a failed
subprocess, or an HTTP error returns a structured ``{"ok": False, "error": …}``;
an output side-channel never breaks a build. ElevenLabs with no key returns a
structured "no key" error (it does not raise) — the Phase 2a deferral (Q-A3:
no ElevenLabs key provisioned; the live ElevenLabs probe pends one).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("anvil.tts")

VALID_BACKENDS = ("say", "elevenlabs")
_ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech"
_DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs "Rachel" — overridable via env


def _say(text: str, *, timeout: int = 60) -> dict:
    """macOS ``say`` (subprocess). Never-raises."""
    try:
        r = subprocess.run(["say", text], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return {"ok": False, "backend": "say", "error": f"say not found: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "backend": "say", "error": f"say timeout: {e}"}
    except Exception as e:  # never-raises
        return {"ok": False, "backend": "say", "error": f"say: {type(e).__name__}: {e}"}
    if r.returncode != 0:
        return {"ok": False, "backend": "say", "error": (r.stderr or "").strip()[:200]}
    return {"ok": True, "backend": "say"}


def _elevenlabs(text: str, *, api_key: str | None = None, voice_id: str | None = None,
                timeout: int = 30) -> dict:
    """ElevenLabs TTS via stdlib urllib + afplay. Never-raises. Key-gated:
    a missing key returns a structured error (the Phase 2a deferral)."""
    api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return {"ok": False, "backend": "elevenlabs", "error": "ELEVENLABS_API_KEY not set"}
    voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID") or _DEFAULT_VOICE
    body = json.dumps({"text": text, "model_id": "eleven_monolingual_v1"}).encode()
    req = Request(f"{_ELEVENLABS_TTS}/{voice_id}", data=body, headers={
        "xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            audio = resp.read()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio)
            path = f.name
        subprocess.run(["afplay", path], capture_output=True, timeout=timeout)
        return {"ok": True, "backend": "elevenlabs", "bytes": len(audio)}
    except HTTPError as e:
        return {"ok": False, "backend": "elevenlabs", "error": f"elevenlabs HTTP {e.code}"}
    except (URLError, TimeoutError, ConnectionError) as e:
        return {"ok": False, "backend": "elevenlabs", "error": f"elevenlabs request failed: {e}"}
    except Exception as e:  # never-raises
        return {"ok": False, "backend": "elevenlabs", "error": f"elevenlabs: {type(e).__name__}: {e}"}


def speak(text: str, *, backend: str = "say", **kwargs) -> dict:
    """Speak `text` via `backend` ("say" | "elevenlabs"). Never-raises; returns
    a structured ``{"ok", "backend", ...}``. An empty/blank `text` is a no-op
    success; an unknown backend is a structured error."""
    if not text or not text.strip():
        return {"ok": True, "backend": backend, "skipped": "empty text"}
    if backend == "say":
        return _say(text, **{k: v for k, v in kwargs.items() if k == "timeout"})
    if backend == "elevenlabs":
        return _elevenlabs(text, **{k: v for k, v in kwargs.items()
                                    if k in ("api_key", "voice_id", "timeout")})
    return {"ok": False, "backend": backend, "error": f"unknown backend: {backend!r}"}
