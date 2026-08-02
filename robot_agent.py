#!/usr/bin/env python3
"""ShineLabs robot agent — runs ON the robot, driven by the laptop over SSH.

The laptop uploads this file and runs it as:

    python3 -u /tmp/shinelabs_agent.py

Protocol: newline-delimited JSON, both directions, over the SSH channel's
stdin/stdout. One JSON object per line, no framing of its own.

  laptop -> agent   {"cmd": "drive", "speed": 40, "steer": -10}
  agent  -> laptop  {"type": "telemetry", "battery_v": 7.9, ...}

Design notes, all of which are load-bearing:

* STDOUT IS THE PROTOCOL. Nothing may print to it except JSON lines. The
  vendor libraries do print (robot_hat's debug logging, and reset_mcu shells out
  to pinctrl), so the whole import-and-construct of Picarx happens with stdout
  redirected to stderr. Corrupting the stream is the single easiest way to break
  this thing.

* WATCHDOG. Motors are stopped if no command arrives within WATCHDOG_S. A GUI
  that crashes, a laptop lid that closes, or Wi-Fi that drops must not leave a
  robot driving into a wall. The GUI sends a keepalive to hold the watchdog off.

* NO THIRD-PARTY IMPORTS. Standard library plus picarx, which is already on the
  robot. Nothing to install for the agent itself.

* SIM MODE. If picarx cannot be imported, the agent runs with a stub so the
  protocol can be exercised off-robot. It says so in the hello message; the GUI
  shows it, because silently pretending to drive would be worse than failing.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

AGENT_VERSION = "0.1.0"

TELEMETRY_HZ = 2.0          # sensor sampling rate pushed to the laptop
WATCHDOG_S = 1.5            # motors stop if no command within this window
SNAP_TIMEOUT_S = 20.0       # rpicam-still, worst case

# Limits mirrored from picarx so the agent can clamp before calling in, and so
# the GUI can be told the real ranges rather than guessing.
LIMITS = {
    "speed": [-100, 100],
    "steer": [-30, 30],       # picarx DIR_MIN / DIR_MAX
    "pan": [-90, 90],         # CAM_PAN_MIN / CAM_PAN_MAX
    "tilt": [-35, 65],        # CAM_TILT_MIN / CAM_TILT_MAX
}

# Battery thresholds for a 2S Li-ion pack, taken from the Robot HAT's own LED
# behaviour (2 LEDs >7.6 V, 1 LED >7.15 V) and the pack's 6.0 V protection
# cutoff. Anchoring on these is more honest than a made-up linear percentage.
BATTERY = {"full": 8.4, "good": 7.6, "warn": 7.15, "empty": 6.0}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def emit(obj: dict) -> None:
    """Write one protocol line. The only function allowed to touch stdout."""
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    """Diagnostics go to stderr, which the GUI surfaces but never parses."""
    sys.stderr.write(f"[agent] {message}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# hardware
# --------------------------------------------------------------------------- #


class SimCar:
    """Stand-in for Picarx so the protocol can be tested without a robot."""

    def __init__(self) -> None:
        self._t = time.time()

    def forward(self, speed): pass
    def backward(self, speed): pass
    def stop(self): pass
    def set_dir_servo_angle(self, a): pass
    def set_cam_pan_angle(self, a): pass
    def set_cam_tilt_angle(self, a): pass

    def get_distance(self):
        # A slow sweep plus an occasional -1, so the GUI's "no reading" path
        # actually gets exercised during development.
        t = time.time() - self._t
        return -1 if int(t) % 11 == 0 else round(20 + 15 * (1 + __import__("math").sin(t / 2)), 1)

    def get_grayscale_data(self):
        t = time.time() - self._t
        return [int(900 + 600 * __import__("math").sin(t / 3 + i)) for i in range(3)]

    def get_line_status(self, data):
        return [0 if v > 1000 else 1 for v in data]

    def get_cliff_status(self, data):
        return any(v <= 500 for v in data)


def build_car():
    """Construct Picarx with stdout muzzled; fall back to SimCar.

    robot_hat logs to stdout and reset_mcu runs an external command, so this
    must not happen while stdout is the protocol channel.
    """
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from picarx import Picarx  # noqa: PLC0415 - deliberately late
            car = Picarx()
        return car, "picarx", None
    except Exception as exc:  # noqa: BLE001 - any failure means no hardware
        log(f"picarx unavailable ({type(exc).__name__}: {exc}); running in SIM mode")
        return SimCar(), "sim", f"{type(exc).__name__}: {exc}"


def battery_voltage() -> float | None:
    """Pack voltage via the Robot HAT's internal ADC channel A4."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from robot_hat.device import get_battery_voltage
            return round(float(get_battery_voltage()), 2)
    except Exception:  # noqa: BLE001
        return None


def battery_state(volts: float | None) -> str | None:
    if volts is None:
        return None
    if volts >= BATTERY["good"]:
        return "good"
    if volts >= BATTERY["warn"]:
        return "low"
    if volts > BATTERY["empty"]:
        return "critical"
    return "cutoff"


# --------------------------------------------------------------------------- #
# vitals (cheap, read straight from the OS)
# --------------------------------------------------------------------------- #

_TEMP_RE = re.compile(r"temp=([\d.]+)")
_SIGNAL_RE = re.compile(r"signal:\s*(-?\d+)")


def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def cpu_temp_c() -> float | None:
    m = _TEMP_RE.search(_run(["vcgencmd", "measure_temp"]))
    if m:
        return float(m.group(1))
    try:  # sysfs fallback, works without vcgencmd
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read().strip()) / 1000, 1)
    except OSError:
        return None


def throttled() -> str | None:
    out = _run(["vcgencmd", "get_throttled"]).strip()
    return out.split("=", 1)[1] if "=" in out else None


def wifi_signal_dbm() -> int | None:
    for iface in ("wlan0", "wlp3s0"):
        m = _SIGNAL_RE.search(_run(["iw", "dev", iface, "link"]))
        if m:
            return int(m.group(1))
    return None


def load_avg() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except OSError:
        return None


def mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def uptime_s() -> int | None:
    try:
        with open("/proc/uptime") as fh:
            return int(float(fh.read().split()[0]))
    except (OSError, ValueError):
        return None


def snapshot_png(width: int = 640, height: int = 480, immediate: bool = True) -> tuple[bytes | None, str | None]:
    """One still frame as PNG bytes, straight from rpicam-still's stdout.

    PNG rather than JPEG because Tkinter's PhotoImage decodes PNG natively,
    which keeps Pillow off twenty student laptops. To stdout rather than a file
    so repeated clicking doesn't hammer the SD card.
    """
    cmd = ["rpicam-still", "--nopreview", "--width", str(width), "--height", str(height),
           "--encoding", "png", "-o", "-"]
    if immediate:
        cmd.append("--immediate")   # skip AE/AWB settle: much faster, worse exposure
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=SNAP_TIMEOUT_S)
    except FileNotFoundError:
        return None, "rpicam-still not found"
    except subprocess.TimeoutExpired:
        return None, f"camera timed out after {SNAP_TIMEOUT_S:.0f}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if out.returncode != 0 or not out.stdout:
        err = (out.stderr or b"").decode(errors="replace").strip().splitlines()
        return None, err[-1] if err else f"rpicam-still exited {out.returncode}"
    return out.stdout, None


# --------------------------------------------------------------------------- #
# agent
# --------------------------------------------------------------------------- #


class Agent:
    def __init__(self) -> None:
        self.car, self.mode, self.hw_error = build_car()
        self.commands: queue.Queue[dict] = queue.Queue()
        self.running = True
        self.last_command = time.time()
        self.driving = False
        self.lock = threading.Lock()

    # --- reader ---------------------------------------------------------- #

    def read_stdin(self) -> None:
        """Parse one JSON object per line. EOF means the laptop went away."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                self.commands.put(json.loads(line))
            except json.JSONDecodeError as exc:
                emit({"type": "error", "error": f"bad JSON: {exc}"})
        self.running = False          # stdin closed: shut down

    # --- telemetry ------------------------------------------------------- #

    def telemetry_loop(self) -> None:
        period = 1.0 / TELEMETRY_HZ
        # Vitals change slowly and cost a subprocess each, so sample them at a
        # fraction of the sensor rate. On a Zero 2 W that matters.
        vitals_every = max(1, int(TELEMETRY_HZ * 2))
        tick = 0
        vitals: dict = {}
        while self.running:
            tick += 1
            try:
                grayscale = self.car.get_grayscale_data()
            except Exception as exc:  # noqa: BLE001
                grayscale, gs_err = None, str(exc)
            else:
                gs_err = None
            try:
                distance = self.car.get_distance()
            except Exception as exc:  # noqa: BLE001
                distance, dist_err = None, str(exc)
            else:
                dist_err = None

            if tick % vitals_every == 1:
                volts = battery_voltage()
                vitals = {
                    "battery_v": volts,
                    "battery_state": battery_state(volts),
                    "cpu_temp_c": cpu_temp_c(),
                    "throttled": throttled(),
                    "load": load_avg(),
                    "mem_available_mb": mem_available_mb(),
                    "wifi_dbm": wifi_signal_dbm(),
                    "uptime_s": uptime_s(),
                }

            msg = {
                "type": "telemetry",
                "t": round(time.time(), 3),
                # A negative distance is the sensor's "no echo" sentinel, not a
                # measurement. Pass it through as null plus a flag so the GUI
                # cannot accidentally plot -1 as a distance.
                "distance_cm": distance if (distance is not None and distance >= 0) else None,
                "distance_timeout": distance == -1,
                "grayscale": grayscale,
                "line": self._safe(lambda: self.car.get_line_status(grayscale)) if grayscale else None,
                "cliff": self._safe(lambda: self.car.get_cliff_status(grayscale)) if grayscale else None,
                "driving": self.driving,
                **vitals,
            }
            if gs_err or dist_err:
                msg["sensor_error"] = gs_err or dist_err
            emit(msg)
            time.sleep(period)

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    # --- watchdog -------------------------------------------------------- #

    def watchdog_loop(self) -> None:
        """Stop the motors if the laptop goes quiet.

        Without this, a crashed GUI or a dropped Wi-Fi link leaves the robot
        driving. Twenty of those in a classroom is not acceptable.
        """
        while self.running:
            time.sleep(0.2)
            with self.lock:
                stale = self.driving and (time.time() - self.last_command) > WATCHDOG_S
            if stale:
                self._stop()
                emit({"type": "event", "event": "watchdog_stop",
                      "detail": f"no command for {WATCHDOG_S}s"})

    # --- actions --------------------------------------------------------- #

    def _stop(self) -> None:
        with self.lock:
            self.driving = False
        self._safe(self.car.stop)

    def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        with self.lock:
            self.last_command = time.time()

        if cmd in ("keepalive", "ping"):
            emit({"type": "pong", "t": round(time.time(), 3)})
            return

        if cmd == "stop":
            self._stop()
            emit({"type": "ack", "cmd": "stop"})
            return

        if cmd == "drive":
            speed = int(clamp(float(msg.get("speed", 0)), *LIMITS["speed"]))
            steer = float(clamp(float(msg.get("steer", 0)), *LIMITS["steer"]))
            self._safe(lambda: self.car.set_dir_servo_angle(steer))
            if speed == 0:
                self._stop()
            else:
                with self.lock:
                    self.driving = True
                if speed > 0:
                    self._safe(lambda: self.car.forward(speed))
                else:
                    self._safe(lambda: self.car.backward(-speed))
            emit({"type": "ack", "cmd": "drive", "speed": speed, "steer": steer})
            return

        if cmd == "cam":
            # Echo the APPLIED angles, not the requested ones. Asking for tilt=80
            # gets clamped to 65, and a GUI told "80" would draw a position the
            # servo is not in.
            applied: dict = {}
            if "pan" in msg:
                applied["pan"] = float(clamp(float(msg["pan"]), *LIMITS["pan"]))
                self._safe(lambda: self.car.set_cam_pan_angle(applied["pan"]))
            if "tilt" in msg:
                applied["tilt"] = float(clamp(float(msg["tilt"]), *LIMITS["tilt"]))
                self._safe(lambda: self.car.set_cam_tilt_angle(applied["tilt"]))
            emit({"type": "ack", "cmd": "cam", **applied})
            return

        if cmd == "snap":
            # Runs on the command thread: capture takes seconds and telemetry
            # must keep flowing, which it does because that is a separate thread.
            emit({"type": "event", "event": "snap_started"})
            data, err = snapshot_png(
                int(msg.get("width", 640)), int(msg.get("height", 480)),
                bool(msg.get("immediate", True)),
            )
            if data is None:
                emit({"type": "snap_error", "error": err})
            else:
                # base64 inside the JSON line: one framing mechanism, not two.
                emit({"type": "snap", "format": "png", "bytes": len(data),
                      "data": base64.b64encode(data).decode("ascii")})
            return

        if cmd == "shutdown":
            emit({"type": "ack", "cmd": "shutdown"})
            self.running = False
            return

        emit({"type": "error", "error": f"unknown command: {cmd!r}"})

    # --- main ------------------------------------------------------------ #

    def run(self) -> None:
        emit({
            "type": "hello",
            "agent_version": AGENT_VERSION,
            "mode": self.mode,                  # "picarx" or "sim"
            "hardware_error": self.hw_error,
            "limits": LIMITS,
            "battery": BATTERY,
            "telemetry_hz": TELEMETRY_HZ,
            "watchdog_s": WATCHDOG_S,
            "python": sys.version.split()[0],
            "hostname": os.uname().nodename if hasattr(os, "uname") else None,
        })

        for target in (self.read_stdin, self.telemetry_loop, self.watchdog_loop):
            threading.Thread(target=target, daemon=True).start()

        try:
            while self.running:
                try:
                    msg = self.commands.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    self.handle(msg)
                except Exception as exc:  # noqa: BLE001 - never die on one bad command
                    emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            # Whatever happened, the robot must not be left driving.
            self._stop()
            log("stopped motors, exiting")


if __name__ == "__main__":
    Agent().run()
