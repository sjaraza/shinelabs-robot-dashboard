# ShineLabs Robot Console

The program you run on **your own laptop** to talk to your robot. It shows the
robot's vitals and sensors, lets you drive it, and takes photos.

```
your laptop                                    your robot
┌──────────────────────────┐                  ┌────────────────────┐
│  Robot Console (Python)  │  ──── SSH ────►  │  agent             │
│  battery · sensors       │  ◄── data ─────  │  motors · sensors   │
│  driving · camera        │                  │  camera            │
└──────────────────────────┘                  └────────────────────┘
```

---

## 1. Set up your robot

SSH into your robot first, then:

```bash
curl -O https://raw.githubusercontent.com/sjaraza/shinelabs-robot-dashboard/main/setup.sh
less setup.sh
bash setup.sh
```

Read it before you run it. That is a good habit in general, and this script uses
`sudo` — you should always know what you are giving that to.

It finishes with **READY** and a summary. Show that to your instructor.

Safe to run again if your Wi-Fi drops halfway; it skips whatever is already done.

## 2. Get the console onto your laptop

```bash
git clone https://github.com/sjaraza/shinelabs-robot-dashboard.git
cd shinelabs-robot-dashboard
python3 launch.py
```

That is the whole thing — the same three lines on macOS, Windows and Linux.
`launch.py` sets everything up the first time (about half a minute) and starts
instantly after that.

On Windows the interpreter is usually called `py`, so the last line is
`py launch.py`.

Something wrong? Ask it to check without changing anything:

```bash
python3 launch.py --check
```

## 3. Connect

Type your robot's name (for example `zoomer.local`), the username and password
you chose in Raspberry Pi Imager, and press **Connect**.

## Driving

| | |
|---|---|
| **Hold** the arrow keys | drive — release to stop |
| **Space** | STOP |
| speed slider | how fast, when you do hold a key |
| pan / tilt sliders | aim the camera |
| **Capture** | take one photo |

The robot stops the moment you let go. It also stops by itself if the console
closes, your laptop sleeps, or the Wi-Fi drops — it will not keep driving without
someone asking it to.

⚠️ **Put the robot on the floor before you drive it.** Desks are a long way down
and it lands on its camera.

---

## Things that surprise people

**Speed has no slow setting.** Ask for 1% and you get about 50%. The motor
driver's library forces any non-zero speed up to at least half power, so the
slider goes from "stopped" to "moving briskly" with nothing in between. That is
the hardware, not a bug in the console — and it is a good example of a real
constraint you have to design around.

**Distance sometimes says "no echo".** The ultrasonic sensor sends a pulse and
listens for it to bounce back. Point it at a soft surface, an angled surface, or
nothing at all, and no echo returns. The sensor reports `-1`, which is not a
distance. The console shows "no echo" and leaves a gap in the graph rather than
drawing a line to zero — because pretending to know is worse than admitting you
don't.

**Steering only goes ±30°.** Not ±90°. The linkage binds beyond that, and
forcing a servo against a stop makes it draw current and get hot.

---

## For maintainers

### Layout

```
launch.py                sets up and starts the console. One file, all platforms.
robot_agent.py           runs ON the robot. Uploaded fresh on every connect.
robot_console/
  transport.py           paramiko SSH + newline-delimited JSON
  app.py                 the Tkinter GUI
setup.sh                 provisioning, run on the robot
```

**One launcher, not one per OS.** `launch.py` is Python rather than a shell
script because the logic is identical on every platform, so there should be one
copy of it. Three shell scripts would mean three dialects and three sets of bugs,
of which only one would ever get tested. It finds the interpreter, checks tkinter
(bundled on macOS and Windows, a separate package on Linux), creates a `.venv`
with `--system-site-packages` so tkinter stays visible, works around PEP 668 on
Debian and Ubuntu, installs paramiko once, and then starts the console.

### Protocol

Newline-delimited JSON over the SSH channel's stdin/stdout — one object per line,
no framing of its own.

| Direction | Example |
|---|---|
| console → agent | `{"cmd":"drive","speed":40,"steer":-10}` |
| console → agent | `{"cmd":"cam","pan":20,"tilt":-5}` · `{"cmd":"snap"}` · `{"cmd":"stop"}` · `{"cmd":"keepalive"}` |
| agent → console | `{"type":"hello",…}` once, then `{"type":"telemetry",…}` at 2 Hz |
| agent → console | `{"type":"ack"}` · `{"type":"event"}` · `{"type":"snap"}` · `{"type":"error"}` |

### Decisions worth knowing before changing anything

**The agent is uploaded on every connect**, by SFTP to `/tmp/`. It can therefore
never be a different version from the GUI driving it. Version skew between two
machines is a miserable thing to debug in a classroom.

**stdout is the protocol, so nothing else may print to it.** The vendor libraries
do print — `robot_hat` logs, and `reset_mcu` shells out — so importing and
constructing `Picarx` happens inside `contextlib.redirect_stdout(sys.stderr)`.
Corrupting the stream is the easiest way to break this.

**Two independent stop mechanisms.** Releasing a key sends `stop`; separately the
agent stops the motors if no command arrives for 1.5 s, and the console sends a
keepalive every 0.5 s to hold that off. If the console dies, keepalives stop, the
watchdog fires. A robot must not outlive the thing driving it.

**Photos are PNG, captured to stdout.** PNG because Tkinter's `PhotoImage`
decodes it natively, so Pillow is not needed on twenty student laptops; to stdout
rather than a file so repeated clicking does not hammer the SD card. Verified:
`PhotoImage(data=<base64>)` renders a real capture without Pillow installed.

**`-1` from the ultrasonic is converted to `null` plus a `distance_timeout` flag**
before it reaches the GUI, so it cannot be plotted as a distance by accident.

**Sim mode is loud.** If `picarx` will not import, the agent runs with a stub so
the protocol still works — and says `mode: sim` in its hello, which the console
displays in amber with a warning. A robot that silently pretends to drive would be
worse than one that fails.

**`/opt/picar-x` needs to exist and be writable.** `picarx` writes its calibration
there through `fileDB`, the directory is absent on a fresh card, and creating it
needs root — so an unprivileged agent dies with `PermissionError` on the first
`Picarx()`. `setup.sh` fixes it; it is not optional.

### Testing without a robot

The agent falls back to a simulator, so the whole protocol can be exercised
locally:

```bash
python3 robot_agent.py       # then type JSON lines on stdin
{"cmd":"drive","speed":40,"steer":-10}
{"cmd":"snap"}
```

### Verification status

✅ Protocol tested end to end against the simulated agent: hello, telemetry,
clamping, the `-1` timeout path, unknown commands, the camera failure path,
watchdog firing after 1.5 s, and a clean exit on EOF.

✅ `launch.py` tested from clean and warm states, and with `--check`, which
provably changes nothing: creates the venv, installs paramiko 5.0.0, and keeps
tkinter visible through `--system-site-packages`.

✅ GUI smoke-tested headlessly with synthetic telemetry, including a real PNG
through `PhotoImage`, sim-mode warning, disconnect reset, and the low-battery and
cliff colour states.

⚠️ **Never run against a real robot.** paramiko, SFTP upload, real sensors, real
motors, `rpicam-still` timings and the watchdog under genuine network loss are all
untested on hardware.

## Licence

The Pi-side agent imports SunFounder's `picar-x` (GPL-2.0) and `robot-hat`
(GPL-3.0). Licensing for this repo is not yet decided.
