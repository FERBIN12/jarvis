"""Joey — voice agent. Wake word -> Whisper STT -> claude -p -> Piper TTS."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import webrtcvad
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeWordModel

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"
VOICES_DIR = ROOT / "voices"
SESSIONS_DIR = ROOT / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_LEN = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples
WAKE_THRESHOLD = 0.5
SILENCE_TIMEOUT_S = 1.5     # stop recording after this much silence
MAX_UTTERANCE_S = 20.0      # hard cap on one user utterance
SESSION_IDLE_S = 120.0      # reset conversation after 2 min idle
WAKE_WORD = "hey jarvis"    # pre-trained; swap to custom "hey joey" later

PIPER_VOICE = VOICES_DIR / "en_US-amy-medium.onnx"


def log(msg: str) -> None:
    print(f"[joey] {msg}", flush=True)


class Recorder:
    """Record one utterance: start at first speech, stop after SILENCE_TIMEOUT_S of silence."""

    def __init__(self) -> None:
        self.vad = webrtcvad.Vad(2)  # 0-3, higher = more aggressive

    def record(self) -> np.ndarray:
        frames: list[bytes] = []
        silence_frames = 0
        speech_frames = 0
        started = False
        max_frames = int(MAX_UTTERANCE_S * 1000 / FRAME_MS)
        silence_limit = int(SILENCE_TIMEOUT_S * 1000 / FRAME_MS)

        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=FRAME_LEN,
            dtype="int16",
            channels=1,
        ) as stream:
            for _ in range(max_frames):
                data, _ = stream.read(FRAME_LEN)
                pcm = bytes(data)
                is_speech = self.vad.is_speech(pcm, SAMPLE_RATE)
                if is_speech:
                    started = True
                    speech_frames += 1
                    silence_frames = 0
                    frames.append(pcm)
                elif started:
                    frames.append(pcm)
                    silence_frames += 1
                    if silence_frames >= silence_limit:
                        break
                # before first speech, wait up to ~3s for user to start
                elif not started and len(frames) == 0:
                    silence_frames += 1
                    if silence_frames >= int(3.0 * 1000 / FRAME_MS):
                        return np.zeros(0, dtype=np.int16)

        if speech_frames < 5:  # less than ~150ms of voiced audio
            return np.zeros(0, dtype=np.int16)
        audio = np.frombuffer(b"".join(frames), dtype=np.int16)
        return audio


class WakeWord:
    def __init__(self) -> None:
        log("loading wake word model...")
        self.model = WakeWordModel(
            wakeword_models=["hey_jarvis"],
            inference_framework="onnx",
        )

    def listen(self) -> None:
        log(f"listening for '{WAKE_WORD}'...")
        # openwakeword wants 80ms (1280 samples @ 16kHz) chunks for best accuracy
        chunk = 1280
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=chunk,
            dtype="int16",
            channels=1,
        ) as stream:
            while True:
                data, _ = stream.read(chunk)
                pcm = np.frombuffer(bytes(data), dtype=np.int16)
                preds = self.model.predict(pcm)
                for name, score in preds.items():
                    if score >= WAKE_THRESHOLD:
                        log(f"wake! ({name}={score:.2f})")
                        # drain a bit to avoid retriggering on the same word
                        self.model.reset()
                        return


def play_chime(freq: float = 880.0, ms: int = 120) -> None:
    t = np.linspace(0, ms / 1000.0, int(SAMPLE_RATE * ms / 1000.0), endpoint=False)
    tone = (np.sin(2 * np.pi * freq * t) * 0.2).astype(np.float32)
    # fade in/out to avoid clicks
    fade = int(SAMPLE_RATE * 0.01)
    tone[:fade] *= np.linspace(0, 1, fade)
    tone[-fade:] *= np.linspace(1, 0, fade)
    sd.play(tone, SAMPLE_RATE, blocking=True)


class Whisper:
    def __init__(self) -> None:
        log("loading whisper base model (cpu/int8)...")
        self.model = WhisperModel("base.en", device="cpu", compute_type="int8")

    def transcribe(self, audio_int16: np.ndarray) -> str:
        audio = audio_int16.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1)
        return " ".join(s.text for s in segments).strip()


class Piper:
    """Wrap piper-tts via its python API."""

    def __init__(self) -> None:
        from piper import PiperVoice
        log(f"loading piper voice from {PIPER_VOICE.name}...")
        self.voice = PiperVoice.load(str(PIPER_VOICE))

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return
        rate = chunks[0].sample_rate
        audio = np.concatenate([c.audio_float_array for c in chunks])
        sd.play(audio, rate, blocking=True)


class ClaudeBrain:
    """Talk to Claude Code in headless mode, with persistent session id."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_used: float = 0.0

    def _maybe_reset(self) -> None:
        if self.session_id and (time.time() - self.last_used) > SESSION_IDLE_S:
            log("session timed out — starting fresh.")
            self.session_id = None

    def ask(self, user_text: str) -> str:
        self._maybe_reset()
        cmd = ["claude", "-p", "--output-format", "json"]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        cmd += [user_text]
        log(f"$ {' '.join(cmd[:-1])} <prompt>")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return "Claude timed out, sorry."
        if proc.returncode != 0:
            return f"Claude error: {proc.stderr.strip()[:200]}"
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return proc.stdout.strip()[:500]
        sid = data.get("session_id") or data.get("sessionId")
        if sid:
            self.session_id = sid
        self.last_used = time.time()
        return data.get("result") or data.get("response") or "(no response)"


class HermesAPIBrain:
    """Direct HTTPS calls to Nous Portal inference API (the same backend
    `hermes -z` uses, but without the 5-7s Python CLI cold-start per turn).

    Reads the agent_key from ~/.hermes/auth.json (Hermes CLI keeps it
    refreshed in the background; we just consume what's there). Falls back
    to spawning `hermes status` once if we hit a 401, to nudge a refresh.

    Maintains conversation history in-process for multi-turn memory."""

    AUTH_PATH = Path.home() / ".hermes" / "auth.json"
    CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"
    DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
    IDLE_S = 180.0
    MAX_TOKENS = 1024

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.last_used = 0.0
        self._model: str | None = None

    def _resolve_model(self) -> str:
        if self._model is not None:
            return self._model
        # Honor hermes config model.default if set
        try:
            import yaml  # PySide6 brings yaml in transitively; falls back gracefully
            if self.CONFIG_PATH.exists():
                cfg = yaml.safe_load(self.CONFIG_PATH.read_text()) or {}
                m = (cfg.get("model") or {}).get("default")
                if m:
                    self._model = m
                    return m
        except Exception:
            pass
        self._model = self.DEFAULT_MODEL
        return self._model

    def _load_auth(self) -> dict:
        with open(self.AUTH_PATH) as f:
            return json.load(f)["providers"]["nous"]

    def _refresh_via_hermes(self) -> None:
        """Last-resort 401 recovery: kick hermes to mint a fresh agent_key.
        Amortized — only happens when the cached key expires (~daily)."""
        try:
            subprocess.run(["hermes", "status"], capture_output=True, timeout=20)
        except Exception:
            pass

    def _post_chat(self, payload: dict) -> dict:
        import urllib.error
        import urllib.request

        def _do():
            auth = self._load_auth()
            req = urllib.request.Request(
                auth["inference_base_url"] + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {auth['agent_key']}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())

        try:
            return _do()
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            log("brain: 401 — refreshing agent_key via hermes status…")
            self._refresh_via_hermes()
            return _do()

    def ask(self, user_text: str) -> str:
        if self.messages and (time.time() - self.last_used) > self.IDLE_S:
            log("brain: idle reset, fresh session.")
            self.messages = []
        self.messages.append({"role": "user", "content": user_text})
        model = self._resolve_model()
        log(f"$ POST chat/completions  model={model}  msgs={len(self.messages)}")
        t0 = time.time()
        try:
            data = self._post_chat({
                "model": model,
                "messages": self.messages,
                "max_tokens": self.MAX_TOKENS,
            })
        except Exception as e:
            self.messages.pop()  # rollback so failed turn doesn't poison history
            return f"API error: {e}"
        dt = time.time() - t0
        try:
            reply = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError):
            reply = ""
        log(f"brain: {dt:.2f}s  ({len(reply)} chars)")
        self.messages.append({"role": "assistant", "content": reply})
        self.last_used = time.time()
        return reply or "(empty reply)"


class HermesBrain:
    """Original `hermes -z` subprocess path — kept as fallback. Slower
    (~5-10s per turn) because hermes is a heavy Python CLI that cold-starts
    every call. Prefer HermesAPIBrain."""

    HERMES_IDLE_S = 180.0  # 3 min between turns before starting fresh

    def __init__(self) -> None:
        self.has_session = False
        self.last_used = 0.0

    def _maybe_reset(self) -> None:
        if self.has_session and (time.time() - self.last_used) > self.HERMES_IDLE_S:
            log("hermes session idle — starting fresh.")
            self.has_session = False

    def ask(self, user_text: str) -> str:
        self._maybe_reset()
        cmd = ["hermes", "-z", user_text]
        if self.has_session:
            cmd.append("--continue")
        log(f"$ hermes -z <prompt>{' --continue' if self.has_session else ''}")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return "Hermes timed out, sorry."
        if proc.returncode != 0:
            return f"Hermes error: {proc.stderr.strip()[:200]}"
        self.has_session = True
        self.last_used = time.time()
        return proc.stdout.strip() or "(no response)"


def main() -> None:
    if not PIPER_VOICE.exists():
        log(f"ERROR: piper voice not found at {PIPER_VOICE}")
        log("run: bash scripts/download_models.sh first")
        sys.exit(1)

    wake = WakeWord()
    rec = Recorder()
    whisper = Whisper()
    piper = Piper()
    brain = ClaudeBrain()

    log("joey ready.")
    while True:
        try:
            wake.listen()
            play_chime(880, 100)
            audio = rec.record()
            if audio.size == 0:
                log("no speech detected.")
                continue
            user_text = whisper.transcribe(audio)
            if not user_text:
                log("whisper returned empty transcript.")
                continue
            log(f"USER: {user_text}")
            response = brain.ask(user_text)
            log(f"CLAUDE: {response}")
            piper.speak(response)
        except KeyboardInterrupt:
            log("bye.")
            return
        except Exception as e:
            log(f"loop error: {e!r}")
            time.sleep(1)


if __name__ == "__main__":
    main()
