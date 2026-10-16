#!/usr/bin/env python3
"""Restart the Spotify desktop client after resume from suspend.

Simple fixed sequence (no watchdog, no retry budgets):

1. On resume, wait 1 min (let the session settle).
2. Kill spotify.
3. Wait 30 sec.
4. Launch spotify again.
5. After 2 more min, check the MPRIS player the screensaver uses
   (`playerctl -l` contains "spotify"); if missing, restart again.
6. After 5 more min, check again; if missing, restart again.
7. Done.

If Spotify was not running before suspend, nothing is done.

Usage:
    spotify_resume_fix.py              # watch for resume events forever
    spotify_resume_fix.py --check-now  # run the resume sequence once now
"""

import shutil
import subprocess
import sys
import threading
import time

TAG = "spotify-resume-fix"

WAIT_AFTER_RESUME_SEC = 60   # 1 min settle before touching spotify
WAIT_AFTER_KILL_SEC = 30     # 30 sec between kill and relaunch
FIRST_CHECK_DELAY_SEC = 120  # 2 min after relaunch: first health check
SECOND_CHECK_DELAY_SEC = 300  # 5 min later: second health check


def log(msg):
    print(f"[{TAG}] {msg}", flush=True)


def spotify_on_bus():
    """True if the Spotify MPRIS player (used by the screensaver) is up."""
    try:
        out = subprocess.check_output(
            ["playerctl", "-l"], timeout=10, stderr=subprocess.DEVNULL
        ).decode()
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return False
    return "spotify" in out.split()


def kill_spotify():
    """Kill leftover spotify processes (never spotifyd)."""
    log("killing spotify")
    subprocess.run(["pkill", "-x", "spotify"],
                   capture_output=True, timeout=30)
    time.sleep(5)
    # Escalate anything that survived SIGTERM.
    subprocess.run(["pkill", "-9", "-x", "spotify"],
                   capture_output=True, timeout=30)


def launch_spotify():
    """Launch the Spotify desktop client detached."""
    log("launching spotify")
    subprocess.Popen(
        ["spotify"], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def restart_spotify():
    """Kill, wait 30s, then launch again."""
    kill_spotify()
    log(f"waiting {WAIT_AFTER_KILL_SEC}s before relaunch")
    time.sleep(WAIT_AFTER_KILL_SEC)
    launch_spotify()


def check_and_fix(label):
    """Check MPRIS; restart once if the player is missing. Returns bool up."""
    if spotify_on_bus():
        log(f"check {label}: Spotify player up, nothing to do")
        return True
    log(f"check {label}: Spotify player missing, restarting")
    restart_spotify()
    return False


def handle_resume():
    """The full post-resume sequence. Blocks ~8.5 min, so run in a thread."""
    log(f"resumed, waiting {WAIT_AFTER_RESUME_SEC}s before restart")
    time.sleep(WAIT_AFTER_RESUME_SEC)
    restart_spotify()
    log(f"waiting {FIRST_CHECK_DELAY_SEC}s before first health check")
    time.sleep(FIRST_CHECK_DELAY_SEC)
    check_and_fix("first (2 min after relaunch)")
    log(f"waiting {SECOND_CHECK_DELAY_SEC}s before second health check")
    time.sleep(SECOND_CHECK_DELAY_SEC)
    check_and_fix("second (5 min later)")
    log("resume sequence done")


def watch():
    """Block on logind sleep signals forever; run the sequence per resume."""
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
                else:
                    threading.Thread(
                        target=handle_resume, daemon=True).start()
                had_player_before_sleep = None
    finally:
        proc.terminate()


def main(argv):
    if "--check-now" in argv:
        handle_resume()
        return 0
    if shutil.which("gdbus") is None:
        log("ERROR: 'gdbus' not found; cannot watch for resume events")
        return 3
    watch()
    # Monitor died unexpectedly; non-zero so systemd restarts us.
    log("ERROR: logind monitor exited, restarting")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
