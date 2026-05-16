<div align="center">

```
       ▄▄▄██████████▄▄▄
     ▄██▀▀░░  ░░░░ ░░▀▀██▄        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
   ▄██▀░ ░░░ ▄▄████▄▄ ░░░▀██▄     ┃  J  A  R  V  I  S            ┃
  ██▀░░░░  ▄██▀░ ░▀██▄  ░░░░▀██   ┃  voice-first AI agent overlay┃
 ██░░░░░░ ██▀  ▄  ░▀██ ░░░░░░██   ┃                              ┃
██░░░░░░░██░  ███   ██░░░░░░░██   ┃  // ECHELON 7.21 // ARMED    ┃
██░░░░░░░██░  ▀▀▀  ██░░░░░░░░██   ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
 ██░░░░░░ ██▄    ▄██ ░░░░░░░██
  ██▄░░░░  ▀██████▀  ░░░░░▄██
   ▀██▄░░░  ░░ ░░░░░░ ░░░▄██▀
     ▀██▄▄░░  ░░░░ ░░░▄▄██▀
       ▀▀▀██████████▀▀▀
```

**A fullscreen Iron-Man-inspired HUD that fades over your desktop.**
Wake-word + Whisper STT + Hermes/OpenClaw/Claude brain + Piper TTS.
Every visual element on screen displays real telemetry.

[![Python](https://img.shields.io/badge/python-3.11-blue?style=flat-square)](https://www.python.org/)
[![Qt](https://img.shields.io/badge/Qt-PySide6-41cd52?style=flat-square)](https://wiki.qt.io/Qt_for_Python)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%E2%9A%99-orange?style=flat-square)]()
[![Brain](https://img.shields.io/badge/brain-OpenClaw%20%E2%86%92%20Haiku%204.5-c060ff?style=flat-square)]()

</div>

![JARVIS demo](docs/demo.gif)

> Built as a stepping-stone toward a long-term goal: a **physical AI robot** for
> personal task automation. JARVIS develops the voice / agent / UX layer; the
> brain stack is intentionally agent-router-first (OpenClaw → routes →
> Hermes/Claude/Codex/etc) so skills and procedural memory will accumulate as
> the project matures.

---

## ▶ Quick start

```bash
# 1. clone
git clone https://github.com/FERBIN12/jarvis.git ~/joey && cd ~/joey

# 2. python env
curl -fsSL https://astral.sh/uv/install.sh | bash
uv venv --python 3.11
uv pip install -e .

# 3. download Piper voice (Whisper + wake-word auto-fetch on first run)
bash scripts/download_models.sh

# 4. wire a brain — pick one
hermes auth add nous --type oauth          # free DeepSeek tier
# or just rely on `claude` (already installed) — JOEY_BRAIN=claude-haiku
# or route through OpenClaw (default) if you have it

# 5. bind Ctrl+Space → joey-trigger
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']"
SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
gsettings set "$SCHEMA" name "Joey wake"
gsettings set "$SCHEMA" command "$PWD/joey-trigger"
gsettings set "$SCHEMA" binding "<Control>space"

# 6. (optional) auto-start on every login
mkdir -p ~/.config/systemd/user
cp systemd/joey.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now joey.service

# 7. launch
./run.sh
```

Expected boot output:
```
[joey] loading TTS + brain (HUD mode, mic disabled, brain=openclaw)...
[joey] loading piper voice from en_US-amy-medium.onnx...
[joey] joey HUD ready.
[joey] trigger socket: /home/<you>/.joey/trigger.sock
[joey] voice: listening for 'hey jarvis'
```

Now press **<kbd>Ctrl</kbd>+<kbd>Space</kbd>** (silent text) or say **"hey jarvis"** (full voice).

---

## ▶ Two modes, one HUD

| Mode | Trigger | Behavior | Use case |
|------|---------|----------|----------|
| **🤫 Silent** | <kbd>Ctrl</kbd>+<kbd>Space</kbd> | HUD opens, type, reply renders on screen — **no audio output** | Meetings, libraries, shared offices |
| **🔊 Voice** | "hey jarvis" | Mic → Whisper → brain → Piper speaks back | Hands-free, cooking, debugging hardware |

Both modes share the same conversation memory — alternate freely.
**ESC** dismisses the HUD; **45 s idle** in listening mode auto-hides.

---

## ▶ HUD anatomy — *every pixel is data*

```
┌──────────────────────────────────────────────────────────────────────────┐
│  JARVIS // HEY JARVIS // ECHELON 7.21                T+0042s STATE//... │   ← header
│ ╔══════════════╗                                       ╔══════════════╗  │
│ ║ TELEMETRY    ║                                       ║ SUBSYSTEMS   ║  │
│ ║ CPU   12.4%  ║                                       ║ BRAIN openclaw│  │
│ ║ MEM   47.1%  ║                                       ║ LATENCY 1842 │  │
│ ║ NET ↓ 12.4   ║              ╭──── arcs ────╮         ║ WAKE 0.93/0.65│  │
│ ║ NET ↑  3.1   ║         ╱  CPU %  60s history  ╲       ║ TURNS    7   │  │
│ ║ DISK r/w …   ║       ╱   MEM % filled arc      ╲      ║ MIC PK 47.2% │  │
│ ║ TEMP  69 °C  ║      │     ╱╲  NET ↑↓ split  ╱╲   │    ║ STT WHISPER  │  │
│ ║ BATT ⚡ 78%  ║      │ 96× audio bars (mic)   │      ║ TTS PIPER    │  │
│ ╚══════════════╝       ╲      ╳────╳            ╱     ╚══════════════╝  │
│                          ╲   ▓ SPHERE ▓        ╱                          │
│ ▌MIC RMS                  ╲   reactor core    ╱                CPU 60s ▌ │
│ ▌  bars                     ╲▒  pulse  ▒▓   ╱                    bars ▌ │
│ ▌                            ╲      ╱╲   ╱                            ▌ │
│ ▌                              ╲──╳──╳──╱                              ▌ │
│ ╔══════════════╗                                       ╔══════════════╗  │
│ ║ HISTORY      ║              radar scan sweep         ║ MIC // SCOPE ║  │
│ ║ > prev q     ║              ↻                        ║   ∿∿∿  ∿∿∿   ║  │
│ ║   prev reply ║                                       ║   real wave  ║  │
│ ║ > prev q     ║              status text              ║   RMS 4.2%   ║  │
│ ╚══════════════╝               ┌─────────────────┐     ╚══════════════╝  │
│              ┌──── RESPONSE //                   ────┐                   │
│              │  reply text (wrapped, multi-line)    │                   │
│              └───────────────────────────────────────┘                   │
│                          >> awaiting query…                              │
└──────────────────────────────────────────────────────────────────────────┘
```

| Region | Data source | What you're seeing |
|---|---|---|
| **TELEMETRY** (top-left) | `psutil` @ 1Hz | live CPU, MEM, NET ↓ ↑ KB/s, DISK r/w, TEMP °C, BATT %, process count |
| **SUBSYSTEMS** (top-right) | JoeyApp internal state | active brain backend, last reply latency (ms), turn count, last wake-word score / threshold, mic peak %, STT/TTS models |
| **HISTORY** (bottom-left) | conversation buffer | last 4 (user, reply) pairs |
| **MIC // SCOPE** (bottom-right) | `VoiceWorker.mic_rms_hist` | real-time oscilloscope of the last 60 mic frames (80 ms each) |
| **OUTER arc** (r=320) | `SystemMonitor.cpu_hist` | CPU% per second, newest at 12 o'clock — segment thickness scales with load |
| **MIDDLE arc** (r=250) | `psutil.virtual_memory().percent` | filled arc sweep, 0..100% |
| **INNER arc** (r=200) | net byte deltas / 1s | top semicircle = download span, bottom = upload, log-scaled |
| **96 audio bars** around orb | live mic RMS history | actual voice amplitude — they react to you in real time |
| **Side spectrum bars** | mic RMS (left) / CPU (right) | independent EQ-style histories |
| **Central sphere** | sphere phase + flicker | pulsing core, 4-layer bloom, 8 reactor spokes, brightness flickers with state |
| **Energy beams** | beam phase animation | dashed lines flowing from sphere edge to each corner panel — flow direction toward the data sink |
| **Radar sweep** | scan phase | 26° cone sweeping around the orb, paced by state |
| **Outer rim runners** | rim phase | 8 glowing dots sliding clockwise around screen edge |
| **Particle field** | per-particle random walk | 70 drifting glow dots — pure energy field |
| **CRT scanlines** | static overlay | every 3px horizontal line at low alpha — projection effect |

State-driven palette:

| State | Primary | Accent | Mood |
|---|---|---|---|
| `hidden` | `#3c64a0` | `#00b4ff` | dormant |
| `listening` | `#00b4ff` | `#82ebff` | cyan, awaiting |
| `thinking` | `#9650ff` | `#c382ff` | **violet, computing** |
| `speaking` | `#3cffb4` | `#8cffdc` | cyan-green, responding |

---

## ▶ Brain backends

JARVIS doesn't lock you into one provider. `JOEY_BRAIN` env var picks the LLM path:

| `JOEY_BRAIN=` | Class | First-text | Total | Cost | Streams? | Notes |
|---|---|---:|---:|---|:---:|---|
| `openclaw` *(default)* | `OpenClawBrain` | ~2.5 s | **~3 s** | $0 *(uses Claude Code OAuth via OpenClaw)* | No | Routes through OpenClaw gateway — same brain other channels (Discord/WhatsApp) use |
| `claude-haiku` | `ClaudeCodeBrain` | ~1.7 s | **~2 s** | $0 | ✅ Yes | `claude -p --model haiku --effort low` — fastest path |
| `claude-full` | `ClaudeBrain` | ~2-3 s | ~4-5 s | $0 | No | Full claude agent with default Sonnet |
| `hermes-api` | `HermesAPIBrain` | ~5 s | ~6 s | $0 | ✅ Yes | Direct HTTPS to Nous Portal, free DeepSeek-V4-Flash (reasoning model — slower TTFT) |
| `hermes-cli` | `HermesBrain` | ~9 s | ~11 s | $0 | No | `hermes -z` subprocess — Python cold-start eats most of the latency |

**Conversation memory** is preserved across turns:
- ClaudeCodeBrain → `--resume <session_id>`
- OpenClawBrain → inlines last 6 turns into the prompt
- HermesAPIBrain → in-process `messages[]` history
- All reset after 3 min idle

Set per-launch:
```bash
JOEY_BRAIN=claude-haiku ./run.sh
JOEY_OPENCLAW_MODEL=openai-codex/gpt-5.5 ./run.sh   # override openclaw routing
```

---

## ▶ System architecture

```
                ┌───────────────────────────────────────────────┐
   user input ► │                  JOEY UI                       │ ◄ HUD render
                │  ┌──────────────┐    ┌──────────────────────┐  │
   "hey jarvis" │  │ VoiceWorker  │    │   JarvisHUD          │  │
       ───────► │  │  • wake      │    │   • paintEvent       │  │
                │  │  • record    │───►│   • 3 arcs           │  │
                │  │  • Whisper   │    │   • 4 panels         │  │
                │  └──────────────┘    │   • particles/scope  │  │
   Ctrl+Space   │  ┌──────────────┐    │   • response panel   │  │
   ───────────► │  │ TriggerSocket│───►│                      │  │
   (unix socket)│  └──────────────┘    └──────────┬───────────┘  │
                │  ┌──────────────┐               │              │
                │  │ SystemMonitor│──telemetry──► │              │
                │  │  • psutil    │               │              │
                │  └──────────────┘               ▼              │
                │           ┌───────────────────────────────┐    │
                │           │       BrainWorker (QThread)    │    │
                │           │                                 │    │
                │           │  brain.ask_stream(prompt) ────┐ │    │
                │           │                ▲              │ │    │
                │           │       sentence │              │ │    │
                │           │       chunks   │              │ │    │
                │           │                ▼              │ │    │
                │           │       piper.speak(sentence)  ◄┘ │    │
                │           └───────────┬────────────────────┘    │
                └───────────────────────┼───────────────────────────┘
                                        ▼
                ┌───────────────────────────────────────────────┐
                │                BRAIN BACKEND                   │
                │  ┌────────────┐  ┌────────────┐  ┌──────────┐ │
                │  │ OpenClaw   │  │ Claude     │  │ Hermes   │ │
                │  │ gateway    │  │ Code OAuth │  │ Portal   │ │
                │  │ :18789     │  │ subscription│  │ OAuth   │ │
                │  └────────────┘  └────────────┘  └──────────┘ │
                └───────────────────────────────────────────────┘
```

Key files:

```
joey/
├── joey.py                  # Audio primitives + all Brain classes
│   • WakeWord, Recorder, Whisper, Piper
│   • ClaudeBrain, ClaudeCodeBrain, HermesBrain,
│     HermesAPIBrain, OpenClawBrain
├── joey_ui.py               # PySide6 fullscreen JARVIS HUD
│   • JarvisHUD              ← paintEvent + all the visuals
│   • TriggerSocket          ← Unix-domain socket → Ctrl+Space wake
│   • VoiceWorker            ← background thread: wake → STT loop
│   • BrainWorker            ← background thread: brain → TTS
│   • SystemMonitor          ← background thread: psutil @ 1Hz
│   • JoeyApp                ← top-level signal wiring
├── joey-trigger             # Tiny Python shim — connects to socket
├── run.sh                   # Launcher
├── pyproject.toml           # uv-managed deps
├── scripts/
│   ├── download_models.sh   # Pull Piper voice
│   └── record_demo.sh       # ffmpeg → optimized GIF for README
└── systemd/
    └── joey.service         # Auto-start on graphical-session.target
```

---

## ▶ Configuration

All env vars are optional. Defaults are listed.

| Env var | Default | What it does |
|---|---|---|
| `JOEY_BRAIN` | `openclaw` | Brain backend (see table above) |
| `JOEY_VOICE` | `1` | `0` disables wake-word listening (text only) |
| `JOEY_OPENCLAW_MODEL` | `anthropic/claude-haiku-4-5` | Override model OpenClaw routes to |
| `JOEY_WAKE_THRESHOLD` | `0.65` | openWakeWord sensitivity. `0.75`+ if false fires |
| `JOEY_WAKE_FRAMES` | `2` | Consecutive frames above threshold to trigger |

Code-level tunables in `joey.py`:

| Constant | Default | Effect |
|---|---|---|
| `WAKE_WORD` | `"hey jarvis"` | Display label (model id: `hey_jarvis`) |
| `SILENCE_TIMEOUT_S` | `1.5` | VAD silence to end an utterance |
| `MAX_UTTERANCE_S` | `20.0` | Hard cap on a single voice query |
| `SESSION_IDLE_S` | `120.0` | Claude session reset window |

---

## ▶ Auto-start (systemd user service)

```bash
cp systemd/joey.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now joey.service

# Manage:
systemctl --user status  joey      # current state
systemctl --user restart joey      # after edits
systemctl --user stop    joey      # stop until next login
systemctl --user disable joey      # turn off auto-start
journalctl   --user -u   joey -f   # live log tail
```

The service waits for `graphical-session.target` (X11 + GNOME up), restarts on
failure (4 retries / 120 s window), and exports the right `DISPLAY` /
`XAUTHORITY` env so Qt finds the X server.

---

## ▶ Custom wake word ("hey joey", "computer", …)

`hey_jarvis` is the only **`Hey X`** model openWakeWord ships pre-trained. To
roll your own:

1. Generate ~5 000 synthetic utterances of your phrase using
   [openWakeWord's training Colab](https://github.com/dscripka/openWakeWord/blob/main/notebooks/training_models.ipynb)
   (Piper TTS + a permuted prompt list ≈ 30 min on a free T4).
2. Train the classifier in the same notebook (~10 min).
3. Drop `hey_joey.onnx` + `hey_joey.tflite` into `~/.cache/openwakeword/`.
4. Edit `joey.py`:
   ```python
   WAKE_WORD = "hey joey"
   class WakeWord:
       def __init__(self) -> None:
           self.model = WakeWordModel(
               wakeword_models=["hey_joey"],  # ← your model id
               inference_framework="onnx",
           )
   ```
5. `systemctl --user restart joey`.

---

## ▶ Performance

Measured on Intel i5-1235U, 16 GB RAM, X11 + GNOME on Ubuntu 24.04, 1920×1080
@ 30 fps full-screen redraw.

| Component | Idle | Active |
|---|---:|---:|
| `joey_ui.py` CPU | ~10-15% | ~20-25% |
| `joey_ui.py` RAM | ~600 MB | ~700 MB (after first reply) |
| Wake-word inference | ~3% on top of idle | (same) |
| Whisper STT latency | — | ~0.4-0.7 s (5-word query) |
| Piper TTS synthesis | — | ~0.4-1.5 s (per sentence) |
| Brain (Claude Haiku) | — | ~1.7 s first text |
| **End-to-end "hey jarvis ... reply"** | — | **~3-5 s** |

If your CPU is older / battery-bound: drop `_anim.setInterval(33)` to `50` ms
in `joey_ui.py` for 20 fps and ~7% lower CPU at minimal visual cost.

---

## ▶ Troubleshooting

<details>
<summary><b>No audio out / TTS silent</b></summary>

PipeWire stuck in `pro-audio` profile is the usual culprit:
```bash
systemctl --user restart wireplumber pipewire pipewire-pulse
pactl set-card-profile alsa_card.pci-0000_00_1f.3-platform-skl_hda_dsp_generic HiFi
```
</details>

<details>
<summary><b>Mic captures nothing</b></summary>

Default source is on the headphone jack instead of internal mic:
```bash
pactl list sources short | grep -i microphone
pactl set-default-source alsa_input.…_6__source   # the Digital Microphone one
```
</details>

<details>
<summary><b>Ctrl+Space does nothing</b></summary>

Confirm the binding:
```bash
gsettings list-recursively \
  org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:\
/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/
```
If empty, re-run the binding commands from *Quick start* step 5.
</details>

<details>
<summary><b>Hermes: "Model X not available on Free Tier"</b></summary>

```bash
hermes config set model.default deepseek/deepseek-v4-flash
```
</details>

<details>
<summary><b>"openclaw: command not found" from systemd</b></summary>

Already fixed — `OpenClawBrain._resolve_binary` searches `~/.nvm/versions/node/<ver>/bin`
even when systemd strips PATH. If you renamed/moved openclaw, point
`JOEY_OPENCLAW_BIN=<path>` at the binary explicitly.
</details>

<details>
<summary><b>HUD opens but typing doesn't register</b></summary>

The HUD calls `grabKeyboard()` after focus reclaim, plus an X11
`_NET_ACTIVE_WINDOW` ClientMessage via `python-xlib` to bypass mutter's
focus-stealing prevention. If this fails on a non-GNOME compositor, tell
me the WM and I'll add a fallback.
</details>

---

## ▶ Roadmap

- [ ] Custom `"hey joey"` wake-word model
- [ ] Streaming Piper TTS (start audio mid-synthesis instead of per-sentence)
- [ ] MCP server integration → filesystem / browser / shell tools via voice
- [ ] Hermes GEPA skills layer → persistent procedural memory across sessions
- [ ] Spotify control via Hermes auth provider — `"hey jarvis, play X"`
- [ ] Echo / smart speaker bridge — Alexa Skill webhook → Joey backend
- [ ] Multi-monitor support
- [ ] Wayland fallback path (currently X11-only)
- [ ] Mobile companion via Hermes' Telegram / Signal bridges

---

## ▶ Tech stack

| Layer | Library |
|---|---|
| UI | [PySide6 / Qt 6](https://doc.qt.io/qtforpython-6/) |
| Wake word | [openWakeWord](https://github.com/dscripka/openWakeWord) (ONNX, CPU) |
| STT | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`base.en`, int8) |
| VAD | [webrtcvad-wheels](https://github.com/wiseman/py-webrtcvad) |
| TTS | [Piper](https://github.com/rhasspy/piper) (`en_US-amy-medium`) |
| Audio I/O | [sounddevice](https://python-sounddevice.readthedocs.io/) + PipeWire |
| Brain (default) | [OpenClaw](https://github.com/openclaw/openclaw) → Claude Haiku 4.5 |
| Brain (alt) | [Hermes Agent](https://hermes-agent.nousresearch.com/), [Claude Code](https://docs.claude.com/claude-code) |
| X11 focus | [python-xlib](https://github.com/python-xlib/python-xlib) |
| System telemetry | [psutil](https://github.com/giampaolo/psutil) |

---

## ▶ License

[MIT](LICENSE) — do whatever, attribution appreciated.

---

<div align="center">

*Close that gap between model and reality. That's still the job.*

</div>
