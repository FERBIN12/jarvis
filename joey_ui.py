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
    "thinking":  (QColor(255, 170, 50),  QColor(255, 215, 100), QColor(255, 235, 180)),
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

        amp = {"listening": 0.85, "thinking": 0.55, "speaking": 1.0}.get(self._state, 0.15)
        for i in range(len(self._bars)):
            base = (math.sin(self._tick_n * 0.14 + i * 0.32) + 1) * 0.5
            self._target_bars[i] = base * amp * (0.6 + 0.4 * random.random())
            self._bars[i] = self._bars[i] * 0.72 + self._target_bars[i] * 0.28

        self.update()

    def push_history(self, user: str, reply: str) -> None:
        self._history.insert(0, (user, reply))
        del self._history[6:]

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

        # 2) BG: hex grid + global scan line
        self._draw_hex_grid(p, accent)
        self._draw_horizontal_scan(p, accent)

        # 3) HUD frame brackets
        self._draw_screen_brackets(p, primary, W, H)

        # 4) Corner panels (telemetry, subsystems, history, globe)
        self._draw_panel_telemetry(p, primary, accent, text_color)
        self._draw_panel_subsystems(p, primary, accent, text_color)
        self._draw_panel_history(p, primary, accent, text_color)
        self._draw_panel_globe(p, primary, accent)

        # 5) Center: orb stack
        cx, cy = W / 2, H / 2 - 30
        self._draw_outer_arc_ring(p, cx, cy, primary, accent, radius=320)
        self._draw_tick_marks(p, cx, cy, primary, r_in=290, r_out=302, count=72)
        self._draw_audio_bars(p, cx, cy, accent, r_base=200, max_len=72)
        self._draw_mid_ring(p, cx, cy, accent, radius=180)
        self._draw_inner_ring(p, cx, cy, accent, radius=140)
        self._draw_sphere(p, cx, cy, primary, accent, base_r=92)
        self._draw_scan(p, cx, cy, accent, radius=300)

        # 6) Header + footer text
        self._draw_header_bar(p, primary, accent, text_color, W)
        self._draw_status_big(p, cx, cy, text_color)
        self._draw_transcript_reply(p, text_color, W, H)
        p.end()

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

    # --- LEFT TOP: telemetry panel ---
    def _draw_panel_telemetry(self, p: QPainter, primary, accent, text) -> None:
        x, y, w, h = 60, 100, 280, 180
        self._panel_frame(p, x, y, w, h, primary, "TELEMETRY")
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(9)
        p.setFont(f)
        p.setPen(text)
        amp_avg = sum(self._bars) / len(self._bars)
        rows = [
            f"FREQ      16000 Hz",
            f"CHANNELS  01",
            f"AMP       {amp_avg*100:6.2f}%",
            f"CPU       {(12 + (self._tick_n % 25)):>3d}%",
            f"MEM       {(47 + (self._tick_n % 13)):>3d}%",
            f"UPTIME    {self._tick_n // 30:>04d}s",
        ]
        for i, r in enumerate(rows):
            p.drawText(x + 16, y + 60 + i * 18, r)
        # mini amp bar
        bar_x = x + 16; bar_y = y + h - 22; bar_w = w - 32; bar_h = 8
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 80), 1))
        p.drawRect(bar_x, bar_y, bar_w, bar_h)
        fill = int(bar_w * min(amp_avg * 2.5, 1.0))
        p.fillRect(bar_x, bar_y, fill, bar_h,
                   QColor(accent.red(), accent.green(), accent.blue(), 180))

    # --- RIGHT TOP: subsystems panel ---
    def _draw_panel_subsystems(self, p: QPainter, primary, accent, text) -> None:
        W = self.width()
        w, h = 280, 180
        x, y = W - w - 60, 100
        self._panel_frame(p, x, y, w, h, primary, "SUBSYSTEMS")
        f = QFont("Monospace"); f.setStyleHint(QFont.TypeWriter); f.setPointSize(9)
        p.setFont(f)
        rows = [
            ("BRAIN", "HERMES", True),
            ("VOICE", "ARMED", True),
            ("WAKE",  "JARVIS", True),
            ("STT",   "WHISPER", True),
            ("TTS",   "PIPER", True),
            ("MCP",   "STBY", False),
        ]
        for i, (k, v, ok) in enumerate(rows):
            yy = y + 60 + i * 18
            p.setPen(text)
            p.drawText(x + 16, yy, f"{k:<7s}")
            ok_col = QColor(80, 255, 160, 230) if ok else QColor(255, 180, 60, 200)
            p.setPen(ok_col)
            p.drawText(x + 96, yy, v)
            # status dot
            p.setBrush(ok_col)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(x + w - 24, yy - 4), 3, 3)

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

    # --- RIGHT BOTTOM: wireframe globe ---
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

    # --- audio bars (scaled) ---
    def _draw_audio_bars(self, p, cx, cy, accent, r_base=200, max_len=72) -> None:
        n = len(self._bars)
        for i, h in enumerate(self._bars):
            ang = i * (math.pi * 2 / n)
            r1 = r_base
            r2 = r_base + max_len * h
            x1 = cx + math.cos(ang) * r1
            y1 = cy + math.sin(ang) * r1
            x2 = cx + math.cos(ang) * r2
            y2 = cy + math.sin(ang) * r2
            alpha = int(120 + 130 * h)
            pen = QPen(QColor(accent.red(), accent.green(), accent.blue(), alpha), 2.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

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

    # --- central sphere (scaled up) ---
    def _draw_sphere(self, p, cx, cy, primary, accent, base_r=92) -> None:
        pulse = 1.0 + 0.10 * math.sin(self._sphere_phase)
        r = base_r * pulse
        # outer halo (bloom)
        for i, alpha in enumerate((26, 18, 12, 7)):
            rr = r + 10 + i * 14
            halo = QRadialGradient(QPointF(cx, cy), rr)
            halo.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), alpha))
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
        # highlight
        hl = QRadialGradient(QPointF(cx - r * 0.4, cy - r * 0.4), r * 0.7)
        hl.setColorAt(0.0, QColor(255, 255, 255, 130))
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
            try:
                woken = self._listen_for_wake()
            except Exception as e:
                self.voice_error.emit(f"wake: {e!r}")
                time.sleep(1)
                continue
            if not woken or self._stop:
                continue

            self.wake_detected.emit()
            self._cycle_done.clear()

            # Record + transcribe in one shot. Recorder closes its own stream
            # before this returns, so no contention with subsequent wake loop.
            try:
                audio = self._recorder.record()
            except Exception as e:
                self.voice_error.emit(f"record: {e!r}")
                self._cycle_done.set()
                continue

            if audio.size == 0:
                self.no_speech.emit()
                self._cycle_done.set()
                continue

            try:
                text = self._whisper.transcribe(audio)
            except Exception as e:
                self.voice_error.emit(f"whisper: {e!r}")
                self._cycle_done.set()
                continue

            if not text:
                self.no_speech.emit()
                self._cycle_done.set()
                continue

            self.transcript_ready.emit(text)
            # Block until JoeyApp signals the brain + TTS cycle is done. Without
            # this, we'd loop back into wake detection while the speaker is
            # playing — Piper's output would trigger the mic and re-fire wake.
            self._cycle_done.wait()

    def _listen_for_wake(self) -> bool:
        chunk = 1280  # 80ms @ 16kHz — openWakeWord's preferred frame
        with sd.RawInputStream(
            samplerate=16000, blocksize=chunk, dtype="int16", channels=1
        ) as stream:
            while not self._stop:
                data, _ = stream.read(chunk)
                pcm = np.frombuffer(bytes(data), dtype=np.int16)
                preds = self._wake.model.predict(pcm)
                for _name, score in preds.items():
                    if score >= core.WAKE_THRESHOLD:
                        self._wake.model.reset()
                        return True
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
            try:
                raw_reply = self.brain.ask(prompt)
            except Exception as e:
                raw_reply = f"(error: {e})"
            clean_reply = _strip_markdown(raw_reply)
            if self._cancelled:
                self.finished_speaking.emit()
                continue
            status = "// RESPONDING" if speak else "// REPLY (silent)"
            self.state.emit("speaking", status)
            self.reply.emit(clean_reply)
            if speak and not self._cancelled:
                try:
                    self.piper.speak(clean_reply)
                except Exception as e:
                    core.log(f"piper error: {e!r}")
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

        # Brain backend: Hermes Agent by default (procedural memory, skill
        # accumulation, MCP-native). Set JOEY_BRAIN=claude to fall back.
        backend = os.environ.get("JOEY_BRAIN", "hermes").lower()
        core.log(f"loading TTS + brain (HUD mode, mic disabled, brain={backend})...")
        self.piper = core.Piper()
        if backend == "claude":
            self.brain = core.ClaudeBrain()
        else:
            self.brain = core.HermesBrain()
        self.worker = BrainWorker(self.brain, self.piper)
        self.worker.state.connect(self._on_state)
        self.worker.transcript.connect(self._on_transcript)
        self.worker.reply.connect(self._on_reply)
        self.worker.finished_speaking.connect(self._auto_hide)
        self.worker.start()

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._idle_dismiss)

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

        core.log("joey HUD ready.")
        core.log(f"trigger socket: {SOCK_PATH}")

    def on_trigger(self) -> None:
        self.hud.set_transcript("")
        self.hud.set_reply("")
        self.hud.set_state("listening", "// AWAITING INPUT", show_input=True)
        self._idle_timer.start(15_000)

    def on_voice_wake(self) -> None:
        # Wake word fired — open HUD in listening state, no text input
        # (mic is recording for the next ~few seconds via VoiceWorker.recorder).
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
        if getattr(self, "_last_user", None):
            self.hud.push_history(self._last_user, text)

    def _auto_hide(self) -> None:
        # Conversation continues — drop back to listening so user can follow up.
        self.hud.set_state("listening", "// CONTINUE OR ESC", show_input=True)
        self._idle_timer.start(45_000)  # 45s idle window between turns

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
    sys.exit(rc)


if __name__ == "__main__":
    main()
