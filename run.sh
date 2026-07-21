#!/usr/bin/env bash
# Launch joey UI (pill overlay + Claude brain + Piper TTS).
# Bind Ctrl+Space in GNOME Settings → Keyboard → Custom Shortcuts → ./joey-trigger
cd "$(dirname "$0")"
exec .venv/bin/python joey_ui.py
