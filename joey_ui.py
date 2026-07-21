"""Joey UI — Jarvis-style HUD overlay.

Rotating rings, pulsing sphere, audio-reactive bars, corner brackets, scan sweep.
States: hidden | listening | thinking | speaking
Trigger: Ctrl+Space (bound via GNOME custom shortcut → ./joey-trigger).
"""
from __future__ import annotations

import math
import os
import random
import re
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSocketNotifier,
    QThread,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QLineEdit,
    QWidget,
)

import joey as core

ROOT = Path(__file__).resolve().parent
SOCK_PATH = Path.home() / ".joey" / "trigger.sock"
SOCK_PATH.parent.mkdir(exist_ok=True)


# ---------- helpers ----------
_MD_FENCE   = re.compile(r"```[\s\S]*?```")
_MD_BTICK   = re.compile(r"`([^`]+)`")
_MD_BOLD_S  = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITAL_S  = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_BOLD_U  = re.compile(r"__([^_]+)__")
_MD_ITAL_U  = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_MD_HDR     = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LINK    = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_HRULE   = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_MD_QUOTE   = re.compile(r"^>\s?", re.MULTILINE)
_MD_LIST    = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_MD_BLANK3  = re.compile(r"\n{3,}")
_SENT_END   = re.compile(r"[.!?\n](?:\s|$)")


def _strip_markdown(text: str) -> str:
    """Remove common markdown formatting so the HUD doesn't render literal
    `**` / `#` characters and Piper TTS doesn't pronounce them."""
    if not text:
        return text
    text = _MD_FENCE.sub("", text)
    text = _MD_BTICK.sub(r"\1", text)
    text = _MD_BOLD_S.sub(r"\1", text)
    text = _MD_BOLD_U.sub(r"\1", text)
    text = _MD_ITAL_S.sub(r"\1", text)
    text = _MD_ITAL_U.sub(r"\1", text)
    text = _MD_HDR.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HRULE.sub("", text)
    text = _MD_QUOTE.sub("", text)
    text = _MD_LIST.sub("• ", text)
    text = _MD_BLANK3.sub("\n\n", text)
    return text.strip()


def _wrap_text(text: str, fm, max_width: int) -> list[str]:
    """Greedy word-wrap. Falls back to hard char-split for tokens longer
    than max_width (e.g. URLs, code identifiers)."""
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        words = raw_line.split(" ")
        cur = ""
        for w in words:
            candidate = f"{cur} {w}" if cur else w
            if fm.horizontalAdvance(candidate) <= max_width:
                cur = candidate
            else:
                if cur:
                    lines.append(cur)
                # Hard-split anything still too wide on its own
                while fm.horizontalAdvance(w) > max_width and len(w) > 1:
                    # find split point that fits
                    lo, hi = 1, len(w)
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if fm.horizontalAdvance(w[:mid]) <= max_width:
                            lo = mid
                        else:
                            hi = mid - 1
                    lines.append(w[:lo])
                    w = w[lo:]
                cur = w
        if cur:
            lines.append(cur)
    return lines


# ---------- palette ----------
# (primary, accent, text)
PALETTES = {
    "hidden":    (QColor(60,  100, 160), QColor(0,   180, 255), QColor(140, 200, 230)),
    "listening": (QColor(0,   180, 255), QColor(130, 235, 255), QColor(220, 240, 255)),
    # Violet for thinking — replaces the amber/orange.
    "thinking":  (QColor(140, 80,  255), QColor(195, 130, 255), QColor(225, 200, 255)),
    "speaking":  (QColor(60,  255, 180), QColor(140, 255, 220), QColor(210, 255, 235)),
}


# ---------- Jarvis HUD ----------
class JarvisHUD(QWidget):
    """Fullscreen JARVIS-style overlay (Iron Man HUD).

    Layout (scales with screen):
      - HEADER (top): JARVIS tag, telemetry, T+seconds, state
      - LEFT TOP PANEL:  TELEMETRY readouts
      - RIGHT TOP PANEL: SUBSYSTEMS status
      - CENTER: massive orb + 3 rotating rings + 64 audio bars + scan sweep
      - LEFT BOTTOM PANEL:  HISTORY
      - RIGHT BOTTOM PANEL: WIREFRAME globe (rotating)
      - FOOTER: status line + terminal input
      - FULL BG: dark + hex grid + occasional horizontal scan line
    """

    user_text_submitted = Signal(str)
    dismissed = Signal()  # emitted on ESC / set_state("hidden") — cancels TTS

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)
        # Fullscreen on the primary monitor
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        self._state = "hidden"
        self._status_text = ""
        self._transcript = ""
        self._reply_text = ""
        # Rolling history (most-recent first), kept short for the left panel
        self._history: list[tuple[str, str]] = []  # (user, reply)

        # Animation phases
        self._ring_outer = 0.0
        self._ring_mid = 0.0
        self._ring_inner = 0.0
        self._sphere_phase = 0.0
        self._scan_phase = -0.4
        self._globe_phase = 0.0
        self._hline_phase = 0.0
        self._bars = [0.05] * 96
        self._target_bars = [0.05] * 96
        self._tick_n = 0
        # --- Stark-mode extras ---
        # Floating particle field (initialized lazily after geometry known)
        self._particles: list[list[float]] = []   # [x, y, vx, vy, phase]
        self._n_particles = 70
        # Side EQ bars (left + right vertical spectrums)
        self._side_bars_l = [0.05] * 28
        self._side_bars_r = [0.05] * 28
        self._target_side_l = [0.05] * 28
        self._target_side_r = [0.05] * 28
        # Energy-line dash offset for orb → corner-panel beams
        self._beam_phase = 0.0
        # Scrolling pseudo-tech-text streams in panels
        self._stream_lines_left: list[str] = []
        self._stream_lines_right: list[str] = []
        self._stream_seed = random.Random(42)
        # Holographic flicker
        self._flicker = 1.0
        self._flicker_target = 1.0
        # Outer rim slider position
        self._rim_phase = 0.0

        # Live telemetry sources (attached by JoeyApp after construction)
        self._sysmon = None
        self._voice = None
        self._brain_info: callable = lambda: {}

        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick)

        # Text input — styled like a terminal. Position computed after geometry.
        self.input = QLineEdit(self)
        iw = min(720, self.width() - 200)
        ih = 42
        self.input.setGeometry(
            (self.width() - iw) // 2,
            self.height() - 110,
            iw,
            ih,
        )
        f = QFont("Monospace")
        f.setStyleHint(QFont.TypeWriter)
        f.setPointSize(13)
        self.input.setFont(f)
        self.input.setStyleSheet(
            "QLineEdit {"
            "  background: rgba(2, 20, 36, 220);"
            "  border: 1px solid rgba(0, 180, 255, 180);"
            "  border-radius: 4px;"
            "  color: #d8f0ff;"
            "  padding: 6px 14px;"
            "  selection-background-color: #005580;"
            "}"
        )
        self.input.setPlaceholderText(">>  awaiting query…")
        self.input.returnPressed.connect(self._on_submit)
        # Catch Escape on the input itself — grabKeyboard prevents the parent's
        # keyPressEvent from seeing key events while active.
        self.input.installEventFilter(self)
        self.input.hide()

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity")
        self._opacity_anim.setDuration(280)
        self.setWindowOpacity(0.0)
        # NB: window starts UNMAPPED. A fullscreen always-on-top transparent
        # window will swallow clicks even with WA_TransparentForMouseEvents
        # on some X11 compositors — so we genuinely hide it when idle.
        # X11 focus on re-show is handled via grabKeyboard() + _xlib_force_activate.

    # ---- public state api ----
    def set_state(self, state: str, status: str = "", show_input: bool = False) -> None:
        prev = self._state
        core.log(f"DEBUG set_state({state!r}, show_input={show_input}) prev={prev!r} "
                 f"opacity={self.windowOpacity():.2f} visible={self.isVisible()}")
        self._state = state
        self._status_text = status or self._default_status(state)
        if state == "hidden":
            self._fade_out()
            self._anim.stop()
            self.input.hide()
            self.dismissed.emit()
            return
        if prev == "hidden":
            self._reply_text = ""
        self._anim.start()
        self._fade_in()
        if show_input:
            self.input.show()
            self.input.clear()
            QTimer.singleShot(0,   self._reclaim_focus)
            QTimer.singleShot(80,  self._reclaim_focus)
            QTimer.singleShot(250, self._reclaim_focus)
        else:
            self.input.hide()
        self.update()

    def _reclaim_focus(self) -> None:
        app = QApplication.instance()
        core.log(f"DEBUG _reclaim_focus pre: visible={self.isVisible()} "
                 f"input.visible={self.input.isVisible()} "
                 f"input.focus={self.input.hasFocus()} "
                 f"focusWidget={app.focusWidget()!r} "
                 f"activeWin={app.activeWindow()!r}")
        if not self.isVisible() or not self.input.isVisible():
            return
        self.raise_()
        self.activateWindow()
        handle = self.windowHandle()
        if handle is not None:
            handle.requestActivate()
        self.input.setFocus(Qt.ActiveWindowFocusReason)
        self._xlib_force_activate()
        self.input.grabKeyboard()
        core.log(f"DEBUG _reclaim_focus post: input.focus={self.input.hasFocus()} "
                 f"focusWidget={app.focusWidget()!r}")

    def _xlib_force_activate(self) -> None:
        try:
            from Xlib import X, display
            from Xlib.protocol import event as xevent
        except Exception:
            return
        try:
            wid = int(self.winId())
            d = display.Display()
            atom_active = d.intern_atom("_NET_ACTIVE_WINDOW")
            root = d.screen().root
            win = d.create_resource_object("window", wid)
            data = (32, [2, X.CurrentTime, 0, 0, 0])  # source=2 (pager/script)
            ev = xevent.ClientMessage(
                window=win,
                client_type=atom_active,
                data=data,
            )
            root.send_event(
                ev,
                event_mask=(X.SubstructureNotifyMask | X.SubstructureRedirectMask),
            )
            # Belt-and-braces: also set the input focus directly.
            win.set_input_focus(X.RevertToParent, X.CurrentTime)
            d.flush()
            d.close()
        except Exception as e:
            core.log(f"xlib activate error: {e!r}")

    def set_transcript(self, text: str) -> None:
        self._transcript = text
        self.update()

    def set_reply(self, text: str) -> None:
        self._reply_text = text
        self.update()

    @staticmethod
    def _default_status(state: str) -> str:
        return {
            "listening": "// AWAITING INPUT",
            "thinking":  "// PROCESSING",
            "speaking":  "// RESPONDING",
        }.get(state, "")

    # ---- internal ----
    def _on_submit(self) -> None:
        txt = self.input.text().strip()
        if txt:
            self.user_text_submitted.emit(txt)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key_Escape:
            self.set_state("hidden")
            return
        super().keyPressEvent(ev)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.input and event.type() == QEvent.KeyPress:
            core.log(f"DEBUG input keyPress: key={event.key()} text={event.text()!r}")
            if event.key() == Qt.Key_Escape:
                self.set_state("hidden")
                return True
        return super().eventFilter(obj, event)

    def _fade_in(self) -> None:
        # Map + raise + activate. grabKeyboard() in _reclaim_focus handles the
        # X11 focus race so input still receives keys on subsequent re-shows.
        self.show()
        self.raise_()
        self.activateWindow()
        self._opacity_anim.stop()
        try:
            self._opacity_anim.finished.disconnect()
        except RuntimeError:
            pass
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(1.0)
        self._opacity_anim.start()

    def _fade_out(self) -> None:
        self._opacity_anim.stop()
        self._opacity_anim.setStartValue(self.windowOpacity())
        self._opacity_anim.setEndValue(0.0)
        try:
            self._opacity_anim.finished.disconnect()
        except RuntimeError:
            pass
        self._opacity_anim.finished.connect(self._on_fade_out_done)
        self._opacity_anim.start()

    def _on_fade_out_done(self) -> None:
        # Genuinely hide the window — never block clicks/scrolls on the
        # rest of the desktop. grabKeyboard is released here so other
        # apps can receive keyboard input again.
        self.input.releaseKeyboard()
        self.input.hide()
        self.input.clear()
        self.hide()

    def _tick(self) -> None:
        self._tick_n += 1
        speed = {"listening": 1.0, "thinking": 1.9, "speaking": 1.3}.get(self._state, 0.5)
        self._ring_outer += 0.011 * speed
        self._ring_mid   -= 0.019 * speed
        self._ring_inner += 0.030 * speed
        self._sphere_phase += 0.08
        self._scan_phase += 0.040 * speed
        if self._scan_phase > math.pi + 0.5:
            self._scan_phase = -0.5
        self._globe_phase += 0.018
        self._hline_phase += 0.013
        if self._hline_phase > 1.2:
            self._hline_phase = -0.2
        self._beam_phase += 0.6 * speed
        self._rim_phase += 0.006 * speed

        amp = {"listening": 0.85, "thinking": 0.55, "speaking": 1.0}.get(self._state, 0.15)
        for i in range(len(self._bars)):
            base = (math.sin(self._tick_n * 0.14 + i * 0.32) + 1) * 0.5
            self._target_bars[i] = base * amp * (0.6 + 0.4 * random.random())
            self._bars[i] = self._bars[i] * 0.72 + self._target_bars[i] * 0.28

        # Side spectrum bars: independent random walk, smoother (slower decay)
        amp_side = amp * 0.85
        for i in range(len(self._side_bars_l)):
            self._target_side_l[i] = (
                (math.sin(self._tick_n * 0.11 + i * 0.5) + 1) * 0.5
                * amp_side * (0.55 + 0.45 * random.random())
            )
            self._target_side_r[i] = (
                (math.sin(self._tick_n * 0.13 + i * 0.47 + 1.7) + 1) * 0.5
                * amp_side * (0.55 + 0.45 * random.random())
            )
            self._side_bars_l[i] = self._side_bars_l[i] * 0.78 + self._target_side_l[i] * 0.22
            self._side_bars_r[i] = self._side_bars_r[i] * 0.78 + self._target_side_r[i] * 0.22

        # Particle field — wrap on edges
        if not self._particles:
            self._init_particles()
        W, H = self.width(), self.height()
        for p in self._particles:
            p[0] += p[2]
            p[1] += p[3]
            p[4] += 0.04
            if p[0] < 0: p[0] += W
            elif p[0] > W: p[0] -= W
            if p[1] < 0: p[1] += H
            elif p[1] > H: p[1] -= H

        # Scrolling tech-text — occasionally append a new line to each panel stream
        if self._tick_n % 4 == 0:
            self._stream_lines_left.append(self._gen_tech_line())
            self._stream_lines_right.append(self._gen_tech_line())
            del self._stream_lines_left[:-12]
            del self._stream_lines_right[:-12]

        # Holographic flicker — drift toward target, occasionally jump
        if self._tick_n % 12 == 0:
            self._flicker_target = 0.92 + 0.08 * random.random()
        self._flicker = self._flicker * 0.9 + self._flicker_target * 0.1

        self.update()

    def _init_particles(self) -> None:
        W, H = self.width(), self.height()
        self._particles = []
        for _ in range(self._n_particles):
            self._particles.append([
                random.uniform(0, W),
                random.uniform(0, H),
                random.uniform(-0.45, 0.45),
                random.uniform(-0.35, 0.35),
                random.uniform(0, 6.28),
            ])

    def _gen_tech_line(self) -> str:
        r = self._stream_seed
        kind = r.randint(0, 5)
        if kind == 0:
            return f"0x{r.randint(0, 0xFFFFFFFF):08X}  OK"
        if kind == 1:
            return f"LAT {r.randint(0, 9999):04d}  LON {r.randint(0, 9999):04d}"
        if kind == 2:
            return f"PKT {r.randint(0, 999):03d}/{r.randint(1, 999):03d}  RX"
        if kind == 3:
            return f"ANGL {r.uniform(-180,180):+7.2f}°  RAD {r.uniform(0,1):.3f}"
        if kind == 4:
            return f"NET  {r.randint(1, 254):3d}.{r.randint(0,254):3d}.{r.randint(0,254):3d}.{r.randint(1,254):3d}"
        return f"PROC PID {r.randint(1000, 99999)}  Q={r.randint(0,9)}"

    def push_history(self, user: str, reply: str) -> None:
        self._history.insert(0, (user, reply))
        del self._history[6:]

    def attach_telemetry(self, *, sysmon, voice, brain_info) -> None:
        """Plug in live data sources after JoeyApp wires everything up."""
        self._sysmon = sysmon
        self._voice = voice
        self._brain_info = brain_info

    def _palette(self):
        return PALETTES.get(self._state, PALETTES["hidden"])

    # ============== painting ==============
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        primary, accent, text_color = self._palette()
        W, H = self.width(), self.height()

        # 1) DARK BACKDROP (full screen dimmer)
        p.fillRect(self.rect(), QColor(2, 6, 14, 220))

        # 2) BG layers: hex grid + horizontal scan + particles + CRT scanlines
        self._draw_hex_grid(p, accent)
        self._draw_particles(p, accent)
        self._draw_crt_scanlines(p, accent)
        self._draw_horizontal_scan(p, accent)

        # 3) HUD frame brackets + outer rim sliding indicators
        self._draw_screen_brackets(p, primary, W, H)
        self._draw_rim_indicators(p, primary, accent, W, H)

        # 4) Side spectrum bars (left + right vertical EQ)
        self._draw_side_spectrums(p, accent, W, H)

        # 5) Center: orb position
        cx, cy = W / 2, H / 2 - 30

        # 6) Energy beams (drawn BEFORE panels so beams appear to emerge
        #    from the orb and connect to corner panels)
        self._draw_energy_beams(p, cx, cy, accent, W, H)

        # 7) Corner panels (real-data versions)
        self._draw_panel_telemetry(p, primary, accent, text_color)
        self._draw_panel_subsystems(p, primary, accent, text_color)
        self._draw_panel_history(p, primary, accent, text_color)
        self._draw_panel_scope(p, primary, accent)

        # 8) Three INFORMATIONAL arcs around the orb:
        #    outer  = CPU history per second (60 segments)
        #    middle = Memory % (filled arc, 0..360°)
        #    inner  = Network in (upper arc) + out (lower arc)
        self._draw_arc_cpu(p, cx, cy, primary, accent)
        self._draw_arc_mem(p, cx, cy, primary, accent)
        self._draw_arc_net(p, cx, cy, primary, accent)

        # 9) Orb stack: tick marks + REAL audio bars + rings + sphere
        self._draw_tick_marks(p, cx, cy, primary, r_in=290, r_out=302, count=72)
        self._draw_audio_bars_real(p, cx, cy, accent, r_base=200, max_len=72)
        self._draw_inner_ring(p, cx, cy, accent, radius=140)
        self._draw_sphere(p, cx, cy, primary, accent, base_r=92)
        self._draw_scan(p, cx, cy, accent, radius=300)

        # 9) Header + footer text + response panel
        self._draw_header_bar(p, primary, accent, text_color, W)
        self._draw_status_big(p, cx, cy, text_color)
        self._draw_transcript_reply(p, text_color, W, H)
        p.end()

    # --- floating particle field (drifting glow dots) ---
    def _draw_particles(self, p: QPainter, accent: QColor) -> None:
        if not self._particles:
            return
        p.setPen(Qt.NoPen)
        for px, py, _vx, _vy, ph in self._particles:
            br = 0.5 + 0.5 * math.sin(ph)
            alpha = int(40 + 120 * br * self._flicker)
            r = 1.2 + 1.6 * br
            # subtle glow halo
            glow = QRadialGradient(QPointF(px, py), r * 3)
            glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), alpha))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(glow)
            p.drawEllipse(QPointF(px, py), r * 3, r * 3)
            # core dot
            p.setBrush(QColor(255, 255, 255, min(255, alpha + 60)))
            p.drawEllipse(QPointF(px, py), r, r)

    # --- subtle CRT-style scanlines overlay ---
    def _draw_crt_scanlines(self, p: QPainter, accent: QColor) -> None:
        col = QColor(accent.red(), accent.green(), accent.blue(), 10)
        p.setPen(QPen(col, 1))
        H = self.height()
        for y in range(0, H, 3):
            p.drawLine(0, y, self.width(), y)

    # --- 4 sliding "indicator dots" running around the outer screen edge ---
    def _draw_rim_indicators(self, p: QPainter, primary: QColor, accent: QColor,
                              W: int, H: int) -> None:
        # 8 dots, evenly spaced, all sliding clockwise around the rectangle's edge
        perim = 2 * (W + H)
        count = 8
        p.setPen(Qt.NoPen)
        for i in range(count):
            t = ((i / count) + (self._rim_phase % 1.0)) % 1.0
            d = t * perim
            # walk the rectangle perimeter (TL → TR → BR → BL → TL)
            if d < W:
                x, y = d, 0.0
            elif d < W + H:
                x, y = float(W), d - W
            elif d < 2 * W + H:
                x, y = float(W) - (d - W - H), float(H)
            else:
                x, y = 0.0, float(H) - (d - 2 * W - H)
            r_glow = 7
            glow = QRadialGradient(QPointF(x, y), r_glow * 2.5)
            glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 200))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(glow)
            p.drawEllipse(QPointF(x, y), r_glow * 2.5, r_glow * 2.5)
            p.setBrush(QColor(255, 255, 255, 220))
            p.drawEllipse(QPointF(x, y), 2.5, 2.5)

    # --- LEFT margin: live mic amplitude history (last ~28 frames @ 80ms)
    # --- RIGHT margin: rolling CPU% history (last ~28 seconds)
    def _draw_side_spectrums(self, p: QPainter, accent: QColor, W: int, H: int) -> None:
        bar_w = 4
        gap = 14
        n = 28
        total = n * (bar_w + gap) - gap
        y0 = (H - total) // 2
        max_h = 130

        # LEFT — REAL mic RMS, newest at top so it animates downward
        mic = list(self._voice.mic_rms_hist[-n:]) if self._voice else [0.0] * n
        mic += [0.0] * (n - len(mic))
        for i, h in enumerate(mic):
            bh = int(max_h * max(0.0, min(1.0, h)))
            yy = y0 + i * (bar_w + gap)
            alpha = int(120 + 130 * h)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), alpha))
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(28, yy, bh, bar_w))
        # legend
        self._mini_label(p, 28, y0 - 14, "MIC RMS", accent)

        # RIGHT — REAL CPU history (recent seconds)
        cpu = list(self._sysmon.cpu_hist[-n:]) if self._sysmon else [0.0] * n
        cpu += [0.0] * (n - len(cpu))
        for i, c in enumerate(cpu):
            bh = int(max_h * max(0.0, min(1.0, c / 100.0)))
            yy = y0 + i * (bar_w + gap)
            alpha = int(120 + 130 * (c / 100.0))
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), alpha))
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(W - 28 - bh, yy, bh, bar_w))
        self._mini_label(p, W - 28 - 56, y0 - 14, "CPU 60s", accent, right=True)

    def _mini_label(self, p, x, y, text, accent: QColor, *, right: bool = False) -> None:
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(7)
        p.setFont(f)
        p.setPen(QColor(accent.red(), accent.green(), accent.blue(), 200))
        if right:
            fm = p.fontMetrics()
            x = x + 56 - fm.horizontalAdvance(text)
        p.drawText(int(x), int(y), text)

    # --- 4 animated dashed beams from orb center to each corner-panel center ---
    def _draw_energy_beams(self, p: QPainter, cx: float, cy: float,
                            accent: QColor, W: int, H: int) -> None:
        # Skip rendering through the sphere itself — start at sphere edge
        sphere_r = 100
        endpoints = [
            (200, 190),                 # top-left panel center
            (W - 200, 190),              # top-right
            (200, H - 200),              # bottom-left
            (W - 200, H - 200),          # bottom-right
        ]
        for ex, ey in endpoints:
            dx, dy = ex - cx, ey - cy
            dist = math.hypot(dx, dy) or 1
            ux, uy = dx / dist, dy / dist
            sx, sy = cx + ux * sphere_r, cy + uy * sphere_r
            # Animated dashed line — flowing outward
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 160), 1.4)
            pen.setStyle(Qt.DashLine)
            pen.setDashPattern([4, 8])
            pen.setDashOffset(-self._beam_phase * 1.5)  # negative = flow outward
            p.setPen(pen)
            p.drawLine(QPointF(sx, sy), QPointF(ex, ey))
            # Pulse blob at the panel endpoint
            pulse = 0.6 + 0.4 * math.sin(self._beam_phase * 0.4)
            r_pulse = 5 * pulse
            glow = QRadialGradient(QPointF(ex, ey), r_pulse * 3)
            glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 200))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(glow)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(ex, ey), r_pulse * 3, r_pulse * 3)

    # --- subtle hex grid covering full screen ---
    def _draw_hex_grid(self, p: QPainter, accent: QColor) -> None:
        col = QColor(accent.red(), accent.green(), accent.blue(), 14)
        pen = QPen(col, 1)
        p.setPen(pen)
        size = 36  # hex radius
        h = size * math.sqrt(3)
        W, H = self.width(), self.height()
        rows = int(H / h) + 2
        cols = int(W / (1.5 * size)) + 2
        for r in range(rows):
            for c in range(cols):
                cx_h = c * 1.5 * size
                cy_h = r * h + (h / 2 if c % 2 else 0)
                pts = []
                for k in range(6):
                    a = math.pi / 3 * k
                    pts.append(QPointF(cx_h + size * math.cos(a),
                                       cy_h + size * math.sin(a)))
                # draw only 3 alternating sides (lighter pattern)
                for i in (0, 2, 4):
                    p.drawLine(pts[i], pts[(i + 1) % 6])

    # --- horizontal scan line that drifts down the screen ---
    def _draw_horizontal_scan(self, p: QPainter, accent: QColor) -> None:
        if self._hline_phase < 0 or self._hline_phase > 1.0:
            return
        H = self.height()
        y = self._hline_phase * H
        grad = QRadialGradient(self.width() / 2, y, self.width() / 2)
        c = QColor(accent.red(), accent.green(), accent.blue(), 32)
        grad.setColorAt(0.0, c)
        grad.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawRect(QRectF(0, y - 1, self.width(), 2))

    # --- 4 large L-brackets at screen corners ---
    def _draw_screen_brackets(self, p: QPainter, color: QColor, W: int, H: int) -> None:
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        m = 30
        L = 60
        for x, y, dx, dy in [
            (m, m, 1, 1),
            (W - m, m, -1, 1),
            (m, H - m, 1, -1),
            (W - m, H - m, -1, -1),
        ]:
            p.drawLine(int(x), int(y), int(x + dx * L), int(y))
            p.drawLine(int(x), int(y), int(x), int(y + dy * L))

    # --- LEFT TOP: REAL system telemetry (psutil) ---
    def _draw_panel_telemetry(self, p: QPainter, primary, accent, text) -> None:
        x, y, w, h = 60, 100, 280, 180
        self._panel_frame(p, x, y, w, h, primary, "TELEMETRY")
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(9)
        p.setFont(f)
        p.setPen(text)
        sm = self._sysmon
        rows = []
        if sm:
            cpu = sm.cpu_hist[-1] if sm.cpu_hist else 0.0
            rows.append(f"CPU       {cpu:6.1f}%")
            rows.append(f"MEM       {sm.mem_pct:6.1f}%")
            rows.append(f"NET ↓     {sm.net_in_kbps:8.1f} KB/s")
            rows.append(f"NET ↑     {sm.net_out_kbps:8.1f} KB/s")
            rows.append(f"DISK r/w  {sm.disk_read_kbps:.0f}/{sm.disk_write_kbps:.0f}")
            if sm.temp_c is not None:
                rows.append(f"TEMP      {sm.temp_c:5.1f} °C")
            if sm.batt_pct is not None:
                chg = "⚡" if sm.batt_charging else "  "
                rows.append(f"BATT  {chg} {sm.batt_pct:5.1f}%")
            rows.append(f"PROCS     {sm.process_count:>5d}")
        else:
            rows = ["(sysmon offline)"]
        for i, r in enumerate(rows[:7]):
            p.drawText(x + 16, y + 50 + i * 16, r)
        # CPU mini-bar at the panel's bottom (matches the leftmost row label)
        cpu_now = (sm.cpu_hist[-1] / 100.0) if (sm and sm.cpu_hist) else 0.0
        bar_x = x + 16; bar_y = y + h - 22; bar_w = w - 32; bar_h = 8
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 80), 1))
        p.drawRect(bar_x, bar_y, bar_w, bar_h)
        fill = int(bar_w * min(cpu_now, 1.0))
        p.fillRect(bar_x, bar_y, fill, bar_h,
                   QColor(accent.red(), accent.green(), accent.blue(), 180))

    # --- RIGHT TOP: REAL Joey state ---
    def _draw_panel_subsystems(self, p: QPainter, primary, accent, text) -> None:
        W = self.width()
        w, h = 280, 180
        x, y = W - w - 60, 100
        self._panel_frame(p, x, y, w, h, primary, "SUBSYSTEMS")
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(9)
        p.setFont(f)

        bi = self._brain_info() if self._brain_info else {}
        v = self._voice
        last_score = v.last_wake_score if v else 0.0
        peak = v.mic_peak if v else 0.0
        latency = bi.get("last_latency_s", 0.0)
        turns = bi.get("turns", 0)
        brain = bi.get("name", "?")

        rows = [
            ("BRAIN",   brain.upper(),                              True),
            ("LATENCY", f"{latency*1000:>4.0f} ms" if latency else "—", latency > 0),
            ("TURNS",   f"{turns}",                                  True),
            ("WAKE",    f"{last_score:.2f} / {core.WAKE_THRESHOLD:.2f}", last_score >= 0.0),
            ("MIC PK",  f"{peak*100:5.1f}%",                         True),
            ("STT",     "WHISPER base.en",                           True),
            ("TTS",     "PIPER amy-medium",                          True),
        ]
        for i, (k, vstr, ok) in enumerate(rows):
            yy = y + 50 + i * 16
            p.setPen(text)
            p.drawText(x + 16, yy, f"{k:<8s}")
            ok_col = QColor(80, 255, 160, 230) if ok else QColor(255, 100, 100, 200)
            p.setPen(QColor(220, 240, 255, 230))
            p.drawText(x + 100, yy, vstr)
            p.setBrush(ok_col)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x + w - 18, yy - 4), 3, 3)

    # --- LEFT BOTTOM: history panel ---
    def _draw_panel_history(self, p: QPainter, primary, accent, text) -> None:
        H = self.height()
        x, y, w, h = 60, H - 280, 280, 160
        self._panel_frame(p, x, y, w, h, primary, "HISTORY")
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(8)
        p.setFont(f)
        if not self._history:
            p.setPen(QColor(text.red(), text.green(), text.blue(), 110))
            p.drawText(x + 16, y + 64, "(no prior queries)")
            return
        for i, (u, r) in enumerate(self._history[:4]):
            yy = y + 56 + i * 26
            p.setPen(QColor(text.red(), text.green(), text.blue(), 200))
            p.drawText(x + 16, yy, f"> {u[:32]}")
            p.setPen(QColor(text.red(), text.green(), text.blue(), 130))
            p.drawText(x + 16, yy + 11, f"  {r[:34]}")

    # --- RIGHT BOTTOM: real-time mic waveform scope ---
    def _draw_panel_scope(self, p: QPainter, primary, accent) -> None:
        W = self.width(); H = self.height()
        w, h = 280, 160
        x, y = W - w - 60, H - 280
        self._panel_frame(p, x, y, w, h, primary, "MIC // SCOPE")
        # baseline
        baseline = y + h / 2 + 8
        # axis line
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 80), 1))
        p.drawLine(int(x + 14), int(baseline), int(x + w - 14), int(baseline))
        # waveform from voice.mic_rms_hist — 60 most recent
        mic = list(self._voice.mic_rms_hist[-60:]) if self._voice else []
        if len(mic) < 2:
            return
        path_w = w - 28
        amp_h = (h - 50) / 2
        pen = QPen(accent, 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        prev_x = x + 14
        prev_y = baseline
        for i, m in enumerate(mic):
            xx = x + 14 + (i / max(1, len(mic) - 1)) * path_w
            yy = baseline - m * amp_h
            p.drawLine(QPointF(prev_x, prev_y), QPointF(xx, yy))
            prev_x, prev_y = xx, yy
        # numeric current level top-right
        cur = mic[-1] if mic else 0.0
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(8)
        p.setFont(f)
        p.setPen(accent)
        p.drawText(x + w - 80, y + h - 12, f"RMS {cur*100:5.1f}%")

    # --- RIGHT BOTTOM (legacy): wireframe globe (kept but unused; can revive) ---
    def _draw_panel_globe(self, p: QPainter, primary, accent) -> None:
        W = self.width(); H = self.height()
        w, h = 280, 160
        x, y = W - w - 60, H - 280
        self._panel_frame(p, x, y, w, h, primary, "GEO // WIREFRAME")
        gx, gy = x + w / 2, y + h / 2 + 6
        r = 56
        # Wireframe sphere: latitude rings (rotating tilt) + longitude meridians
        col = QColor(accent.red(), accent.green(), accent.blue(), 200)
        pen = QPen(col, 1)
        p.setPen(pen)
        rot = self._globe_phase
        # latitude ellipses (varying minor axis based on rot)
        for lat in (-0.6, -0.3, 0.0, 0.3, 0.6):
            yy = gy + lat * r
            major = r * math.sqrt(max(0.0001, 1 - lat * lat))
            minor = major * abs(math.sin(rot + lat))
            p.drawEllipse(QPointF(gx, yy), major, minor)
        # longitude meridians (vertical ellipses, rotated)
        for k in range(6):
            ang = rot + k * (math.pi / 6)
            minor = r * abs(math.cos(ang))
            p.drawEllipse(QPointF(gx, gy), minor, r)
        # central dot
        p.setBrush(col)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(gx, gy), 2.5, 2.5)

    # --- Faint scrolling tech-text inside a panel (background flavor) ---
    def _draw_panel_stream(self, p: QPainter, x: int, y: int, w: int, h: int,
                            lines: list[str], accent: QColor) -> None:
        if not lines:
            return
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(7)
        p.setFont(f)
        col = QColor(accent.red(), accent.green(), accent.blue(), 38)
        p.setPen(col)
        # right-justified column on the panel's right edge
        right = x + w - 14
        bottom = y + h - 10
        for i, line in enumerate(reversed(lines)):
            yy = bottom - i * 11
            if yy < y + 36:
                break
            fm = p.fontMetrics()
            p.drawText(right - fm.horizontalAdvance(line), yy, line)

    # --- Panel frame helper: thin rect + title bar ---
    def _panel_frame(self, p: QPainter, x: float, y: float, w: float, h: float,
                     primary: QColor, title: str) -> None:
        rect = QRectF(x, y, w, h)
        # bg fill (slightly opaque)
        p.fillRect(rect, QColor(4, 12, 22, 160))
        # border
        pen = QPen(QColor(primary.red(), primary.green(), primary.blue(), 200), 1)
        p.setPen(pen)
        p.drawRect(rect)
        # title bar
        title_h = 28
        p.fillRect(QRectF(x, y, w, title_h),
                   QColor(primary.red(), primary.green(), primary.blue(), 50))
        pen = QPen(primary, 1)
        p.setPen(pen)
        p.drawLine(int(x), int(y + title_h), int(x + w), int(y + title_h))
        # title text
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(9)
        f.setBold(True); p.setFont(f)
        p.setPen(QColor(220, 240, 255))
        p.drawText(int(x + 12), int(y + 19), title)
        # corner notches (top-right L)
        nx, ny = x + w, y
        p.drawLine(int(nx - 10), int(ny), int(nx), int(ny + 10))

    # --- outer arc ring (scaled) ---
    def _draw_outer_arc_ring(self, p, cx, cy, primary, accent, radius=320) -> None:
        rot = math.degrees(self._ring_outer)
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        for off in (0, 90, 180, 270):
            start = (rot + off) * 16
            span = 72 * 16
            p.setPen(QPen(primary, 2))
            p.drawArc(rect, int(start), int(span))
            r2 = radius - 9
            rect2 = QRectF(cx - r2, cy - r2, 2 * r2, 2 * r2)
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 90), 1))
            p.drawArc(rect2, int(start), int(span))

    # --- tick marks (scaled) ---
    def _draw_tick_marks(self, p, cx, cy, color, r_in=158, r_out=165, count=60) -> None:
        for i in range(count):
            ang = i * (math.pi * 2 / count) + self._ring_outer * 0.3
            x1 = cx + math.cos(ang) * r_in
            y1 = cy + math.sin(ang) * r_in
            x2 = cx + math.cos(ang) * r_out
            y2 = cy + math.sin(ang) * r_out
            major = (i % 6 == 0)
            p.setPen(QPen(
                QColor(color.red(), color.green(), color.blue(), 180 if major else 70),
                1.8 if major else 1.0,
            ))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    # --- audio bars driven by REAL mic amplitude history (newest at top) ---
    def _draw_audio_bars_real(self, p, cx, cy, accent, r_base=200, max_len=72) -> None:
        n = 96
        mic = list(self._voice.mic_rms_hist[-n:]) if self._voice else []
        # Pad/repeat so the circle is full
        while len(mic) < n:
            mic.insert(0, 0.0)
        # Add a small idle baseline so bars don't disappear entirely
        baseline = {"listening": 0.10, "speaking": 0.15}.get(self._state, 0.06)
        for i, h in enumerate(mic):
            h = max(baseline, min(1.0, h))
            ang = i * (math.pi * 2 / n) - math.pi / 2  # start at top
            r1 = r_base
            r2 = r_base + max_len * h
            x1 = cx + math.cos(ang) * r1
            y1 = cy + math.sin(ang) * r1
            x2 = cx + math.cos(ang) * r2
            y2 = cy + math.sin(ang) * r2
            alpha = int(110 + 140 * h)
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha), 2.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    # --- OUTER ARC: per-second CPU history (60 segments around the orb) ---
    def _draw_arc_cpu(self, p, cx, cy, primary: QColor, accent: QColor) -> None:
        radius = 320
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        # Background full ring (faint)
        p.setPen(QPen(QColor(primary.red(), primary.green(), primary.blue(), 60), 2))
        p.drawArc(rect, 0, 360 * 16)
        # 60 segments — one per second of CPU history
        cpu = list(self._sysmon.cpu_hist[-60:]) if self._sysmon else [0.0] * 60
        cpu = [0.0] * (60 - len(cpu)) + cpu
        seg_deg = 360.0 / 60
        for i, c in enumerate(cpu):
            t = c / 100.0
            if t < 0.02:
                continue
            # rotate so newest is at top (12 o'clock)
            start_deg = 90 - (i + 1) * seg_deg
            alpha = int(140 + 110 * min(1.0, t))
            # height of segment grows with CPU%
            seg_r_outer = radius + int(14 * min(1.0, t))
            seg_rect = QRectF(cx - seg_r_outer, cy - seg_r_outer,
                              2 * seg_r_outer, 2 * seg_r_outer)
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha), 4))
            p.drawArc(seg_rect, int(start_deg * 16), int((seg_deg - 1.5) * 16))
        # Current CPU value text top-right of the ring
        self._arc_label(p, cx + radius - 70, cy - radius + 6,
                        f"CPU {cpu[-1]:5.1f}%", accent)

    # --- MIDDLE ARC: memory % as a single filled arc ---
    def _draw_arc_mem(self, p, cx, cy, primary: QColor, accent: QColor) -> None:
        radius = 250
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        # background ring
        p.setPen(QPen(QColor(primary.red(), primary.green(), primary.blue(), 80), 2))
        p.drawArc(rect, 0, 360 * 16)
        pct = self._sysmon.mem_pct if self._sysmon else 0.0
        span_deg = int(360 * pct / 100.0)
        # 12 o'clock start, sweep clockwise
        pen = QPen(accent, 4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, 90 * 16, -span_deg * 16)
        self._arc_label(p, cx - radius + 12, cy - radius + 6,
                        f"MEM {pct:5.1f}%", accent)

    # --- INNER ARC: network in/out as opposing half-arcs ---
    def _draw_arc_net(self, p, cx, cy, primary: QColor, accent: QColor) -> None:
        radius = 200
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        p.setPen(QPen(QColor(primary.red(), primary.green(), primary.blue(), 60), 1.5))
        p.drawArc(rect, 0, 360 * 16)
        if not self._sysmon:
            return
        net_in = self._sysmon.net_in_kbps
        net_out = self._sysmon.net_out_kbps
        # Map to angular span — log-scale, cap at ~2MB/s = full span
        def to_span(kbps: float) -> int:
            x = min(1.0, math.log10(max(1.0, kbps)) / 3.3)  # 0..1
            return int(180 * x)
        in_span = to_span(net_in)
        out_span = to_span(net_out)
        # Upper half = down/in (cyan-ish bright)
        pen_in = QPen(accent, 3)
        p.setPen(pen_in)
        p.drawArc(rect, (180 - in_span // 2) * 16, in_span * 16)
        # Lower half = up/out (slightly dim)
        pen_out = QPen(QColor(accent.red(), accent.green(), accent.blue(), 180), 3)
        p.setPen(pen_out)
        p.drawArc(rect, (360 - out_span // 2) * 16, out_span * 16)
        # Labels
        self._arc_label(p, cx - radius - 4, cy + 2, f"↓ {net_in:6.1f} KB/s", accent, right=True)
        self._arc_label(p, cx + radius + 8, cy + 2, f"↑ {net_out:6.1f} KB/s", accent)

    def _arc_label(self, p, x, y, text, accent, *, right: bool = False) -> None:
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(accent.red(), accent.green(), accent.blue(), 220))
        if right:
            fm = p.fontMetrics()
            x -= fm.horizontalAdvance(text)
        p.drawText(int(x), int(y), text)

    # --- mid ring (scaled, dashed) ---
    def _draw_mid_ring(self, p, cx, cy, accent, radius=180) -> None:
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), 180), 1.5)
        pen.setStyle(Qt.DashLine)
        pen.setDashPattern([3, 4])
        pen.setDashOffset(self._ring_mid * 60)
        p.setPen(pen)
        p.drawEllipse(rect)

    # --- inner ring (scaled, notched arcs) ---
    def _draw_inner_ring(self, p, cx, cy, accent, radius=140) -> None:
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        rot = math.degrees(self._ring_inner)
        for off in (0, 120, 240):
            start = (rot + off) * 16
            span = 60 * 16
            p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 230), 2))
            p.drawArc(rect, int(start), int(span))

    # --- central sphere with inner pulsing core + reactor lines ---
    def _draw_sphere(self, p, cx, cy, primary, accent, base_r=92) -> None:
        pulse = 1.0 + 0.10 * math.sin(self._sphere_phase)
        r = base_r * pulse
        flick = self._flicker
        # outer halo (4-layer bloom)
        for i, alpha in enumerate((30, 22, 14, 8)):
            rr = r + 10 + i * 14
            halo = QRadialGradient(QPointF(cx, cy), rr)
            a = int(alpha * flick)
            halo.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), a))
            halo.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(halo); p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(cx, cy), rr, rr)
        # sphere body
        body = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.3), r * 1.7)
        body.setColorAt(0.0, QColor(255, 255, 255, 240))
        body.setColorAt(0.22, accent)
        body.setColorAt(1.0, primary.darker(220))
        p.setBrush(body)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 220), 1.5))
        p.drawEllipse(QPointF(cx, cy), r, r)
        # reactor-style spokes (8 radial lines inside the sphere)
        spoke_phase = self._sphere_phase * 0.7
        for k in range(8):
            ang = k * math.pi / 4 + spoke_phase
            inner_r = r * 0.25
            outer_r = r * 0.85
            x1 = cx + math.cos(ang) * inner_r
            y1 = cy + math.sin(ang) * inner_r
            x2 = cx + math.cos(ang) * outer_r
            y2 = cy + math.sin(ang) * outer_r
            p.setPen(QPen(QColor(255, 255, 255, 90), 1))
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        # inner pulsing core
        core_r = r * (0.22 + 0.06 * math.sin(self._sphere_phase * 2))
        core = QRadialGradient(QPointF(cx, cy), core_r * 1.4)
        core.setColorAt(0.0, QColor(255, 255, 255, 240))
        core.setColorAt(0.6, QColor(accent.red(), accent.green(), accent.blue(), 200))
        core.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.setBrush(core); p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), core_r * 1.4, core_r * 1.4)
        # specular highlight
        hl = QRadialGradient(QPointF(cx - r * 0.4, cy - r * 0.4), r * 0.7)
        hl.setColorAt(0.0, QColor(255, 255, 255, 140))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(hl); p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx - r * 0.25, cy - r * 0.25), r * 0.75, r * 0.75)

    # --- radar sweep cone (scaled radius) ---
    def _draw_scan(self, p, cx, cy, accent, radius=300) -> None:
        if self._scan_phase < 0 or self._scan_phase > math.pi:
            return
        ang = self._scan_phase - math.pi / 2
        cone_deg = 26
        path = QPainterPath()
        path.moveTo(cx, cy)
        rect = QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius)
        path.arcTo(rect, -math.degrees(ang) - cone_deg / 2, cone_deg)
        path.closeSubpath()
        grad = QConicalGradient(QPointF(cx, cy), -math.degrees(ang))
        grad.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 70))
        grad.setColorAt(0.06, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.setBrush(grad); p.setPen(Qt.NoPen)
        p.drawPath(path)

    # --- HEADER: top tag, telemetry, T+secs, state ---
    def _draw_header_bar(self, p, primary, accent, text, W) -> None:
        # subtle bar background
        p.fillRect(QRectF(0, 0, W, 56), QColor(0, 12, 22, 130))
        pen = QPen(QColor(primary.red(), primary.green(), primary.blue(), 200), 1)
        p.setPen(pen)
        p.drawLine(int(0), int(56), int(W), int(56))

        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter)
        f.setPointSize(11); f.setBold(True)
        p.setFont(f); p.setPen(text)
        wake = (core.WAKE_WORD or "joey").upper()
        p.drawText(96, 36, f"J A R V I S      //   {wake}      //   ECHELON 7.21")

        f2 = QFont("Monospace"); f2.setStyleHint(QFont.TypeWriter); f2.setPointSize(10)
        p.setFont(f2)
        fm = p.fontMetrics()
        right = f"T+{self._tick_n // 30:>04d}s   STATE //  {self._state.upper():<10s}   "
        p.drawText(W - fm.horizontalAdvance(right) - 96, 36, right)

    # --- BIG status near sphere bottom ---
    def _draw_status_big(self, p, cx, cy, text) -> None:
        if self.input.isVisible():
            return
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter)
        f.setPointSize(15); f.setBold(True)
        p.setFont(f); p.setPen(text)
        fm = p.fontMetrics()
        s = self._status_text
        w = fm.horizontalAdvance(s)
        p.drawText(int(cx - w / 2), int(cy + 380), s)

    # --- Transcript + reply: dedicated big panel overlaying the bottom area ---
    def _draw_transcript_reply(self, p, color, W, H) -> None:
        if not (self._transcript or self._reply_text):
            return
        # Don't render the response panel while the user is mid-typing
        if self.input.isVisible() and not self._reply_text:
            return

        # Panel geometry — large, central, above the input. Overlays the
        # bottom side panels (history, globe) when there's content to show.
        margin_x = 100
        box_x = margin_x
        box_w = W - 2 * margin_x
        box_y_top = H - 460
        box_y_bot = H - 130
        box_h = box_y_bot - box_y_top

        # Translucent dark backdrop so text is readable over whatever is behind
        rect = QRectF(box_x, box_y_top, box_w, box_h)
        p.fillRect(rect, QColor(2, 10, 20, 235))
        p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 160), 1))
        p.drawRect(rect)
        # title strip
        title_h = 28
        p.fillRect(QRectF(box_x, box_y_top, box_w, title_h),
                   QColor(color.red(), color.green(), color.blue(), 50))
        p.setPen(QPen(color, 1))
        p.drawLine(int(box_x), int(box_y_top + title_h),
                   int(box_x + box_w), int(box_y_top + title_h))
        # title text
        ft = QFont("Monospace"); ft.setStyleHint(QFont.TypeWriter); ft.setPointSize(9)
        ft.setBold(True); p.setFont(ft)
        p.setPen(QColor(220, 240, 255))
        p.drawText(int(box_x + 14), int(box_y_top + 19), "RESPONSE //")

        # Body font — bigger and roomier for readability
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(12)
        p.setFont(f)
        fm = p.fontMetrics()
        line_h = fm.height() + 2

        inner_x = box_x + 18
        inner_w = box_w - 36
        y = box_y_top + title_h + 8 + fm.ascent()

        # Transcript first (in dimmer tone)
        if self._transcript:
            p.setPen(QColor(color.red(), color.green(), color.blue(), 200))
            t_lines = _wrap_text(f"> {self._transcript}", fm, inner_w)
            for tl in t_lines[:2]:
                p.drawText(inner_x, y, tl)
                y += line_h
            y += 6

        # Reply body
        if self._reply_text:
            p.setPen(color)
            lines = _wrap_text(self._reply_text, fm, inner_w)
            avail_h = (box_y_bot - 14) - y
            max_lines = max(1, avail_h // line_h)
            truncated = len(lines) > max_lines
            if truncated:
                lines = lines[:max_lines]
            for line in lines:
                p.drawText(inner_x, y, line)
                y += line_h
            if truncated:
                p.setPen(QColor(color.red(), color.green(), color.blue(), 160))
                p.drawText(inner_x, y, "  … (truncated — full text logged to console)")


# ---------- trigger socket ----------
class TriggerSocket(QObject):
    triggered = Signal()

    def __init__(self, path: Path) -> None:
        super().__init__()
        if path.exists():
            path.unlink()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.server.setblocking(False)
        os.chmod(path, 0o600)
        self.notifier = QSocketNotifier(self.server.fileno(), QSocketNotifier.Read, self)
        self.notifier.activated.connect(self._accept)

    def _accept(self) -> None:
        try:
            conn, _ = self.server.accept()
            conn.close()
        except BlockingIOError:
            return
        core.log("DEBUG trigger socket: received connection -> emit triggered")
        self.triggered.emit()


# ---------- real system telemetry (psutil + battery) ----------
class SystemMonitor(QThread):
    """Samples real OS metrics at 1Hz and keeps short rolling histories.
    The HUD reads from this for the informational arcs."""

    HIST_LEN = 60  # 60 seconds of history per metric

    def __init__(self) -> None:
        super().__init__()
        self._stop = False
        import psutil  # noqa: F401 — confirmed at import time
        self.cpu_hist: list[float] = [0.0] * self.HIST_LEN
        self.mem_pct: float = 0.0
        self.net_in_kbps: float = 0.0
        self.net_out_kbps: float = 0.0
        self.disk_read_kbps: float = 0.0
        self.disk_write_kbps: float = 0.0
        self.temp_c: float | None = None
        self.batt_pct: float | None = None
        self.batt_charging: bool = False
        self.process_count: int = 0

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        import psutil
        prev_net = psutil.net_io_counters()
        prev_disk = psutil.disk_io_counters()
        prev_t = time.time()
        while not self._stop:
            cpu = psutil.cpu_percent(interval=1)  # blocks 1s — natural pacing
            now = time.time()
            dt = max(1e-3, now - prev_t)
            self.cpu_hist.append(cpu)
            del self.cpu_hist[: max(0, len(self.cpu_hist) - self.HIST_LEN)]
            self.mem_pct = psutil.virtual_memory().percent

            net = psutil.net_io_counters()
            self.net_in_kbps = (net.bytes_recv - prev_net.bytes_recv) / dt / 1024
            self.net_out_kbps = (net.bytes_sent - prev_net.bytes_sent) / dt / 1024
            prev_net = net

            disk = psutil.disk_io_counters()
            if disk and prev_disk:
                self.disk_read_kbps = (disk.read_bytes - prev_disk.read_bytes) / dt / 1024
                self.disk_write_kbps = (disk.write_bytes - prev_disk.write_bytes) / dt / 1024
            prev_disk = disk

            try:
                temps = psutil.sensors_temperatures()
                pkg = (temps.get("coretemp") or temps.get("k10temp") or [])
                self.temp_c = pkg[0].current if pkg else None
            except Exception:
                self.temp_c = None
            try:
                b = psutil.sensors_battery()
                if b is not None:
                    self.batt_pct = b.percent
                    self.batt_charging = bool(b.power_plugged)
            except Exception:
                pass
            self.process_count = len(psutil.pids())
            prev_t = now


# ---------- voice worker thread ----------
class VoiceWorker(QThread):
    """Background loop: wake-word → record → Whisper STT → emit transcript.
    Pauses during brain/TTS phase via a cycle event signaled by JoeyApp.
    """

    wake_detected = Signal()
    transcript_ready = Signal(str)
    no_speech = Signal()
    voice_error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._stop = False
        self._cycle_done = threading.Event()
        self._cycle_done.set()  # idle by default
        # Lazy init in run() — model loads take 1-3s.
        self._wake = None
        self._recorder = None
        self._whisper = None
        # Live audio level history (rolling) + last wake score — for the HUD
        self.mic_rms_hist: list[float] = [0.0] * 60
        self.last_wake_score: float = 0.0
        self.mic_peak: float = 0.0

    def signal_cycle_done(self) -> None:
        """Called from main thread when brain/TTS phase finished — voice may listen again."""
        self._cycle_done.set()

    def stop(self) -> None:
        self._stop = True
        self._cycle_done.set()

    def run(self) -> None:
        try:
            core.log("voice: loading wake word + whisper...")
            self._wake = core.WakeWord()
            self._recorder = core.Recorder()
            self._whisper = core.Whisper()
            core.log(f"voice: listening for '{core.WAKE_WORD}'")
        except Exception as e:
            self.voice_error.emit(f"init: {e!r}")
            return

        while not self._stop:
            # ──────────────────────────────────────────────────
            #  Phase A: wait for the wake word "hey jarvis"
            # ──────────────────────────────────────────────────
            try:
                woken = self._listen_for_wake()
            except Exception as e:
                self.voice_error.emit(f"wake: {e!r}")
                time.sleep(1)
                continue
            if not woken or self._stop:
                continue

            # ──────────────────────────────────────────────────
            #  Phase B: conversation loop — once awake, accept
            #  follow-up turns WITHOUT re-waking until the user
            #  goes silent (no speech detected → fall back to
            #  wake-word listening).
            # ──────────────────────────────────────────────────
            core.log("voice: conversation OPEN — follow-ups accepted")
            while not self._stop:
                self.wake_detected.emit()
                self._cycle_done.clear()

                # Grace pause so any residual speaker echo / room reverb dies
                # before the recorder starts. Critical to prevent feedback
                # (Joey hearing its own TTS as user speech).
                time.sleep(1.5)

                try:
                    audio = self._recorder.record()
                except Exception as e:
                    self.voice_error.emit(f"record: {e!r}")
                    self._cycle_done.set()
                    break  # → back to wake-word listening

                if audio.size == 0:
                    # No speech captured in this turn → conversation ends.
                    self.no_speech.emit()
                    self._cycle_done.set()
                    core.log("voice: conversation CLOSED (silence) — wake-word mode")
                    break

                try:
                    text = self._whisper.transcribe(audio)
                except Exception as e:
                    self.voice_error.emit(f"whisper: {e!r}")
                    self._cycle_done.set()
                    break

                if not text:
                    self.no_speech.emit()
                    self._cycle_done.set()
                    core.log("voice: conversation CLOSED (empty STT) — wake-word mode")
                    break

                self.transcript_ready.emit(text)
                # Block until JoeyApp signals brain + TTS done. Without this
                # the next record() would start while Piper is still playing,
                # bleeding TTS audio into the user's "next turn".
                self._cycle_done.wait()
                # Loop back — start recording the follow-up turn.

    def _listen_for_wake(self) -> bool:
        chunk = 1280  # 80ms @ 16kHz — openWakeWord's preferred frame
        streak = 0
        with sd.RawInputStream(
            samplerate=16000, blocksize=chunk, dtype="int16", channels=1
        ) as stream:
            while not self._stop:
                data, _ = stream.read(chunk)
                pcm = np.frombuffer(bytes(data), dtype=np.int16)
                # Live audio level — normalized RMS [0..1] over this 80ms frame
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) / 32768.0
                self.mic_rms_hist.append(min(1.0, rms * 4.0))  # ×4 makes voice
                del self.mic_rms_hist[: max(0, len(self.mic_rms_hist) - 60)]
                self.mic_peak = max(self.mic_peak * 0.92, float(np.abs(pcm).max()) / 32768.0)

                preds = self._wake.model.predict(pcm)
                top_score = 0.0
                top_name = ""
                for name, score in preds.items():
                    if score > top_score:
                        top_score, top_name = score, name
                self.last_wake_score = float(top_score)
                if top_score >= core.WAKE_THRESHOLD:
                    streak += 1
                    if streak >= core.WAKE_CONSECUTIVE_FRAMES:
                        core.log(f"voice: WAKE  {top_name}={top_score:.3f}  "
                                 f"(threshold={core.WAKE_THRESHOLD}, "
                                 f"streak={streak})")
                        self._wake.model.reset()
                        return True
                else:
                    streak = 0
        return False


# ---------- brain worker thread ----------
class BrainWorker(QThread):
    state = Signal(str, str)
    transcript = Signal(str)
    reply = Signal(str)
    finished_speaking = Signal()

    def __init__(self, brain, piper: core.Piper) -> None:
        super().__init__()
        self.brain = brain
        self.piper = piper
        self._prompt: str | None = None
        self._speak: bool = True
        self._cancelled: bool = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False

    def submit(self, prompt: str, speak: bool = True) -> None:
        with self._lock:
            self._prompt = prompt
            self._speak = speak
            self._cancelled = False
        self._wake.set()

    def cancel(self) -> None:
        """Interrupt mid-cycle: stops TTS playback immediately. Synthesis
        already underway can't be aborted cleanly without killing the thread,
        but cutting the audio is what the user actually feels."""
        self._cancelled = True
        try:
            sd.stop()
        except Exception:
            pass
        # Make sure mic isn't left muted if we cancel mid-speech
        self._mute_default_source(False)

    @staticmethod
    def _mute_default_source(mute: bool) -> bool:
        """Mute/unmute the default mic source via pactl so Piper's TTS output
        doesn't echo back into the wake-word listener and trigger a feedback
        loop. Returns True on success (so callers know whether to unmute)."""
        import subprocess
        try:
            subprocess.run(
                ["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1" if mute else "0"],
                capture_output=True, timeout=2, check=False,
            )
            return True
        except Exception as e:
            core.log(f"mic mute toggle failed: {e!r}")
            return False

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        while not self._stop:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                prompt = self._prompt
                speak = self._speak
                self._prompt = None
            if not prompt:
                continue
            self.transcript.emit(prompt)
            self.state.emit("thinking", "// PROCESSING")

            full_raw = ""
            sentence_buf = ""
            spoke_anything = False
            _mic_was_muted = self._mute_default_source(True) if speak else False
            try:
                if hasattr(self.brain, "ask_stream"):
                    for chunk in self.brain.ask_stream(prompt):
                        if self._cancelled:
                            break
                        full_raw += chunk
                        sentence_buf += chunk
                        # Update HUD progressively so user sees text appearing
                        self.reply.emit(_strip_markdown(full_raw))
                        # Emit complete sentences to Piper as soon as we have them
                        if speak:
                            while True:
                                m = _SENT_END.search(sentence_buf)
                                if not m:
                                    break
                                sentence = sentence_buf[: m.end()].strip()
                                sentence_buf = sentence_buf[m.end():]
                                clean = _strip_markdown(sentence)
                                if clean and not self._cancelled:
                                    if not spoke_anything:
                                        self.state.emit("speaking", "// RESPONDING")
                                        spoke_anything = True
                                    try:
                                        self.piper.speak(clean)
                                    except Exception as e:
                                        core.log(f"piper error: {e!r}")
                    # trailing partial
                    if speak and sentence_buf.strip() and not self._cancelled:
                        clean = _strip_markdown(sentence_buf)
                        if clean:
                            if not spoke_anything:
                                self.state.emit("speaking", "// RESPONDING")
                            try:
                                self.piper.speak(clean)
                            except Exception as e:
                                core.log(f"piper error: {e!r}")
                else:
                    # Non-streaming fallback (Claude / hermes-cli)
                    full_raw = self.brain.ask(prompt)
                    clean = _strip_markdown(full_raw)
                    self.reply.emit(clean)
                    if speak and not self._cancelled:
                        self.state.emit("speaking", "// RESPONDING")
                        try:
                            self.piper.speak(clean)
                        except Exception as e:
                            core.log(f"piper error: {e!r}")
            except Exception as e:
                core.log(f"brain stream error: {e!r}")

            if not speak:
                # Silent mode — just show the state for the visual + final text
                self.state.emit("speaking", "// REPLY (silent)")
                self.reply.emit(_strip_markdown(full_raw))
            elif _mic_was_muted:
                # Unmute the mic now that Piper is done playing.
                self._mute_default_source(False)

            self.finished_speaking.emit()


# ---------- main app ----------
class JoeyApp(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.hud = JarvisHUD()
        self.trigger = TriggerSocket(SOCK_PATH)
        self.trigger.triggered.connect(self.on_trigger)
        self.hud.user_text_submitted.connect(self.on_submit_text)
        self.hud.dismissed.connect(self._on_dismissed)

        # Brain backend:
        #   default = ClaudeCodeBrain  (claude -p haiku, FULL agent tools:
        #             Read/Write/Edit/Bash/Glob/Grep + MCP, ~3-5s/turn)
        #   openclaw  → OpenClawBrain   (routes through gateway, text-only inference)
        #   claude-full → ClaudeBrain   (original, default model)
        #   hermes-api  → HermesAPIBrain (Nous DeepSeek)
        #   hermes-cli  → HermesBrain   (slow subprocess)
        backend = os.environ.get("JOEY_BRAIN", "claude-haiku").lower()
        core.log(f"loading TTS + brain (HUD mode, mic disabled, brain={backend})...")
        self.piper = core.Piper()
        if backend == "claude-haiku":
            self.brain = core.ClaudeCodeBrain()
        elif backend == "claude-full":
            self.brain = core.ClaudeBrain()
        elif backend == "hermes-cli":
            self.brain = core.HermesBrain()
        elif backend == "hermes-api":
            self.brain = core.HermesAPIBrain()
        else:
            self.brain = core.OpenClawBrain()
        self.worker = BrainWorker(self.brain, self.piper)
        self.worker.state.connect(self._on_state)
        self.worker.transcript.connect(self._on_transcript)
        self.worker.reply.connect(self._on_reply)
        self.worker.finished_speaking.connect(self._auto_hide)
        self.worker.start()

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._idle_dismiss)
        # 'voice' or 'text' — last trigger source; controls follow-up UX.
        self._last_mode: str = "voice"

        # Voice worker — disabled by setting JOEY_VOICE=0
        if os.environ.get("JOEY_VOICE", "1") != "0":
            self.voice = VoiceWorker()
            self.voice.wake_detected.connect(self.on_voice_wake)
            self.voice.transcript_ready.connect(self.on_voice_transcript)
            self.voice.no_speech.connect(self._on_no_speech)
            self.voice.voice_error.connect(lambda e: core.log(f"voice error: {e}"))
            self.voice.start()
            self.worker.finished_speaking.connect(self.voice.signal_cycle_done)
        else:
            self.voice = None

        # Real system telemetry — sampled at 1Hz in the background
        self.sysmon = SystemMonitor()
        self.sysmon.start()
        # Track brain stats for the HUD
        self._brain_name = backend
        self._brain_last_latency_s: float = 0.0
        self._brain_turn_count: int = 0
        self.worker.state.connect(self._on_state_for_stats)
        # Inject everything into the HUD for paint-time consumption
        self.hud.attach_telemetry(
            sysmon=self.sysmon,
            voice=self.voice,
            brain_info=self._brain_info_cb,
        )

        core.log("joey HUD ready.")
        core.log(f"trigger socket: {SOCK_PATH}")

    def on_trigger(self) -> None:
        self._last_mode = "text"
        self.hud.set_transcript("")
        self.hud.set_reply("")
        self.hud.set_state("listening", "// AWAITING INPUT", show_input=True)
        self._idle_timer.start(15_000)

    def on_voice_wake(self) -> None:
        # Wake word fired — open HUD in listening state, no text input
        # (mic is recording for the next ~few seconds via VoiceWorker.recorder).
        self._last_mode = "voice"
        self.hud.set_transcript("")
        self.hud.set_reply("")
        self.hud.set_state("listening", "// LISTENING…", show_input=False)

    def on_voice_transcript(self, text: str) -> None:
        # Voice path → speak the reply aloud
        self.worker.submit(text, speak=True)

    def _on_no_speech(self) -> None:
        # User triggered wake but no speech captured. Quietly dismiss.
        core.log("voice: no speech detected after wake.")
        self.hud.set_state("hidden")
        if self.voice is not None:
            self.voice.signal_cycle_done()

    def on_submit_text(self, txt: str) -> None:
        self._idle_timer.stop()
        # Ctrl+Space path → silent mode (meetings, shared spaces). Reply renders
        # in the HUD but Piper does not play audio.
        self.worker.submit(txt, speak=False)

    def _on_state(self, state: str, status: str) -> None:
        self.hud.set_state(state, status, show_input=False)

    def _on_transcript(self, text: str) -> None:
        self.hud.set_transcript(text)
        self._last_user = text

    def _on_reply(self, text: str) -> None:
        self.hud.set_reply(text)
        self._brain_turn_count += 1
        if getattr(self, "_last_user", None):
            self.hud.push_history(self._last_user, text)

    def _on_state_for_stats(self, state: str, _status: str) -> None:
        if state == "thinking":
            self._thinking_started = time.time()
        elif state == "speaking":
            t0 = getattr(self, "_thinking_started", None)
            if t0:
                self._brain_last_latency_s = time.time() - t0

    def _brain_info_cb(self) -> dict:
        return {
            "name": self._brain_name,
            "last_latency_s": self._brain_last_latency_s,
            "turns": self._brain_turn_count,
        }

    def _auto_hide(self) -> None:
        # Conversation continues. If the user came in by voice, keep wake-word
        # follow-up as the primary path (no text input popup). If text mode,
        # bring the input back for the next typed query.
        if self._last_mode == "voice":
            self.hud.set_state("listening", "// SAY \"HEY JARVIS\" TO CONTINUE",
                               show_input=False)
        else:
            self.hud.set_state("listening", "// CONTINUE OR ESC", show_input=True)
        self._idle_timer.start(45_000)

    def _idle_dismiss(self) -> None:
        if self.hud._state == "listening":
            self.hud.set_state("hidden")

    def _on_dismissed(self) -> None:
        # ESC / idle / programmatic hide — interrupt TTS and let voice resume.
        self.worker.cancel()
        if self.voice is not None:
            self.voice.signal_cycle_done()


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    j = JoeyApp()
    rc = app.exec()
    j.worker.stop()
    j.worker.wait(2000)
    if j.voice is not None:
        j.voice.stop()
        j.voice.wait(2000)
    if hasattr(j, "sysmon"):
        j.sysmon.stop()
        j.sysmon.wait(2000)
    sys.exit(rc)


if __name__ == "__main__":
    main()
