#!/usr/bin/env python3
"""Restart the Spotify desktop client after resume from suspend, if broken.

Watches logind's PrepareForSleep signal via `gdbus monitor`. On resume,
waits for the session bus to settle, then checks whether the Spotify
MPRIS player is back. If (and only if) Spotify was running before suspend
and is still missing afterwards, kills any leftover processes and
relaunches it. The spotifyd daemon is never touched.

Usage:
    spotify_resume_fix.py              # watch for resume events forever
    spotify_resume_fix.py --check-now  # run one settle+verify+fix cycle now
"""

import os
import shutil
import signal
import subprocess
import sys
import time

TAG = "spotify-resume-fix"

SETTLE_DELAY_SEC = 15      # grace period after resume before first check
VERIFY_TIMEOUT_SEC = 60    # how long to wait for MPRIS to reappear on its own
VERIFY_POLL_SEC = 5
KILL_TIMEOUT_SEC = 10      # SIGTERM -> SIGKILL escalation window
RELAUNCH_TIMEOUT_SEC = 30  # how long to wait for MPRIS after relaunch


def log(msg):
    print(f"[{TAG}] {msg}", flush=True)


def run(args, **kwargs):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=30, **kwargs)


def spotify_on_bus():
    """True if the Spotify desktop client's MPRIS player is reachable."""
    try:
        out = subprocess.check_output(
            ["playerctl", "-l"], timeout=10, stderr=subprocess.DEVNULL
        ).decode()
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return False
    return "spotify" in out.split()


def spotify_pids():
    """PIDs of processes named exactly 'spotify' (never 'spotifyd')."""
    out = run(["pgrep", "-x", "spotify"]).stdout.strip()
    return [int(p) for p in out.split()] if out else []


def wait_for_bus(want_present, timeout, poll=VERIFY_POLL_SEC):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if spotify_on_bus() == want_present:
            return True
        time.sleep(poll)
    return spotify_on_bus() == want_present


def fix_spotify():
    """Kill leftovers (TERM, then KILL) and relaunch. True if MPRIS is back."""
    pids = spotify_pids()
    if pids:
        log(f"leftover spotify processes found (pids={pids}), terminating")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + KILL_TIMEOUT_SEC
        while spotify_pids() and time.monotonic() < deadline:
            time.sleep(0.5)
        pids = spotify_pids()
        if pids:
            log(f"processes survived SIGTERM (pids={pids}), killing")
            run(["pkill", "-9", "-x", "spotify"])
            time.sleep(2)
    else:
        log("no spotify processes left, launching fresh copy")
    log("launching spotify")
    subprocess.Popen(
        ["spotify"], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if wait_for_bus(True, RELAUNCH_TIMEOUT_SEC):
        log("Spotify player back on the bus")
        return True
    log("WARNING: relaunched Spotify but no MPRIS player appeared; "
        "manual restart may be needed")
    return False


def check_now():
    """One settle+verify+fix cycle. True if the player is up afterwards."""
    log(f"settling for {SETTLE_DELAY_SEC}s after resume")
    time.sleep(SETTLE_DELAY_SEC)
    if wait_for_bus(True, VERIFY_TIMEOUT_SEC):
        log("Spotify player healthy after resume, nothing to do")
        return True
    log("Spotify player still missing after resume, restarting client")
    return fix_spotify()


def watch():
    """Block on logind sleep signals forever; check on every resume."""
    had_player_before_sleep = None
    proc = subprocess.Popen(
        ["gdbus", "monitor", "--system",
         "--dest", "org.freedesktop.login1",
         "--object-path", "/org/freedesktop/login1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    log("watching logind for suspend/resume")
    try:
        for line in proc.stdout:
            if "PrepareForSleep" not in line:
                continue
            if "true" in line and "false" not in line:
                had_player_before_sleep = spotify_on_bus()
                log(f"suspending (spotify on bus: {had_player_before_sleep})")
            elif "false" in line:
                log("resumed from sleep")
                if had_player_before_sleep is False:
                    log("Spotify was not running before suspend, "
                        "leaving it stopped")
                elif not check_now():
                    log("resume fix failed (see WARNING above)")
                had_player_before_sleep = None
    finally:
        proc.terminate()


def main(argv):
    if "--check-now" in argv:
        return 0 if check_now() else 1
    if shutil.which("gdbus") is None:
        log("ERROR: 'gdbus' not found; cannot watch for resume events")
        return 3
    watch()
    # Monitor died unexpectedly; non-zero so systemd restarts us.
    log("ERROR: logind monitor exited, restarting")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
