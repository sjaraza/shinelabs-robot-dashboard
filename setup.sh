#!/usr/bin/env bash
#
# ShineLabs robot setup — run this ON THE ROBOT, over SSH.
#
#   curl -O https://raw.githubusercontent.com/sjaraza/shinelabs-robot-dashboard/main/setup.sh
#   less setup.sh        # read it before you run it
#   bash setup.sh
#
# Installs everything the robot needs: SunFounder's robot-hat, vilib and picar-x
# libraries, plus the one permission fix they forget.
#
#   bash setup.sh              # install anything missing, then verify
#   bash setup.sh --verify     # check only, change nothing
#   bash setup.sh --audio      # also set up the speaker (interactive)
#
# Deliberate properties:
#
# * IDEMPOTENT. Every step checks whether it is already satisfied and skips if
#   so. Safe to re-run, which matters because classroom Wi-Fi drops mid-apt.
# * IT PRINTS WHAT IT RUNS. Students see the real commands, so the script is a
#   readable recipe rather than a black box.
# * AUDIO IS OPT-IN. SunFounder's i2samp.sh is interactive and reboots the pi.
#   The dashboard does not need sound, so a script students run unattended must
#   not hang on a y/n prompt. Add --audio when you actually want the speaker.
# * NOT `set -e`. A failing verify is information; the script reports and
#   continues so you get the whole picture in one run.

set -uo pipefail

DO_VERIFY_ONLY=0
DO_AUDIO=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify|--verify-only) DO_VERIFY_ONLY=1 ;;
    --audio)                DO_AUDIO=1 ;;
    -h|--help) sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

BOLD=$'\033[1m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RST=$'\033[0m'
[[ -t 1 ]] || { BOLD=""; GRN=""; YLW=""; RED=""; DIM=""; RST=""; }

FAILED=0
step()  { printf '\n%s==> %s%s\n' "$BOLD" "$1" "$RST"; }
run()   { printf '%s    $ %s%s\n' "$DIM" "$*" "$RST"; "$@"; }
ok()    { printf '    %s✓%s %s\n' "$GRN" "$RST" "$1"; }
skip()  { printf '    %s·%s %s %s(already done)%s\n' "$GRN" "$RST" "$1" "$DIM" "$RST"; }
warn()  { printf '    %s!%s %s\n' "$YLW" "$RST" "$1"; }
bad()   { printf '    %s✗%s %s\n' "$RED" "$RST" "$1"; FAILED=1; }

REPOS=(
  "robot-hat|2.5.x|https://github.com/sunfounder/robot-hat.git"
  "vilib||https://github.com/sunfounder/vilib.git"
  "picar-x|2.1.x|https://github.com/sunfounder/picar-x.git"
)

have_python_module() { python3 -c "import $1" >/dev/null 2>&1; }

# --------------------------------------------------------------------------- #
printf '%sShineLabs robot setup%s\n' "$BOLD" "$RST"
printf '  host   : %s\n' "$(hostname)"
printf '  model  : %s\n' "$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
printf '  os     : %s\n' "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
printf '  python : %s\n' "$(python3 -V 2>&1)"
printf '  user   : %s\n' "$USER"

if [[ ! -e /proc/device-tree/model ]] || ! grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  warn "this does not look like a Raspberry Pi — continuing anyway"
fi

# --------------------------------------------------------------------------- #
if [[ $DO_VERIFY_ONLY -eq 0 ]]; then

  step "1/6  System packages"
  if [[ -f /var/lib/apt/periodic/update-success-stamp ]] &&
     [[ $(( $(date +%s) - $(stat -c %Y /var/lib/apt/periodic/update-success-stamp) )) -lt 86400 ]]; then
    skip "apt index is less than a day old"
  else
    run sudo apt-get update -qq || warn "apt update had problems; continuing"
  fi
  MISSING_PKGS=()
  for p in git python3-pip python3-setuptools python3-smbus i2c-tools; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING_PKGS+=("$p")
  done
  if [[ ${#MISSING_PKGS[@]} -eq 0 ]]; then
    skip "git, pip, setuptools, smbus, i2c-tools all present"
  else
    run sudo apt-get install -y "${MISSING_PKGS[@]}" || bad "could not install: ${MISSING_PKGS[*]}"
  fi

  step "2/6  Enable I2C"
  # The Robot HAT's ADC and PWM live on I2C. Without this, nothing reads.
  if [[ -e /dev/i2c-1 ]]; then
    skip "/dev/i2c-1 exists"
  elif command -v raspi-config >/dev/null 2>&1; then
    run sudo raspi-config nonint do_i2c 0 && ok "I2C enabled (a reboot may be needed)"
  else
    warn "raspi-config not found — enable I2C by hand if /dev/i2c-1 stays missing"
  fi

  step "3/6  SunFounder libraries"
  for entry in "${REPOS[@]}"; do
    IFS='|' read -r name branch url <<<"$entry"
    mod="${name//-/_}"
    [[ "$name" == "picar-x" ]] && mod="picarx"
    if have_python_module "$mod"; then
      skip "$name (python module '$mod' imports)"
      continue
    fi
    if [[ ! -d "$HOME/$name" ]]; then
      if [[ -n "$branch" ]]; then
        run git clone -b "$branch" "$url" --depth 1 "$HOME/$name" || { bad "clone failed: $name"; continue; }
      else
        run git clone "$url" --depth 1 "$HOME/$name" || { bad "clone failed: $name"; continue; }
      fi
    else
      skip "$HOME/$name already cloned"
    fi
    if [[ "$name" == "picar-x" ]]; then
      # --break-system-packages: Debian marks system python as externally managed
      # (PEP 668). SunFounder installs system-wide, and the agent runs with the
      # system interpreter, so a venv here would hide the library from it.
      ( cd "$HOME/$name" && run sudo pip3 install . --break-system-packages ) \
        || bad "pip install failed: $name"
    else
      ( cd "$HOME/$name" && run sudo python3 install.py ) || bad "install.py failed: $name"
    fi
  done

  step "4/6  Calibration directory"
  # picarx writes /opt/picar-x/picar-x.conf via fileDB. The directory does not
  # exist on a fresh card and creating it needs root, so an unprivileged agent
  # dies with PermissionError on the very first Picarx(). This is that fix.
  if [[ -d /opt/picar-x && -w /opt/picar-x ]]; then
    skip "/opt/picar-x exists and is writable by $USER"
  else
    run sudo mkdir -p /opt/picar-x
    run sudo chown "$USER:$USER" /opt/picar-x
    ok "/opt/picar-x is now writable by $USER"
  fi

  step "5/6  Speaker (optional)"
  if [[ $DO_AUDIO -eq 1 ]]; then
    if [[ -d "$HOME/robot-hat" ]]; then
      warn "i2samp.sh is interactive and will offer to reboot — answer its prompts"
      ( cd "$HOME/robot-hat" && run sudo bash i2samp.sh )
    else
      bad "cannot set up audio: $HOME/robot-hat is not cloned"
    fi
  else
    skip "skipped — the dashboard needs no sound. Re-run with --audio if you want it"
  fi

  step "6/6  First hardware init"
  # Constructing Picarx once here creates the config file and centres the servos,
  # so the first thing a student does is not also the first thing to fail.
  warn "the servos will move — hold the car or put it on the floor"
  python3 - <<'PY' || bad "Picarx() failed — see the traceback above"
import contextlib, sys
with contextlib.redirect_stdout(sys.stderr):
    from picarx import Picarx
    px = Picarx(); px.stop()
print("    picarx initialised, servos centred")
PY

fi

# --------------------------------------------------------------------------- #
step "Verify"

for mod in robot_hat picarx vilib; do
  ver="$(python3 -c "import ${mod} as m; print(getattr(m,'__version__','?'))" 2>/dev/null)"
  if [[ -n "$ver" ]]; then ok "${mod} ${ver}"; else bad "${mod} does not import"; fi
done

if [[ -e /dev/i2c-1 ]]; then
  addrs="$(sudo i2cdetect -y 1 2>/dev/null | awk 'NR>1{for(i=2;i<=NF;i++) if ($i ~ /^[0-9a-f]{2}$/) printf "0x%s ", $i}')"
  if [[ -n "$addrs" ]]; then ok "I2C devices: ${addrs}"; else bad "I2C bus present but nothing responds — is the HAT powered?"; fi
else
  bad "/dev/i2c-1 missing — I2C not enabled (a reboot may fix it)"
fi

if [[ -d /opt/picar-x && -w /opt/picar-x ]]; then
  ok "/opt/picar-x writable$([[ -f /opt/picar-x/picar-x.conf ]] && echo ' (config present)')"
else
  bad "/opt/picar-x is not writable by $USER — the dashboard will fail to connect"
fi

volts="$(python3 -c "
import contextlib,sys
with contextlib.redirect_stdout(sys.stderr):
    from robot_hat.device import get_battery_voltage as g
print(round(g(),2))" 2>/dev/null)"
if [[ -n "$volts" ]]; then
  # Thresholds from the HAT's own battery LEDs; 6.0 V is the pack's cutoff.
  if   (( $(echo "$volts >= 7.6"  | bc -l) )); then ok "battery ${volts} V (good)"
  elif (( $(echo "$volts >= 7.15" | bc -l) )); then warn "battery ${volts} V — getting low, charge soon"
  else                                              bad "battery ${volts} V — charge before using the robot"
  fi
else
  bad "could not read the battery voltage"
fi

if command -v rpicam-still >/dev/null 2>&1; then
  if rpicam-still --list-cameras 2>/dev/null | grep -q ':'; then ok "camera detected"
  else warn "rpicam-still present but no camera detected — check the ribbon cable"; fi
else
  warn "rpicam-still not found — the Capture button will not work"
fi

sig="$(iw dev wlan0 link 2>/dev/null | sed -n 's/.*signal: //p')"
ssid="$(iw dev wlan0 link 2>/dev/null | sed -n 's/.*SSID: //p')"
[[ -n "$ssid" ]] && ok "wifi: ${ssid} ${sig}" || warn "not associated with any wifi network"

# --------------------------------------------------------------------------- #
printf '\n'
if [[ $FAILED -eq 0 ]]; then
  printf '  %s%sREADY%s  — %s is set up.\n' "$GRN" "$BOLD" "$RST" "$(hostname)"
  printf '  Show this screen to your instructor, then open the Robot Console on your laptop.\n\n'
else
  printf '  %s%sNOT READY%s — see the %s✗%s lines above.\n' "$RED" "$BOLD" "$RST" "$RED" "$RST"
  printf '  Safe to run again: %sbash setup.sh%s\n\n' "$BOLD" "$RST"
fi
exit $FAILED
