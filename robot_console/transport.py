"""Transport: SSH to the robot, JSON lines over the channel's stdio.

Responsibilities, and nothing else:

* connect with paramiko using a password typed into the GUI
* upload robot_agent.py, so the agent can never be a different version to the
  GUI that is driving it
* run it and pump newline-delimited JSON both ways
* hand every inbound message to a callback on a background thread

The GUI never touches paramiko or sockets. Everything here is non-blocking from
the GUI's point of view: outbound commands go on a queue, inbound messages
arrive via callback.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Callable

try:
    import paramiko
except ImportError:  # surfaced by the GUI with an install hint
    paramiko = None  # type: ignore[assignment]

AGENT_LOCAL = Path(__file__).resolve().parent.parent / "robot_agent.py"
AGENT_REMOTE = "/tmp/shinelabs_agent.py"

KEEPALIVE_S = 0.5      # must be comfortably under the agent's watchdog
CONNECT_TIMEOUT_S = 12.0


class TransportError(Exception):
    """Anything the student needs to read and act on."""


class RobotLink:
    """One SSH session to one robot, carrying the agent protocol."""

    def __init__(
        self,
        on_message: Callable[[dict], None],
        on_state: Callable[[str, str | None], None],
        on_log: Callable[[str], None],
    ) -> None:
        self.on_message = on_message
        self.on_state = on_state          # ("connecting"|"online"|"offline", detail)
        self.on_log = on_log
        self._client = None
        self._channel = None
        self._out: queue.Queue[dict] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._running = False

    # ------------------------------------------------------------------ #

    @property
    def connected(self) -> bool:
        return self._running

    def connect(self, host: str, user: str, password: str, port: int = 22) -> None:
        """Blocking connect. Callers run this on a worker thread."""
        if paramiko is None:
            raise TransportError(
                "paramiko is not installed.\n\n"
                "Install it with:\n    pip install paramiko"
            )
        if not AGENT_LOCAL.exists():
            raise TransportError(f"cannot find the agent to upload: {AGENT_LOCAL}")

        self.on_state("connecting", f"{user}@{host}")
        client = paramiko.SSHClient()
        # First connection to a robot is always an unknown host, and students
        # rewrite cards (which changes the key). Prompting about host keys would
        # be noise they cannot evaluate, on a closed classroom network.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host, port=port, username=user, password=password,
                timeout=CONNECT_TIMEOUT_S, banner_timeout=CONNECT_TIMEOUT_S,
                auth_timeout=CONNECT_TIMEOUT_S, look_for_keys=False, allow_agent=False,
            )
        except paramiko.AuthenticationException as exc:
            raise TransportError(
                f"{user}@{host} rejected that password.\n\n"
                "Use the username and password you set in Raspberry Pi Imager."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - socket, DNS, timeout, all the same to a student
            raise TransportError(
                f"Could not reach {host}.\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "Check the robot is switched on, and that your laptop and the "
                "robot are on the class network."
            ) from exc

        self._client = client
        self._upload_agent(client)

        transport = client.get_transport()
        transport.set_keepalive(15)         # notice a dead link rather than hanging
        channel = transport.open_session()
        # Deliberately NO get_pty(). paramiko allocates no PTY unless asked, which
        # is what we want: a PTY would merge stderr into stdout and echo input,
        # both of which would corrupt the JSON protocol. (An earlier version called
        # get_pty(False) -- but its first argument is the terminal NAME, not a
        # boolean, so that raised TypeError: object of type 'bool' has no len().)
        channel.exec_command(f"python3 -u {AGENT_REMOTE}")
        self._channel = channel

        self._running = True
        self._threads = [
            threading.Thread(target=self._read_loop, daemon=True, name="link-read"),
            threading.Thread(target=self._write_loop, daemon=True, name="link-write"),
            threading.Thread(target=self._keepalive_loop, daemon=True, name="link-keepalive"),
            threading.Thread(target=self._stderr_loop, daemon=True, name="link-stderr"),
        ]
        for t in self._threads:
            t.start()

    def _upload_agent(self, client) -> None:
        """SFTP the agent across on every connect.

        Deliberately unconditional: it removes any possibility of the robot
        running a stale agent against a newer GUI, which would be a confusing
        class of bug to debug in a classroom.
        """
        try:
            sftp = client.open_sftp()
            try:
                sftp.put(str(AGENT_LOCAL), AGENT_REMOTE)
                sftp.chmod(AGENT_REMOTE, 0o755)
            finally:
                sftp.close()
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"Connected, but could not upload the agent: {type(exc).__name__}: {exc}"
            ) from exc
        self.on_log(f"uploaded agent -> {AGENT_REMOTE}")

    # ------------------------------------------------------------------ #

    def send(self, **msg) -> None:
        if self._running:
            self._out.put(msg)

    def close(self) -> None:
        """Stop the robot, then tear the link down. Order matters."""
        if self._running:
            try:
                self._channel.sendall((json.dumps({"cmd": "stop"}) + "\n").encode())
                time.sleep(0.15)          # let it land before the channel dies
            except Exception:  # noqa: BLE001
                pass
        # Stop the loops BEFORE closing, then leave the references in place. The
        # loops each check _running and then touch _channel; setting it to None
        # here would race them into AttributeError, which surfaced as spurious
        # "send failed" log lines at the exact moment a student closes the window.
        self._running = False
        time.sleep(KEEPALIVE_S + 0.1)      # let the loops notice and exit
        for closer in (self._channel, self._client):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        self.on_state("offline", None)

    # --- loops --------------------------------------------------------- #

    def _read_loop(self) -> None:
        buf = b""
        try:
            while self._running:
                if self._channel.recv_ready():
                    chunk = self._channel.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    # Snapshots are ~0.5 MB base64 on one line, so a line can
                    # span many reads. Only split on complete lines.
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self.on_message(json.loads(line))
                        except json.JSONDecodeError:
                            self.on_log(f"unparseable line: {line[:120]!r}")
                elif self._channel.exit_status_ready() and not self._channel.recv_ready():
                    break
                else:
                    time.sleep(0.02)
        except Exception as exc:  # noqa: BLE001
            self.on_log(f"read loop ended: {type(exc).__name__}: {exc}")
        if self._running:
            self._running = False
            self.on_state("offline", "the robot closed the connection")

    def _write_loop(self) -> None:
        while self._running:
            try:
                msg = self._out.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._channel.sendall((json.dumps(msg) + "\n").encode())
            except Exception as exc:  # noqa: BLE001
                self.on_log(f"send failed: {type(exc).__name__}: {exc}")
                self._running = False
                self.on_state("offline", "lost the connection")
                return

    def _keepalive_loop(self) -> None:
        """Hold off the agent's watchdog while the GUI is alive and idle.

        If the GUI dies, these stop, the watchdog fires, and the robot stops --
        which is the entire point.
        """
        while self._running:
            self.send(cmd="keepalive")
            time.sleep(KEEPALIVE_S)

    def _stderr_loop(self) -> None:
        """The agent's diagnostics. Never parsed, only shown."""
        buf = b""
        while self._running:
            try:
                if self._channel.recv_stderr_ready():
                    buf += self._channel.recv_stderr(8192)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode(errors="replace").strip()
                        if text:
                            self.on_log(text)
                else:
                    time.sleep(0.05)
            except Exception:  # noqa: BLE001
                return
