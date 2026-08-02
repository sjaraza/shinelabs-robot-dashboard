"""ShineLabs Robot Console — the Tkinter GUI that students run on their laptop.

Layout, in one window because a classroom does not need panes to arrange:

  ┌ connect bar ───────────────────────────────────────────────┐
  │ robot [zoomer.local] user [___] pass [___]  [Connect]      │
  ├ vitals ──────────────┬ sensors ──────────┬ camera ─────────┤
  │ battery gauge        │ distance          │  [ image ]      │
  │ cpu / wifi / mem     │ grayscale x3      │  pan / tilt     │
  │ uptime / load        │ line / cliff      │  [Capture]      │
  ├ drive ───────────────┴───────────────────┴─────────────────┤
  │ speed ▮▮▮▯▯   steer ◀──▶   arrow keys to drive   [ STOP ]  │
  ├ log ───────────────────────────────────────────────────────┤

Two things that shape the whole file:

* TKINTER IS SINGLE-THREADED. The transport's callbacks arrive on paramiko's
  threads, so they may not touch widgets. Everything is pushed onto a queue and
  drained by _pump() on the main thread via after(). Calling into Tk from a
  worker thread produces crashes that look random and are miserable to diagnose.

* DEAD-MAN DRIVING. Holding an arrow key drives; releasing it stops. Combined
  with the agent's watchdog, a robot only moves while a student is actively
  asking it to.

Dependencies: Tkinter (ships with Python) and paramiko. Nothing else — every
extra install is twenty chances to lose ten minutes of a lesson.
"""

from __future__ import annotations

import platform
import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from .transport import RobotLink, TransportError

# Matches the deck's palette so the tool and the slides look like one thing.
BG = "#12141a"
PANEL = "#1b1f28"
LINE = "#2c323f"
TEXT = "#e8ebf1"
MUTED = "#98a1b3"
ACCENT = "#56b6f0"
OK = "#4ec9a5"
WARN = "#e8b93f"
BAD = "#f0716f"

DRIVE_SPEED_DEFAULT = 50
STEER_STEP = 10


class ConsoleApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ShineLabs Robot Console")
        self.configure(bg=BG)
        self.geometry("1080x720")
        self.minsize(940, 660)

        self.inbox: queue.Queue[dict] = queue.Queue()
        self.link = RobotLink(
            on_message=self.inbox.put,
            on_state=lambda s, d: self.inbox.put({"type": "_state", "state": s, "detail": d}),
            on_log=lambda m: self.inbox.put({"type": "_log", "message": m}),
        )

        self.limits = {"speed": [-100, 100], "steer": [-30, 30], "pan": [-90, 90], "tilt": [-35, 65]}
        self.battery_ref = {"full": 8.4, "good": 7.6, "warn": 7.15, "empty": 6.0}
        self.held: set[str] = set()
        self.mono = tkfont.Font(family="Menlo" if platform.system() == "Darwin" else "Consolas", size=12)
        self.mono_big = tkfont.Font(family=self.mono.cget("family"), size=22, weight="bold")

        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._pump)

    # ------------------------------------------------------------------ #
    # construction
    # ------------------------------------------------------------------ #

    def _panel(self, parent, title: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        tk.Label(outer, text=title.upper(), bg=PANEL, fg=MUTED,
                 font=(self.mono.cget("family"), 9, "bold"), anchor="w").pack(
                     fill="x", padx=10, pady=(8, 4))
        return outer

    def _row(self, parent, label: str) -> tk.Label:
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", padx=10, pady=1)
        tk.Label(row, text=label, bg=PANEL, fg=MUTED, font=(self.mono.cget("family"), 10),
                 anchor="w", width=15).pack(side="left")
        value = tk.Label(row, text="—", bg=PANEL, fg=TEXT, font=self.mono, anchor="w")
        value.pack(side="left", fill="x", expand=True)
        return value

    def _build_ui(self) -> None:
        # --- connect bar ---------------------------------------------- #
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(12, 8))

        self.host_var = tk.StringVar(value="")
        self.user_var = tk.StringVar(value="")
        self.pass_var = tk.StringVar(value="")

        for label, var, width, show in (
            ("robot", self.host_var, 20, None),
            ("user", self.user_var, 12, None),
            ("password", self.pass_var, 14, "•"),
        ):
            tk.Label(bar, text=label, bg=BG, fg=MUTED, font=(self.mono.cget("family"), 10)).pack(side="left", padx=(0, 4))
            entry = tk.Entry(bar, textvariable=var, width=width, show=show, bg=PANEL, fg=TEXT,
                             insertbackground=TEXT, relief="flat", font=self.mono,
                             highlightbackground=LINE, highlightthickness=1)
            entry.pack(side="left", padx=(0, 14))
            entry.bind("<Return>", lambda _e: self._toggle_connect())

        self.connect_btn = tk.Button(bar, text="Connect", command=self._toggle_connect,
                                     bg=ACCENT, fg="#0b0d12", relief="flat",
                                     font=(self.mono.cget("family"), 11, "bold"),
                                     activebackground=ACCENT, padx=16, cursor="hand2")
        self.connect_btn.pack(side="left")

        self.status = tk.Label(bar, text="offline", bg=BG, fg=MUTED, font=self.mono)
        self.status.pack(side="right")

        # --- three columns -------------------------------------------- #
        cols = tk.Frame(self, bg=BG)
        cols.pack(fill="both", expand=True, padx=12)
        for i, w in enumerate((1, 1, 1)):
            cols.columnconfigure(i, weight=w, uniform="c")
        cols.rowconfigure(0, weight=1)

        # vitals
        vit = self._panel(cols, "Vitals")
        vit.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.batt_value = tk.Label(vit, text="—", bg=PANEL, fg=TEXT, font=self.mono_big)
        self.batt_value.pack(padx=10, anchor="w")
        self.batt_canvas = tk.Canvas(vit, height=14, bg=PANEL, highlightthickness=0)
        self.batt_canvas.pack(fill="x", padx=10, pady=(2, 8))
        self.v_state = self._row(vit, "state")
        self.v_temp = self._row(vit, "cpu temp")
        self.v_throttle = self._row(vit, "throttled")
        self.v_wifi = self._row(vit, "wifi")
        self.v_load = self._row(vit, "load")
        self.v_mem = self._row(vit, "mem free")
        self.v_uptime = self._row(vit, "uptime")
        self.v_mode = self._row(vit, "agent")

        # sensors
        sen = self._panel(cols, "Sensors")
        sen.grid(row=0, column=1, sticky="nsew", padx=6)
        self.dist_value = tk.Label(sen, text="—", bg=PANEL, fg=TEXT, font=self.mono_big)
        self.dist_value.pack(padx=10, anchor="w")
        tk.Label(sen, text="distance to nearest object", bg=PANEL, fg=MUTED,
                 font=(self.mono.cget("family"), 9)).pack(padx=10, anchor="w")
        self.dist_canvas = tk.Canvas(sen, height=44, bg=PANEL, highlightthickness=0)
        self.dist_canvas.pack(fill="x", padx=10, pady=(6, 10))
        self.dist_history: list[float | None] = []

        tk.Label(sen, text="GRAYSCALE", bg=PANEL, fg=MUTED,
                 font=(self.mono.cget("family"), 9, "bold")).pack(padx=10, anchor="w")
        self.gs_canvas = tk.Canvas(sen, height=58, bg=PANEL, highlightthickness=0)
        self.gs_canvas.pack(fill="x", padx=10, pady=(2, 6))
        self.s_line = self._row(sen, "line")
        self.s_cliff = self._row(sen, "cliff")

        # camera
        cam = self._panel(cols, "Camera")
        cam.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self.image_label = tk.Label(cam, text="no photo yet", bg="#0b0d12", fg=MUTED,
                                    font=(self.mono.cget("family"), 10), width=42, height=13)
        self.image_label.pack(padx=10, pady=(0, 8), fill="both", expand=True)
        self.photo = None                     # keep a reference or Tk garbage-collects it

        self.pan_var = tk.DoubleVar(value=0)
        self.tilt_var = tk.DoubleVar(value=0)
        self._slider(cam, "pan", self.pan_var, self.limits["pan"], self._send_cam)
        self._slider(cam, "tilt", self.tilt_var, self.limits["tilt"], self._send_cam)
        self.snap_btn = tk.Button(cam, text="Capture", command=self._snap, state="disabled",
                                  bg=PANEL, fg=TEXT, relief="flat", font=self.mono,
                                  highlightbackground=LINE, highlightthickness=1, cursor="hand2")
        self.snap_btn.pack(padx=10, pady=(4, 10), fill="x")

        # --- drive ----------------------------------------------------- #
        drive = self._panel(self, "Drive")
        drive.pack(fill="x", padx=12, pady=(12, 0))
        inner = tk.Frame(drive, bg=PANEL)
        inner.pack(fill="x", padx=10, pady=(0, 10))

        left = tk.Frame(inner, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        self.speed_var = tk.DoubleVar(value=DRIVE_SPEED_DEFAULT)
        self._slider(left, "speed", self.speed_var, [0, 100], None, parent_pack=True)
        tk.Label(left, text="Hold the arrow keys to drive. Release to stop.  Space = STOP.",
                 bg=PANEL, fg=MUTED, font=(self.mono.cget("family"), 10)).pack(anchor="w", pady=(4, 0))
        tk.Label(left, text="Speed jumps straight to about half power — the motor driver has no slow range.",
                 bg=PANEL, fg=MUTED, font=(self.mono.cget("family"), 9)).pack(anchor="w")

        pad = tk.Frame(inner, bg=PANEL)
        pad.pack(side="left", padx=20)
        for (r, c, key, glyph) in ((0, 1, "Up", "▲"), (1, 0, "Left", "◀"),
                                   (1, 1, "Down", "▼"), (1, 2, "Right", "▶")):
            b = tk.Button(pad, text=glyph, width=3, bg=PANEL, fg=TEXT, relief="flat",
                          font=(self.mono.cget("family"), 14), highlightbackground=LINE,
                          highlightthickness=1, cursor="hand2")
            b.grid(row=r, column=c, padx=2, pady=2)
            b.bind("<ButtonPress-1>", lambda _e, k=key: self._press(k))
            b.bind("<ButtonRelease-1>", lambda _e, k=key: self._release(k))

        self.stop_btn = tk.Button(inner, text="STOP", command=self._stop, bg=BAD, fg="#0b0d12",
                                  relief="flat", font=(self.mono.cget("family"), 15, "bold"),
                                  padx=26, pady=10, activebackground=BAD, cursor="hand2")
        self.stop_btn.pack(side="right")

        # --- log ------------------------------------------------------- #
        logp = self._panel(self, "Log")
        logp.pack(fill="both", expand=False, padx=12, pady=12)
        self.logbox = tk.Text(logp, height=6, bg="#0b0d12", fg=MUTED, relief="flat",
                              font=(self.mono.cget("family"), 10), wrap="word",
                              highlightthickness=0, insertbackground=TEXT)
        self.logbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.logbox.configure(state="disabled")

        self._set_controls(False)

    def _slider(self, parent, label, var, bounds, command, parent_pack=False):
        frame = tk.Frame(parent, bg=PANEL)
        frame.pack(fill="x", padx=(0 if parent_pack else 10), pady=2)
        tk.Label(frame, text=label, bg=PANEL, fg=MUTED, width=6, anchor="w",
                 font=(self.mono.cget("family"), 10)).pack(side="left")
        readout = tk.Label(frame, text="0", bg=PANEL, fg=TEXT, width=5, anchor="e", font=self.mono)
        readout.pack(side="right")

        def on_change(_v):
            readout.configure(text=f"{var.get():.0f}")
            if command:
                command()

        scale = tk.Scale(frame, variable=var, from_=bounds[0], to=bounds[1], orient="horizontal",
                         showvalue=False, bg=PANEL, fg=TEXT, troughcolor="#0b0d12",
                         highlightthickness=0, relief="flat", sliderrelief="flat",
                         activebackground=ACCENT, command=on_change)
        scale.pack(side="left", fill="x", expand=True, padx=6)
        return scale

    def _bind_keys(self) -> None:
        for key in ("Up", "Down", "Left", "Right"):
            self.bind(f"<KeyPress-{key}>", lambda e, k=key: self._press(k))
            self.bind(f"<KeyRelease-{key}>", lambda e, k=key: self._release(k))
        self.bind("<space>", lambda _e: self._stop())

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #

    def _toggle_connect(self) -> None:
        if self.link.connected:
            self.link.close()
            return
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        if not host or not user:
            self._log("Enter your robot's name and your username first.")
            return
        self.connect_btn.configure(state="disabled", text="…")

        def worker():
            try:
                self.link.connect(host, user, self.pass_var.get())
            except TransportError as exc:
                self.inbox.put({"type": "_error", "message": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self.inbox.put({"type": "_error", "message": f"{type(exc).__name__}: {exc}"})

        threading.Thread(target=worker, daemon=True).start()

    def _set_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.snap_btn.configure(state=state)

    # ------------------------------------------------------------------ #
    # driving
    # ------------------------------------------------------------------ #

    def _press(self, key: str) -> None:
        if key in self.held or not self.link.connected:
            return
        self.held.add(key)
        self._send_drive()

    def _release(self, key: str) -> None:
        self.held.discard(key)
        self._send_drive()

    def _send_drive(self) -> None:
        if not self.link.connected:
            return
        speed = int(self.speed_var.get())
        fwd = ("Up" in self.held) - ("Down" in self.held)
        turn = ("Right" in self.held) - ("Left" in self.held)
        steer = turn * min(abs(self.limits["steer"][0]), self.limits["steer"][1])
        self.link.send(cmd="drive", speed=fwd * speed, steer=steer)

    def _stop(self) -> None:
        self.held.clear()
        if self.link.connected:
            self.link.send(cmd="stop")

    def _send_cam(self) -> None:
        if self.link.connected:
            self.link.send(cmd="cam", pan=self.pan_var.get(), tilt=self.tilt_var.get())

    def _snap(self) -> None:
        if not self.link.connected:
            return
        self.snap_btn.configure(state="disabled", text="capturing…")
        self.link.send(cmd="snap", width=640, height=480, immediate=True)

    # ------------------------------------------------------------------ #
    # inbound
    # ------------------------------------------------------------------ #

    def _pump(self) -> None:
        """Drain the inbox on the main thread. The only place widgets change."""
        try:
            for _ in range(200):            # bounded, so a flood cannot freeze the UI
                msg = self.inbox.get_nowait()
                try:
                    self._handle(msg)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ui error: {type(exc).__name__}: {exc}")
        except queue.Empty:
            pass
        self.after(50, self._pump)

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")

        if kind == "_state":
            state, detail = msg["state"], msg.get("detail")
            colours = {"online": OK, "connecting": WARN, "offline": MUTED}
            self.status.configure(text=f"{state}{f'  {detail}' if detail else ''}",
                                  fg=colours.get(state, MUTED))
            if state == "offline":
                self.connect_btn.configure(state="normal", text="Connect")
                self._set_controls(False)
                self.held.clear()
            elif state == "connecting":
                self.connect_btn.configure(state="disabled", text="…")
            return

        if kind == "_log":
            self._log(msg["message"]); return

        if kind == "_error":
            self.connect_btn.configure(state="normal", text="Connect")
            self._log(msg["message"])
            return

        if kind == "hello":
            self.limits.update(msg.get("limits") or {})
            self.battery_ref.update(msg.get("battery") or {})
            mode = msg.get("mode")
            self.v_mode.configure(
                text=f"v{msg.get('agent_version')} · {mode}",
                fg=WARN if mode == "sim" else TEXT)
            self.status.configure(text=f"online  {msg.get('hostname') or ''}", fg=OK)
            self.connect_btn.configure(state="normal", text="Disconnect")
            self._set_controls(True)
            self._log(f"connected — agent v{msg.get('agent_version')}, python {msg.get('python')}, mode {mode}")
            if mode == "sim":
                # A silently-simulated robot would be far worse than a loud failure.
                self._log("WARNING: the robot could not load picarx, so nothing will "
                          f"physically move. Reason: {msg.get('hardware_error')}")
            return

        if kind == "telemetry":
            self._telemetry(msg); return

        if kind == "snap":
            self._show_photo(msg); return

        if kind == "snap_error":
            self.snap_btn.configure(state="normal", text="Capture")
            self._log(f"camera failed: {msg.get('error')}")
            return

        if kind == "event":
            self._log(f"event: {msg.get('event')} — {msg.get('detail', '')}")
            if msg.get("event") == "watchdog_stop":
                self.held.clear()
            return

        if kind == "error":
            self._log(f"agent error: {msg.get('error')}"); return

    # --- rendering ------------------------------------------------------ #

    def _telemetry(self, m: dict) -> None:
        volts = m.get("battery_v")
        state = m.get("battery_state")
        colour = {"good": OK, "low": WARN, "critical": BAD, "cutoff": BAD}.get(state, MUTED)
        self.batt_value.configure(text=f"{volts:.2f} V" if volts is not None else "— V", fg=colour)
        self.v_state.configure(text=state or "unknown", fg=colour)
        self._draw_battery(volts, colour)

        temp = m.get("cpu_temp_c")
        self.v_temp.configure(text=f"{temp:.1f} °C" if temp is not None else "—",
                              fg=BAD if (temp or 0) > 75 else TEXT)
        thr = m.get("throttled")
        self.v_throttle.configure(text=thr or "—",
                                  fg=TEXT if thr in (None, "0x0") else WARN)
        dbm = m.get("wifi_dbm")
        self.v_wifi.configure(text=f"{dbm} dBm" if dbm is not None else "—",
                              fg=OK if (dbm or -100) >= -67 else WARN)
        self.v_load.configure(text=f"{m.get('load')}" if m.get("load") is not None else "—")
        mem = m.get("mem_available_mb")
        self.v_mem.configure(text=f"{mem} MB" if mem is not None else "—",
                             fg=WARN if (mem or 999) < 60 else TEXT)
        up = m.get("uptime_s")
        self.v_uptime.configure(text=f"{up // 60}m {up % 60}s" if up else "—")

        dist = m.get("distance_cm")
        if dist is None:
            self.dist_value.configure(text="no echo" if m.get("distance_timeout") else "—", fg=MUTED)
        else:
            self.dist_value.configure(text=f"{dist:.1f} cm",
                                      fg=BAD if dist < 10 else (WARN if dist < 25 else TEXT))
        self.dist_history.append(dist)
        self.dist_history = self.dist_history[-120:]
        self._draw_distance()

        gs = m.get("grayscale")
        self._draw_grayscale(gs)
        line = m.get("line")
        self.s_line.configure(
            text=" ".join("■" if v else "□" for v in line) + "   (■ = dark)" if line else "—")
        cliff = m.get("cliff")
        self.s_cliff.configure(text="CLIFF" if cliff else ("clear" if cliff is not None else "—"),
                               fg=BAD if cliff else TEXT)

    def _draw_battery(self, volts, colour) -> None:
        c = self.batt_canvas
        c.delete("all")
        w = max(c.winfo_width(), 100)
        c.create_rectangle(0, 0, w, 14, fill="#0b0d12", outline=LINE)
        if volts is None:
            return
        lo, hi = self.battery_ref["empty"], self.battery_ref["full"]
        frac = max(0.0, min(1.0, (volts - lo) / (hi - lo)))
        c.create_rectangle(1, 1, 1 + (w - 2) * frac, 13, fill=colour, outline="")
        # Mark the thresholds the robot's own LEDs use, so the bar means something
        # rather than being a decorative percentage.
        for ref in ("warn", "good"):
            x = 1 + (w - 2) * ((self.battery_ref[ref] - lo) / (hi - lo))
            c.create_line(x, 0, x, 14, fill=MUTED, dash=(2, 2))

    def _draw_distance(self) -> None:
        c = self.dist_canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 100), 44
        pts = self.dist_history
        if len(pts) < 2:
            return
        top = max([p for p in pts if p is not None], default=50) or 50
        step = w / max(1, len(pts) - 1)
        prev = None
        for i, p in enumerate(pts):
            if p is None:
                prev = None                 # break the line: a gap is not a reading
                continue
            x, y = i * step, h - 2 - (h - 6) * min(1.0, p / top)
            if prev is not None:
                c.create_line(prev[0], prev[1], x, y, fill=ACCENT, width=1)
            prev = (x, y)
        for i, p in enumerate(pts):
            if p is None:
                c.create_line(i * step, 0, i * step, h, fill="#3a2b2b")

    def _draw_grayscale(self, gs) -> None:
        c = self.gs_canvas
        c.delete("all")
        if not gs:
            return
        w, h = max(c.winfo_width(), 100), 58
        bar_w = w / 3
        for i, (v, name) in enumerate(zip(gs, ("left", "mid", "right"))):
            frac = max(0.0, min(1.0, v / 4095))
            x0 = i * bar_w + 4
            x1 = (i + 1) * bar_w - 4
            c.create_rectangle(x0, 16, x1, h - 2, fill="#0b0d12", outline=LINE)
            c.create_rectangle(x0, 16 + (h - 18) * (1 - frac), x1, h - 2,
                               fill=ACCENT, outline="")
            c.create_text((x0 + x1) / 2, 8, text=f"{name} {v}", fill=MUTED,
                          font=(self.mono.cget("family"), 8))

    def _show_photo(self, msg: dict) -> None:
        self.snap_btn.configure(state="normal", text="Capture")
        try:
            # Tk 8.6+ decodes PNG natively from base64, which is why the agent
            # captures PNG: it keeps Pillow off every student laptop.
            self.photo = tk.PhotoImage(data=msg["data"])
            self.image_label.configure(image=self.photo, text="")
            self._log(f"photo received — {msg.get('bytes', 0) / 1024:.0f} KB")
        except tk.TclError as exc:
            self._log(f"could not display the photo: {exc}")

    def _log(self, message: str) -> None:
        self.logbox.configure(state="normal")
        self.logbox.insert("end", message.rstrip() + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ------------------------------------------------------------------ #

    def _on_close(self) -> None:
        try:
            self.link.close()          # stops the robot before the window dies
        finally:
            self.destroy()


def main() -> None:
    ConsoleApp().mainloop()
