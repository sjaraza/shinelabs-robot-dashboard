#!/usr/bin/env python3
"""ShineLabs robot playground — run this ON THE ROBOT, over SSH.

    python3 ~/robot_play.py

A menu of things to try. Every action prints the Python it is about to run, so
this doubles as a tour of the library you will be writing against in later
lectures:

    >>> px.forward(50)

This file is meant to be read as much as run. Open it up:

    less ~/robot_play.py

Safety, because this drives a real vehicle:

  * every movement is time-limited, then stops
  * live drive stops as soon as you stop pressing keys
  * the motors are stopped on quit, on Ctrl-C, and on any error
"""

from __future__ import annotations

import contextlib
import os
import select
import sys
import termios
import time
import tty

BOLD = "\033[1m"; DIM = "\033[2m"; RST = "\033[0m"
GRN = "\033[32m"; YLW = "\033[33m"; RED = "\033[31m"; CYA = "\033[36m"

PHOTO_DIR = os.path.expanduser("~/photos")
BURST_S = 1.0            # how long a menu-driven move lasts
LIVE_IDLE_STOP_S = 0.4   # live drive: stop if no key within this long


def show(code: str) -> None:
    """Print the API call before making it. This is the teaching bit."""
    print(f"    {CYA}>>> {code}{RST}")


def ask(prompt: str, default: float, low: float, high: float) -> float:
    raw = input(f"    {prompt} [{default}] ({low} to {high}): ").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"    {YLW}not a number — using {default}{RST}")
        return default
    if not low <= value <= high:
        clamped = max(low, min(high, value))
        print(f"    {YLW}{value} is outside {low}..{high} — using {clamped}{RST}")
        return clamped
    return value


# --------------------------------------------------------------------------- #
# single-key reading, for live drive
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def raw_keys():
    """Put the terminal in raw mode so keys arrive without waiting for Enter."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def key_within(timeout: float) -> str | None:
    """Next keypress, or None if nothing arrives in time.

    A terminal has no concept of "key released" — it only sends characters, and
    holding a key produces a stream of repeats. So "no key for a moment" is the
    closest thing to "let go", and that is what stops the robot.
    """
    if select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return None


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #


def act_distance(px) -> None:
    print(f"\n  {BOLD}Ultrasonic distance{RST}   {DIM}Ctrl-C to stop{RST}\n")
    show("px.get_distance()")
    print()
    try:
        while True:
            d = px.get_distance()
            if d is None or d < 0:
                # -1 is the sensor's way of saying "no echo came back". It is not
                # a distance, and treating it as one is how robots hit walls.
                print(f"    {YLW}no echo{RST}      (the sensor returned {d})      ", end="\r")
            else:
                bar = "#" * min(40, int(d / 2))
                print(f"    {d:6.1f} cm  {GRN}{bar}{RST}".ljust(70), end="\r")
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\n")


def act_grayscale(px) -> None:
    print(f"\n  {BOLD}Grayscale sensors{RST}   {DIM}Ctrl-C to stop{RST}")
    print(f"  {DIM}Three sensors underneath, 0 = dark, 4095 = bright.{RST}\n")
    show("px.get_grayscale_data()")
    show("px.get_line_status(values)")
    print()
    try:
        while True:
            values = px.get_grayscale_data()
            line = px.get_line_status(values)
            cells = "  ".join(f"{n}={v:>4}{'#' if s else '.'}"
                              for n, v, s in zip(("L", "M", "R"), values, line))
            cliff = f"  {RED}CLIFF{RST}" if px.get_cliff_status(values) else ""
            print(f"    {cells}{cliff}".ljust(74), end="\r")
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n")


def act_vitals(px) -> None:
    print(f"\n  {BOLD}Robot vitals{RST}\n")
    show("from robot_hat.device import get_battery_voltage")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from robot_hat.device import get_battery_voltage
            volts = round(get_battery_voltage(), 2)
    except Exception as exc:  # noqa: BLE001
        volts = None
        print(f"    {RED}battery read failed: {exc}{RST}")
    if volts is not None:
        # Thresholds from the Robot HAT's own two battery LEDs; 6.0 V is the
        # pack's protection cutoff.
        if volts >= 7.6:
            verdict, colour = "good", GRN
        elif volts >= 7.15:
            verdict, colour = "getting low", YLW
        else:
            verdict, colour = "charge it", RED
        print(f"    battery      {colour}{volts} V  ({verdict}){RST}")

    def read(path, transform=lambda x: x):
        try:
            with open(path) as fh:
                return transform(fh.read().strip())
        except (OSError, ValueError):
            return None

    temp = read("/sys/class/thermal/thermal_zone0/temp", lambda v: f"{int(v)/1000:.1f} °C")
    up = read("/proc/uptime", lambda v: f"{int(float(v.split()[0])) // 60} min")
    print(f"    cpu temp     {temp or '—'}")
    print(f"    uptime       {up or '—'}")
    print(f"    load         {os.getloadavg()[0]:.2f}")
    print(f"    hostname     {os.uname().nodename}")
    print()


def act_drive(px) -> None:
    print(f"\n  {BOLD}Drive in a straight line{RST}")
    print(f"  {YLW}Put the robot on the floor first.{RST}\n")
    speed = ask("speed", 50, -100, 100)
    seconds = ask("for how many seconds", BURST_S, 0.2, 3.0)
    print()
    show(f"px.forward({int(abs(speed))})" if speed >= 0 else f"px.backward({int(abs(speed))})")
    show(f"time.sleep({seconds})")
    show("px.stop()")
    print()
    input(f"    {DIM}Enter to go, Ctrl-C to cancel… {RST}")
    try:
        (px.forward if speed >= 0 else px.backward)(int(abs(speed)))
        time.sleep(seconds)
    finally:
        px.stop()          # always, even if the sleep is interrupted
    print(f"    {GRN}done{RST}\n")


def act_steer(px) -> None:
    print(f"\n  {BOLD}Steering{RST}")
    print(f"  {DIM}Only ±30° — the linkage binds beyond that.{RST}\n")
    angle = ask("angle", 0, -30, 30)
    show(f"px.set_dir_servo_angle({angle})")
    px.set_dir_servo_angle(angle)
    print(f"    {GRN}front wheels moved{RST}\n")


def act_camera(px) -> None:
    print(f"\n  {BOLD}Aim the camera{RST}")
    print(f"  {DIM}pan ±90°, tilt −35° to +65°{RST}\n")
    pan = ask("pan", 0, -90, 90)
    tilt = ask("tilt", 0, -35, 65)
    show(f"px.set_cam_pan_angle({pan})")
    show(f"px.set_cam_tilt_angle({tilt})")
    px.set_cam_pan_angle(pan)
    px.set_cam_tilt_angle(tilt)
    print(f"    {GRN}camera moved{RST}\n")


def act_photo(px) -> None:
    print(f"\n  {BOLD}Take a photo{RST}\n")
    os.makedirs(PHOTO_DIR, exist_ok=True)
    path = os.path.join(PHOTO_DIR, time.strftime("photo-%H%M%S.png"))
    cmd = ["rpicam-still", "--nopreview", "--immediate",
           "--width", "640", "--height", "480", "--encoding", "png", "-o", path]
    show(" ".join(cmd))
    import subprocess
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=25)
    except FileNotFoundError:
        print(f"    {RED}rpicam-still is not installed{RST}\n"); return
    except subprocess.TimeoutExpired:
        print(f"    {RED}the camera timed out{RST}\n"); return
    if result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace").strip().splitlines()
        print(f"    {RED}failed: {err[-1] if err else result.returncode}{RST}\n"); return
    size = os.path.getsize(path) // 1024
    count = len([f for f in os.listdir(PHOTO_DIR) if f.endswith(".png")])
    print(f"    {GRN}saved{RST} {path}  ({size} KB)")
    print(f"    {DIM}{count} photo(s) in {PHOTO_DIR}{RST}")
    # The robot has no screen. Menu option 8 serves them over HTTP so they can be
    # opened in a browser, which beats copying files about.
    print(f"\n    To look at it, pick {BOLD}8{RST} from the menu "
          f"({DIM}show my photos in a browser{RST})\n")


def act_gallery(px) -> None:
    """Serve ~/photos over HTTP so a browser on the laptop can see them.

    The robot has no screen, so a PNG sitting on its SD card is invisible.
    Copying files with scp means a second terminal and another password; one
    line of Python turns the robot into a web server instead, and the student
    just clicks a link. It is also a neat thing to have shown them.
    """
    import subprocess
    os.makedirs(PHOTO_DIR, exist_ok=True)
    photos = sorted(f for f in os.listdir(PHOTO_DIR) if f.endswith(".png"))
    print(f"\n  {BOLD}Your photos, in a browser{RST}\n")
    if not photos:
        print(f"    {YLW}No photos yet — take one first (option 7).{RST}\n")
        return

    host = f"{os.uname().nodename}.local"
    port = 8000
    print(f"    {len(photos)} photo(s): {', '.join(photos[-4:])}"
          f"{' …' if len(photos) > 4 else ''}\n")
    show(f"python3 -m http.server {port} --directory {PHOTO_DIR}")
    print(f"\n    Open this on your laptop:\n")
    print(f"      {CYA}{BOLD}http://{host}:{port}/{RST}\n")
    print(f"    {DIM}Anyone on the class network can see these while it runs.{RST}")
    print(f"    {DIM}Ctrl-C here when you are done.{RST}\n")
    try:
        subprocess.run([sys.executable, "-m", "http.server", str(port),
                        "--directory", PHOTO_DIR])
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"    {RED}could not start the server: {exc}{RST}")
        print(f"    {DIM}If the port is busy, something else is already serving.{RST}")
    print(f"\n    {DIM}server stopped{RST}\n")


def act_live(px) -> None:
    print(f"\n  {BOLD}Live drive{RST}")
    print(f"  {YLW}Robot on the floor. Nothing in front of it.{RST}\n")
    print(f"    {BOLD}W{RST} forward   {BOLD}S{RST} back   {BOLD}A{RST} left   {BOLD}D{RST} right")
    print(f"    {BOLD}I K J L{RST} camera up/down/left/right")
    print(f"    {BOLD}space{RST} stop    {BOLD}Q{RST} quit\n")
    print(f"  {DIM}Hold a key to keep moving. Let go and it stops by itself.{RST}\n")

    speed = int(ask("speed", 50, 0, 100))
    pan = tilt = 0.0
    moving = False
    print(f"\n  {DIM}driving — Q to quit{RST}\n")

    try:
        with raw_keys():
            while True:
                key = key_within(LIVE_IDLE_STOP_S)
                if key is None:
                    if moving:
                        px.stop(); moving = False
                        print("    stopped        ", end="\r")
                    continue
                key = key.lower()
                if key in ("q", "\x03"):        # q or Ctrl-C
                    break
                if key == " ":
                    px.stop(); moving = False
                    print("    stop           ", end="\r"); continue
                if key in "wsad":
                    if key == "w":
                        px.set_dir_servo_angle(0); px.forward(speed); moving = True
                        print("    forward        ", end="\r")
                    elif key == "s":
                        px.set_dir_servo_angle(0); px.backward(speed); moving = True
                        print("    back           ", end="\r")
                    elif key == "a":
                        px.set_dir_servo_angle(-30); px.forward(speed); moving = True
                        print("    left           ", end="\r")
                    else:
                        px.set_dir_servo_angle(30); px.forward(speed); moving = True
                        print("    right          ", end="\r")
                elif key in "ikjl":
                    if key == "i":
                        tilt = min(65, tilt + 10)
                    elif key == "k":
                        tilt = max(-35, tilt - 10)
                    elif key == "j":
                        pan = max(-90, pan - 10)
                    else:
                        pan = min(90, pan + 10)
                    px.set_cam_pan_angle(pan); px.set_cam_tilt_angle(tilt)
                    print(f"    camera pan {pan:+.0f} tilt {tilt:+.0f}   ", end="\r")
    finally:
        px.stop()
    print("\n")


def act_config(px) -> None:
    print(f"\n  {BOLD}Calibration{RST}")
    print(f"  {DIM}Your robot's own settings, in /opt/picar-x/picar-x.conf{RST}\n")
    try:
        with open("/opt/picar-x/picar-x.conf") as fh:
            body = fh.read().strip()
        print("\n".join(f"    {line}" for line in body.splitlines()) or "    (empty)")
    except OSError as exc:
        print(f"    {YLW}could not read it: {exc}{RST}")
    print(f"\n  {DIM}All zeros? Your robot is not calibrated yet — that is why it may{RST}")
    print(f"  {DIM}pull to one side. We fix that next lecture.{RST}\n")


MENU = [
    ("Read the distance sensor", act_distance),
    ("Read the grayscale sensors", act_grayscale),
    ("Robot vitals — battery, temperature", act_vitals),
    ("Drive in a straight line", act_drive),
    ("Turn the front wheels", act_steer),
    ("Aim the camera", act_camera),
    ("Take a photo", act_photo),
    ("Show my photos in a browser", act_gallery),
    ("Live drive — WASD keys", act_live),
    ("Show my robot's calibration", act_config),
]


def main() -> int:
    print(f"\n{BOLD}ShineLabs robot playground{RST}")
    print(f"{DIM}Every option prints the Python it runs. Read along.{RST}\n")

    try:
        # robot_hat prints to stdout on import, which would scribble over the menu.
        with contextlib.redirect_stdout(sys.stderr):
            from picarx import Picarx
            px = Picarx()
    except PermissionError:
        print(f"  {RED}Cannot write /opt/picar-x.{RST}")
        print("  Run setup.sh first, or:  sudo mkdir -p /opt/picar-x && "
              f"sudo chown $USER:$USER /opt/picar-x\n")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"  {RED}Could not start the robot: {type(exc).__name__}: {exc}{RST}")
        print("  Is the Robot HAT switched on? Try running setup.sh again.\n")
        return 1

    print(f"  {GRN}Robot ready.{RST} The servos have moved to their centre positions.\n")

    try:
        while True:
            for i, (label, _) in enumerate(MENU, 1):
                print(f"    {BOLD}{i}{RST}  {label}")
            print(f"    {BOLD}0{RST}  Quit\n")
            choice = input("  choose: ").strip()
            if choice in ("0", "q", "quit", ""):
                break
            if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
                print(f"  {YLW}pick a number from 0 to {len(MENU)}{RST}\n")
                continue
            try:
                MENU[int(choice) - 1][1](px)
            except KeyboardInterrupt:
                px.stop()
                print(f"\n  {YLW}cancelled{RST}\n")
            except Exception as exc:  # noqa: BLE001 - one bad action must not end the session
                px.stop()
                print(f"\n  {RED}that failed: {type(exc).__name__}: {exc}{RST}\n")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        # Whatever happened, the robot does not get left driving.
        with contextlib.suppress(Exception):
            px.stop()
        print(f"  {DIM}motors stopped. Bye.{RST}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
