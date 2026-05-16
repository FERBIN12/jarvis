#!/usr/bin/env bash
# Capture a ~15-second JARVIS demo and convert to docs/demo.gif.
#
# Usage:
#   ./scripts/record_demo.sh [seconds]
#
# Workflow:
#   1. Launch joey (./run.sh) in another terminal
#   2. Run this script. It gives you 3 s to focus your screen.
#   3. Hit Ctrl+Space, type a question, get a reply, ESC.
#   4. Script auto-stops after N seconds (default 15) and converts MP4 → GIF.
#
set -euo pipefail

DURATION="${1:-15}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/docs"
mkdir -p "$OUT_DIR"
TMP_MP4="$(mktemp --suffix=.mp4)"
PALETTE="$(mktemp --suffix=.png)"
trap 'rm -f "$TMP_MP4" "$PALETTE"' EXIT

# Detect screen size
RES=$(xdpyinfo | awk '/dimensions:/ {print $2}')
echo "[demo] Detected screen: $RES"
echo "[demo] Will record for ${DURATION}s — start interacting in 3..."
sleep 1
echo "[demo] 2..."; sleep 1
echo "[demo] 1..."; sleep 1
echo "[demo] RECORDING"

ffmpeg -hide_banner -loglevel warning -y \
  -f x11grab -framerate 20 -video_size "$RES" -i "${DISPLAY:-:0.0}" \
  -t "$DURATION" -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
  "$TMP_MP4"

echo "[demo] Encoding GIF (this takes ~10–20 s)…"

# Two-pass palette method → much smaller, much cleaner GIF
ffmpeg -hide_banner -loglevel warning -y -i "$TMP_MP4" \
  -vf "fps=15,scale=960:-1:flags=lanczos,palettegen=max_colors=128" \
  "$PALETTE"

ffmpeg -hide_banner -loglevel warning -y -i "$TMP_MP4" -i "$PALETTE" \
  -filter_complex "fps=15,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5" \
  "$OUT_DIR/demo.gif"

size_kb=$(du -k "$OUT_DIR/demo.gif" | cut -f1)
echo "[demo] ✓ $OUT_DIR/demo.gif (${size_kb} KB)"
