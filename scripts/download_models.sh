#!/usr/bin/env bash
# Downloads the wake-word, Whisper and Piper voice models for joey.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VOICES="$ROOT/voices"
mkdir -p "$VOICES"

# Piper voice: en_US-amy-medium (a clear, natural female voice; ~64MB onnx)
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium"
if [[ ! -f "$VOICES/en_US-amy-medium.onnx" ]]; then
  echo "[download] piper voice (amy, medium)..."
  curl -fL "$VOICE_BASE/en_US-amy-medium.onnx"      -o "$VOICES/en_US-amy-medium.onnx"
  curl -fL "$VOICE_BASE/en_US-amy-medium.onnx.json" -o "$VOICES/en_US-amy-medium.onnx.json"
else
  echo "[skip] piper voice already present"
fi

echo
echo "openWakeWord 'hey_jarvis' and faster-whisper 'base.en' download on first run."
echo "Done."
