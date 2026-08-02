#!/usr/bin/env python3
"""Set up and start the ShineLabs Robot Console. One file, every platform.

    python3 launch.py            set up if needed, then start
    python3 launch.py --check    check the environment, change nothing

On Windows the interpreter is usually called `py`:

    py launch.py

Why a Python launcher rather than a shell script per platform: the logic is
identical everywhere, so there should be one copy of it. Shell scripts would mean
three files, three dialects and three sets of bugs, and only one of them would
ever get tested. This runs on the interpreter we already require.

What it handles, all of which students otherwise hit by hand:

  * python3 / python / py -- whatever this was launched with is what gets used
  * tkinter, which is bundled on macOS and Windows but a separate package on Linux
  * PEP 668: Debian and Ubuntu refuse `pip install` outside a virtual
    environment, which is the most common way this goes wrong
  * installing paramiko, once, into a local .venv

Second and later runs skip everything and start immediately.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
MIN_PYTHON = (3, 9)

# Progress should appear as it happens, not in one burst at the end. Python
# block-buffers stdout when it is not a terminal, which hides everything if this
# is piped to a log or run from a GUI file manager.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, OSError):
    pass

# Colour only when attached to a terminal that will render it. Windows consoles
# have understood ANSI since Windows 10, and if it does not, plain text is fine.
_tty = sys.stdout.isatty()
BOLD = "\033[1m" if _tty else ""
GRN = "\033[32m" if _tty else ""
YLW = "\033[33m" if _tty else ""
RED = "\033[31m" if _tty else ""
DIM = "\033[2m" if _tty else ""
RST = "\033[0m" if _tty else ""


def ok(msg: str) -> None:
    print(f"  {GRN}[ok]{RST} {msg}")


def warn(msg: str) -> None:
    print(f"  {YLW}[!]{RST} {msg}")


def die(headline: str, *lines: str) -> None:
    print(f"\n  {RED}[x] {headline}{RST}\n")
    for line in lines:
        print(f"      {line}")
    print()
    hold_window()
    sys.exit(1)


def hold_window() -> None:
    """Keep a double-clicked window open long enough to read the error."""
    try:
        input("Press Enter to close... ")
    except (EOFError, KeyboardInterrupt):
        pass


def venv_python() -> Path:
    """Interpreter inside the venv. Windows puts it somewhere different."""
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def has_module(python: Path | str, module: str) -> bool:
    try:
        return subprocess.run([str(python), "-c", f"import {module}"],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    print(f"\n{BOLD}ShineLabs Robot Console{RST}\n")

    # --- 1. this interpreter ------------------------------------------- #
    if sys.version_info < MIN_PYTHON:
        die(f"Python {'.'.join(map(str, MIN_PYTHON))} or newer is needed.",
            f"This is {sys.version.split()[0]} at {sys.executable}",
            "Install a current Python from https://www.python.org/downloads/",
            "On Windows, tick 'Add python.exe to PATH' in the installer.")
    ok(f"python {sys.version.split()[0]}  ({sys.executable})")

    # --- 2. tkinter ---------------------------------------------------- #
    try:
        import tkinter
    except ImportError:
        die("Python is installed but tkinter is missing.",
            "tkinter draws the window, so nothing can run without it.",
            "Linux:   sudo apt install python3-tk",
            "macOS:   install Python from python.org, which bundles it",
            "Windows: reinstall from python.org, which bundles it")
    tk_version = float(tkinter.TkVersion)
    ok(f"tkinter {tk_version}")
    if tk_version < 8.6:
        # PhotoImage only decodes PNG from 8.6, and photos arrive as PNG.
        warn("Tk is older than 8.6 — everything works except displaying photos")

    # --- 3. virtual environment ---------------------------------------- #
    vpy = venv_python()
    if not vpy.exists():
        if check_only:
            warn("no virtual environment yet — a normal run would create one")
        else:
            print(f"\n  {DIM}First run: setting up. About half a minute.{RST}\n")
            # --system-site-packages so the venv can see tkinter, which cannot be
            # pip-installed.
            result = subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", str(VENV)],
                capture_output=True, text=True)
            if result.returncode != 0:
                die("Could not create a virtual environment.",
                    "Linux: sudo apt install python3-venv, then run this again.",
                    (result.stderr or "").strip().splitlines()[-1] if result.stderr else "")
            ok(f"created {VENV.name}")
    else:
        ok("virtual environment present")

    # --- 4. paramiko --------------------------------------------------- #
    if vpy.exists():
        if has_module(vpy, "paramiko"):
            ok("paramiko installed")
        elif check_only:
            warn("paramiko not installed — a normal run would install it")
        else:
            print("  installing paramiko...")
            subprocess.run([str(vpy), "-m", "pip", "install", "--quiet",
                            "--upgrade", "pip"], capture_output=True)
            result = subprocess.run(
                [str(vpy), "-m", "pip", "install", "--quiet", "-r",
                 str(HERE / "requirements.txt")], capture_output=True, text=True)
            if result.returncode != 0:
                die("Could not install paramiko.",
                    "Are you online? The console needs it to reach the robot over SSH.",
                    (result.stderr or "").strip().splitlines()[-1] if result.stderr else "")
            ok("paramiko installed")

        if not has_module(vpy, "tkinter"):
            die("The virtual environment cannot see tkinter.",
                f"Delete {VENV.name} and run this again — it needs --system-site-packages.",
                "On Linux also: sudo apt install python3-tk")

    # --- 5. go --------------------------------------------------------- #
    if check_only:
        print(f"\n  {GRN}Environment looks fine.{RST} "
              f"Run '{Path(sys.executable).name} launch.py' to start.\n")
        return 0

    print(f"\n  {BOLD}Starting the console...{RST}\n")
    status = subprocess.run([str(vpy), "-m", "robot_console"], cwd=str(HERE)).returncode
    if status != 0:
        print(f"\n  {RED}The console exited with an error (code {status}).{RST}")
        print("  Show this window to your instructor.\n")
        hold_window()
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
