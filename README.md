# JARVIS

A voice-first, fullscreen AI agent overlay for Linux desktops. Wake-word activated,
Hermes-Agent-backed, with an Iron-Man-inspired HUD that fades in over your screen.

Press `Ctrl+Space` for **silent text mode** (meeting-safe — read the reply, no
audio out). Say **"hey jarvis"** for the full voice loop (mic → STT → agent → TTS).

> Built as a stepping-stone toward a long-term goal: a physical AI robot for
> personal task automation. JARVIS develops the voice/agent layer; the brain
> is intentionally Hermes Agent so skills and procedural memory will accumulate
> over time.

---

## Features

| | |
|---|---|
| **Wake word** | openWakeWord — `"hey jarvis"` (custom phrases trainable) |
| **STT** | faster-whisper, `base.en` model, CPU + int8 (no GPU required) |
| **Brain** | Hermes Agent (`hermes -z … --continue` for multi-turn memory) via Nous Portal OAuth; Claude Code fallback via `JOEY_BRAIN=claude` |
| **TTS** | Piper-TTS, `en_US-amy-medium` (local, fast, natural) |
| **VAD** | webrtcvad — utterances bounded by 1.5s of silence |
| **UI** | PySide6 fullscreen HUD: rotating rings, audio bars, sphere, corner telemetry panels, wireframe globe, scan sweep |
| **Trigger** | `Ctrl+Space` (text/silent) **or** "hey jarvis" (voice/audio) |
| **Multi-turn** | Conversation persists across turns; 3 min idle before fresh session |
| **Dismiss** | `ESC`, or 15 s without typing |

---

## Quick start

Tested on Ubuntu 24.04 (X11 session, PipeWire). Requires Python 3.11.

```bash
# 1. Clone
git clone https://github.com/FERBIN12/jarvis.git ~/joey
cd ~/joey

# 2. Install uv (fast Python package manager)
curl -fsSL https://astral.sh/uv/install.sh | bash

# 3. Create venv + install deps
uv venv --python 3.11
uv pip install -e .

# 4. Download Piper voice (Whisper + wake-word download on first run)
bash scripts/download_models.sh

# 5. Wire up Hermes Agent (one-time)
#    - Install Hermes:  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
#    - Authenticate:    hermes auth add nous --type oauth
#    - Pick a free model: hermes config set model.default deepseek/deepseek-v4-flash

# 6. Bind Ctrl+Space to ./joey-trigger via GNOME custom shortcut
#    (Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts)
#    Command: /home/<you>/joey/joey-trigger     Shortcut: Ctrl+Space

# 7. Launch
./run.sh
```

You should see:
```
[joey] joey HUD ready.
[joey] trigger socket: /home/you/.joey/trigger.sock
[joey] voice: listening for 'hey jarvis'
```

Press **Ctrl+Space** or say **"hey jarvis"** to activate.

---

## How it works

```
┌─────────────────────────────────────────────────────────────┐
│            user input                                        │
│  ┌────────────┐          ┌─────────────────┐                │
│  │ Ctrl+Space │  ──→     │ Unix socket     │ ──┐            │
│  │ (silent)   │          │ joey-trigger    │   │            │
│  └────────────┘          └─────────────────┘   │            │
│  ┌────────────┐          ┌─────────────────┐   ▼            │
│  │ "hey jarvis"│ ──→     │ openWakeWord   │ ──┴──┐          │
│  │ (voice)    │          │ + VAD recorder  │      │         │
│  └────────────┘          └─────────────────┘      │         │
│                                                    ▼         │
│                                          ┌─────────────────┐│
│                                          │  JarvisHUD      ││
│                                          │  (PySide6)      ││
│                                          │  state machine: ││
│                                          │  listening →    ││
│                                          │  thinking →     ││
│                                          │  speaking →     ││
│                                          │  listening      ││
│                                          └────────┬────────┘│
│                                                   │         │
│            text input    ┌──────────┐    ┌────────▼────────┐│
│            ── or ──  ──→ │ Whisper  │ ─→ │ HermesBrain     ││
│            transcript    │ base.en  │    │ hermes -z       ││
│                          └──────────┘    │ --continue      ││
│                                          └────────┬────────┘│
│                                                   │ reply   │
│                                          ┌────────▼────────┐│
│                                  silent? │ Piper TTS       ││
│                                  no →    │ + sounddevice   ││
│                                          └─────────────────┘│
└─────────────────────────────────────────────────────────────┘
```

- **`joey.py`** owns the audio-pipeline primitives: `WakeWord`, `Recorder` (VAD), `Whisper`, `Piper`, `ClaudeBrain`, `HermesBrain`.
- **`joey_ui.py`** owns the UI: `JarvisHUD` (fullscreen overlay), `VoiceWorker` (background wake-word loop), `BrainWorker` (background brain+TTS), `TriggerSocket` (Unix-domain socket listening for hotkey triggers), `JoeyApp` (signal wiring).
- **`joey-trigger`** is a tiny Python shim bound to `Ctrl+Space` via GNOME custom keybinding. Connecting to the socket fires the wake.
- X11 focus management uses **python-xlib** to send `_NET_ACTIVE_WINDOW` with `CurrentTime`, bypassing GNOME's focus-stealing prevention. The QLineEdit also grabs the keyboard so keys land on it regardless of WM focus.

---

## Modes

### Silent text (Ctrl+Space)

```
Ctrl+Space  →  HUD appears  →  type query  →  Enter
            →  brain replies  →  HUD displays reply text (no audio)
            →  back to listening for follow-up
            →  ESC or 45 s idle to dismiss
```

Use case: meetings, shared offices, library — the agent answers you on screen
without broadcasting anything.

### Voice ("hey jarvis")

```
"hey jarvis"  →  HUD appears  →  speak query (auto-stops on silence)
              →  Whisper transcribes  →  brain replies
              →  Piper speaks the reply aloud
              →  back to listening for another "hey jarvis"
```

Use case: hands-busy moments — cooking, debugging hardware, walking around.

---

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `JOEY_BRAIN` | `hermes` | Set to `claude` to use `claude -p` instead of `hermes -z` |
| `JOEY_VOICE` | `1` | Set to `0` to disable wake-word listening (text only) |

Tunables in `joey.py`:

| Constant | Default | Effect |
|---|---|---|
| `WAKE_THRESHOLD` | `0.5` | Wake-word sensitivity (raise to `0.6` if false triggers) |
| `SILENCE_TIMEOUT_S` | `1.5` | How long of silence ends an utterance |
| `MAX_UTTERANCE_S` | `20.0` | Hard cap on a single voice query |
| `SESSION_IDLE_S` | `120.0` | Claude session reset window (Claude brain only) |
| `WAKE_WORD` | `"hey jarvis"` | Display string (model is `hey_jarvis` pre-trained) |

`HermesBrain.HERMES_IDLE_S` (in `joey.py`) is the Hermes idle reset, default 180 s.

---

## Custom wake word

The `hey_jarvis` model ships pre-trained with openWakeWord. To use a custom
phrase like `"hey joey"`:

1. Use the [openWakeWord Colab notebook](https://github.com/dscripka/openWakeWord/blob/main/notebooks/training_models.ipynb) to generate synthetic samples and train a custom `.onnx` model (~30 min).
2. Drop the resulting `hey_joey.onnx` (and `.tflite`) into `~/.cache/openwakeword/`.
3. Update `core.WakeWord.__init__` in `joey.py` to load `["hey_joey"]` instead of `["hey_jarvis"]`, and `WAKE_WORD = "hey joey"`.

---

## Troubleshooting

**No audio output / TTS silent**: PipeWire may be stuck in `pro-audio` profile. Fix:
```bash
systemctl --user restart wireplumber pipewire pipewire-pulse
pactl set-card-profile alsa_card.pci-0000_00_1f.3-platform-skl_hda_dsp_generic HiFi
```

**Mic captures nothing**: default source might be on the headphone jack instead
of the built-in mic. Fix:
```bash
pactl set-default-source alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__hw_sofhdadsp_6__source
```
(Source name varies; check `pactl list sources short | grep -i microphone`.)

**Ctrl+Space does nothing**: confirm the binding is registered:
```bash
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
```
If empty, re-run the setup in `Quick start` step 6.

**Hermes errors `Model 'X' is not available on the Free Tier`**: switch the default model:
```bash
hermes config set model.default deepseek/deepseek-v4-flash
```

---

## Roadmap

- [ ] Custom `"hey joey"` wake-word model
- [ ] Streaming TTS (start speaking before full reply arrives)
- [ ] Strip markdown from replies before TTS (currently asterisks are voiced)
- [ ] MCP server integration for tool use (filesystem, terminal, browser)
- [ ] Skill accumulation via Hermes GEPA — persistent procedural memory
- [ ] Multi-monitor support (currently primary monitor only)
- [ ] Wayland fallback path (currently X11-only due to focus management)
- [ ] Mobile/tablet companion via Hermes' Telegram/Signal bridge

---

## Credits & tech stack

- [Hermes Agent](https://hermes-agent.nousresearch.com/) — self-improving agent framework
- [Nous Portal](https://portal.nousresearch.com/) — model gateway (free DeepSeek-V4-Flash tier)
- [openWakeWord](https://github.com/dscripka/openWakeWord) — wake-word detection
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — STT
- [Piper TTS](https://github.com/rhasspy/piper) — TTS
- [PySide6](https://doc.qt.io/qtforpython-6/) — UI

## License

MIT.
