"""Spotify screensaver: shows current Spotify track after X idle timeout.

No pynput dependency: activity is detected purely by polling xprintidle,
so the service works without an X-listener race and without DISPLAY at import time.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from io import BytesIO
from urllib.parse import unquote, urlparse

from PIL import Image, ImageTk, ImageFilter, ImageOps, ImageEnhance, ImageDraw, ImageFont
import requests

IDLE_THRESHOLD_MS = 180000
# How long input must persist before it dismisses a visible screensaver.
# Without this, one stray input event hides it and you wait the full
# threshold again; see idle_checker.
HIDE_GRACE_SEC = 0.3
# Pointer must move this far (pixels) from where it was when the
# screensaver appeared to count as genuine mouse activity. Touchpad sensor
# noise and tiny bumps reset xprintidle's counter with only 1-2px of
# motion, which used to hide the screensaver with nobody at the desk.
MOUSE_MOVE_THRESHOLD_PX = 10
# When the pointer has NOT moved, a drop in the X idle counter alone is not
# enough: any single event (jitter, lid, resume rebase) makes idle grow
# passively afterwards. Only a *fresh* reset during the grace window --
# idle growing noticeably less than wall-clock time elapsed -- proves the
# user is still there (second keypress, auto-repeat, continued motion).
FRESH_INPUT_TOLERANCE_MS = 250
# How often the Tk thread samples the pointer position for the gate above.
POINTER_POLL_MS = 500
# Startup / reconnect delay when there is no X server (yet).
STARTUP_RETRY_SEC = 5
BKG_BLUR_RADIUS = 8
BKG_DARKNESS = 0.55
TEXT_FONT_SIZE = 108
TITLE_FONT_SIZE = 108
ARTIST_FONT_SIZE = 108
MIN_FONT_SIZE = 82       # never shrink track text below this
FONT_SCALE_STEP = 0.02   # granularity when searching for a fitting size
TITLE_ARTIST_GAP = 36
MIN_TITLE_ARTIST_GAP = 12
TEXT_HEIGHT_MARGIN = 60  # keep the text block this far from screen edges
TEXT_SHADOW_OFFSET = 6
TEXT_SHADOW_BLUR = 8
TEXT_SHADOW_OPACITY = 150
is_screensaver_active = False
screensaver = None
root = None
canvas = None
album_art_image = None
album_art_pil = None
bg_photo = None
text_photo = None
prev_title = None
prev_artist = None
last_player_gone = False
# Pointer position (x, y) sampled in the Tk thread. show_pointer is frozen
# when the screensaver appears; latest_pointer tracks the live position.
# The idle-checker thread only reads these (never calls Tk itself).
show_pointer = None
latest_pointer = None
shutdown_requested = threading.Event()
DEBUG = False


def log(msg):
    print(f"[spotify-screensaver] {msg}", flush=True)


def debug(msg):
    if DEBUG:
        log(f"DEBUG: {msg}")


def get_idle_time():
    """Return X idle time in ms, or None if it could not be determined.

    None (instead of 0) on error is load-bearing: 0 means "user just gave
    input" to the idle checker, so returning 0 on a transient xprintidle
    failure would falsely hide a visible screensaver.
    Never raises.
    """
    try:
        out = subprocess.check_output(["xprintidle"], timeout=5).strip()
        return int(out)
    except FileNotFoundError:
        log("ERROR: 'xprintidle' not found. Is it installed? (sudo apt install xprintidle)")
        return None
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired) as e:
        log(f"ERROR: xprintidle failed: {e}")
        return None


def _playerctl(*args):
    # stderr=DEVNULL: playerctl's own "No players found" message would
    # otherwise spam the journal on every poll during an outage.
    return subprocess.check_output(
        ["playerctl"] + list(args), timeout=5, stderr=subprocess.DEVNULL
    ).decode().strip()


def get_spotify_info():
    """Return (title, artist, art_url), or None if no player is reachable.

    None means there is no MPRIS player on the bus at all (e.g. the Spotify
    client crashed or lost its bus connection, as happens across suspend).
    An answered-but-empty player still yields "Nothing Playing".
    """
    try:
        # Prefer the spotify player explicitly; fall back to default player.
        for prefix in (["-p", "spotify"], []):
            try:
                title = _playerctl(*prefix, "metadata", "title")
                artist = _playerctl(*prefix, "metadata", "artist")
                art_url = _playerctl(*prefix, "metadata", "mpris:artUrl")
                if title:
                    return title, artist, art_url
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        if _playerctl("-l"):
            return "Nothing Playing", "", ""
        return None
    except FileNotFoundError:
        log("ERROR: 'playerctl' not found. Is it installed? (sudo apt install playerctl)")
        return None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def download_album_art(url):
    global album_art_image, album_art_pil
    album_art_image = None
    album_art_pil = None
    if not url:
        return
    try:
        if url.startswith("file://"):
            # Legacy snap layout cached art as file:// URLs; deb uses https.
            # urlparse handles %-encoding and localhost prefixes.
            parsed = urlparse(url)
            path = unquote(parsed.path)
            debug(f"loading local album art: {path}")
            img = Image.open(path)
        else:
            debug(f"downloading album art: {url}")
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content))
        img = img.convert("RGB")
        album_art_pil = img.copy()
        thumb = img.resize((600, 600), Image.Resampling.LANCZOS)
        album_art_image = ImageTk.PhotoImage(thumb)
    except Exception as e:
        log(f"Error loading album art: {e}")


def _load_text_font(size, bold=False):
    """Load a sans font for the track text. DejaVu ships with Mint."""
    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _layout_track_text(measurer, title, artist, max_width, title_size,
                       artist_size, gap):
    """Wrap the text blocks and measure them.

    Returns (laid_out, text_h) where laid_out is one
    (lines, font, line_height, width) tuple per block.
    """
    title_font = _load_text_font(title_size, bold=True)
    artist_font = _load_text_font(artist_size, bold=False)
    blocks = [(title, title_font)]
    if artist:
        blocks.append((artist, artist_font))
    laid_out = []
    for text, font in blocks:
        lines = _wrap_text(measurer, text, font, max_width)
        ascent, descent = font.getmetrics()
        line_height = ascent + descent + 8
        width = max((measurer.textlength(line, font=font) for line in lines),
                    default=0)
        laid_out.append((lines, font, line_height, width))
    text_h = sum(line_height * len(lines)
                 for lines, _, line_height, _ in laid_out)
    text_h += gap * (len(laid_out) - 1)
    return laid_out, text_h


def _fit_track_text(measurer, title, artist, max_width, max_height,
                    title_size, artist_size, gap):
    """Shrink the fonts (and gap) until the block fits max_height.

    Long titles wrap into more lines, so the block can grow taller than
    the screen even though the width always fits. The largest size that
    still fits is used, down to MIN_FONT_SIZE; below that the text
    wraps and may still overflow, which beats rendering it unreadably
    small.
    """
    def layout(ts, as_, g):
        return _layout_track_text(measurer, title, artist, max_width,
                                  ts, as_, g)

    def scaled(scale):
        return (max(MIN_FONT_SIZE, int(title_size * scale)),
                max(MIN_FONT_SIZE, int(artist_size * scale)),
                max(MIN_TITLE_ARTIST_GAP, int(gap * scale)))

    laid_out, text_h = layout(title_size, artist_size, gap)
    if max_height is None or text_h <= max_height:
        return laid_out, text_h
    # Jump near the fitting size, then walk back up to the largest one
    # that still fits.
    scale = min(1.0, max(MIN_FONT_SIZE / title_size, max_height / text_h))
    scale, laid_out, text_h = scale, *layout(*scaled(scale))
    while scale < 1.0:
        bigger = min(1.0, scale + FONT_SCALE_STEP)
        trial, trial_h = layout(*scaled(bigger))
        if trial_h > max_height:
            break
        scale, laid_out, text_h = bigger, trial, trial_h
    return laid_out, text_h


def make_track_text(title, artist, max_width, title_size=None,
                    artist_size=None, gap=None, offset=None, blur=None,
                    opacity=None, max_height=None):
    """Render title (bold) stacked over artist (regular) with one soft shadow.

    max_height, when given, caps the block height: the fonts are shrunk
    down to MIN_FONT_SIZE until it fits.

    Returns a transparent RGBA PhotoImage (caller must keep a reference).
    Must be called with a Tk instance alive.
    """
    if title_size is None:
        title_size = TITLE_FONT_SIZE
    if artist_size is None:
        artist_size = ARTIST_FONT_SIZE
    if gap is None:
        gap = TITLE_ARTIST_GAP
    if offset is None:
        offset = TEXT_SHADOW_OFFSET
    if blur is None:
        blur = TEXT_SHADOW_BLUR
    if opacity is None:
        opacity = TEXT_SHADOW_OPACITY
    measurer = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    laid_out, text_h = _fit_track_text(measurer, title, artist, max_width,
                                       max_height, title_size, artist_size,
                                       gap)
    text_w = max((width for _, _, _, width in laid_out), default=0)
    pad = offset + blur * 2
    img = Image.new("RGBA", (int(text_w + pad * 2), text_h + pad * 2),
                    (0, 0, 0, 0))
    # Shadow layer: all blocks in black, then blurred once for softness.
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    y = pad
    for lines, font, line_height, _ in laid_out:
        for line in lines:
            line_w = shadow_draw.textlength(line, font=font)
            x = (img.width - line_w) / 2
            shadow_draw.text((x + offset, y + offset), line, font=font,
                             fill=(0, 0, 0, opacity))
            y += line_height
        y += gap
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur))
    img = Image.alpha_composite(img, shadow)
    # Crisp white text on top.
    draw = ImageDraw.Draw(img)
    y = pad
    for lines, font, line_height, _ in laid_out:
        for line in lines:
            line_w = draw.textlength(line, font=font)
            x = (img.width - line_w) / 2
            draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
            y += line_height
        y += gap
    return ImageTk.PhotoImage(img)


def make_text_with_shadow(text, max_width, font_size=None, offset=None,
                          blur=None, opacity=None):
    """Render centered white text with a soft blurred drop shadow.

    Returns a transparent RGBA PhotoImage (caller must keep a reference).
    Must be called with a Tk instance alive.
    """
    if font_size is None:
        font_size = TEXT_FONT_SIZE
    if offset is None:
        offset = TEXT_SHADOW_OFFSET
    if blur is None:
        blur = TEXT_SHADOW_BLUR
    if opacity is None:
        opacity = TEXT_SHADOW_OPACITY
    font = _load_text_font(font_size)
    measurer = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    lines = _wrap_text(measurer, text, font, max_width)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent + 8
    text_h = line_height * len(lines)
    text_w = max((measurer.textlength(line, font=font) for line in lines),
                 default=0)
    pad = offset + blur * 2
    img = Image.new("RGBA", (int(text_w + pad * 2), text_h + pad * 2),
                    (0, 0, 0, 0))
    # Shadow layer: black text, then blurred for softness.
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    y = pad
    for line in lines:
        line_w = shadow_draw.textlength(line, font=font)
        x = (img.width - line_w) / 2
        shadow_draw.text((x + offset, y + offset), line, font=font,
                         fill=(0, 0, 0, opacity))
        y += line_height
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur))
    img = Image.alpha_composite(img, shadow)
    # Crisp white text on top.
    draw = ImageDraw.Draw(img)
    y = pad
    for line in lines:
        line_w = draw.textlength(line, font=font)
        x = (img.width - line_w) / 2
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height
    return ImageTk.PhotoImage(img)


def make_blurred_background(pil_img, width, height, blur_radius=None, darkness=None):
    """Cover-fit pil_img to (width, height), blur and darken it.

    Returns a PhotoImage sized to the canvas, or None if sizing failed.
    Must be called with a Tk instance alive; caller must keep a reference
    to the returned PhotoImage.
    """
    if blur_radius is None:
        blur_radius = BKG_BLUR_RADIUS
    if darkness is None:
        darkness = BKG_DARKNESS
    if pil_img is None or width < 2 or height < 2:
        return None
    fitted = ImageOps.fit(pil_img, (width, height), Image.Resampling.LANCZOS)
    blurred = fitted.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    if darkness < 1.0:
        blurred = ImageEnhance.Brightness(blurred).enhance(darkness)
    return ImageTk.PhotoImage(blurred)



def pointer_distance(a, b):
    """Pixel distance between two (x, y) tuples, or None if unknown."""
    if a is None or b is None:
        return None
    try:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    except (TypeError, IndexError):
        return None


def _sample_pointer_tk():
    """Read the pointer via Tk. Must run in the Tk thread. Never raises."""
    try:
        if root is None:
            return None
        return (root.winfo_pointerx(), root.winfo_pointery())
    except Exception:
        return None


def _pointer_poller():
    """Keep latest_pointer fresh. Runs as a Tk after() loop, so a dead
    display ends the chain silently and the reconnect logic takes over."""
    global latest_pointer
    try:
        if shutdown_requested.is_set():
            return
        if root is not None:
            latest_pointer = _sample_pointer_tk()
            root.after(POINTER_POLL_MS, _pointer_poller)
    except (tk.TclError, RuntimeError):
        # Display gone or mainloop ending: stop polling, reconnect handles it.
        pass
    except Exception as e:
        log(f"ERROR in pointer poller: {e}")
        try:
            if root is not None and not shutdown_requested.is_set():
                root.after(POINTER_POLL_MS, _pointer_poller)
        except Exception:
            pass


def _tk_callback_error(exc, val, tb):
    # A rendering bug must never kill the mainloop (previous "hang" symptom
    # was the flip side: a dead callback thread). Log and stay alive.
    try:
        log(f"ERROR in Tk callback: {exc.__name__}: {val}")
    except Exception:
        pass


def _root_after_safe(fn, *args):
    """Schedule fn on the Tk thread. False if the display is gone."""
    try:
        if root is None or shutdown_requested.is_set():
            return False
        root.after(0, fn, *args)
        return True
    except (tk.TclError, RuntimeError):
        return False


def show_screensaver(idle_ms=None):
    global screensaver, canvas, show_pointer, latest_pointer
    try:
        if screensaver is None:
            screensaver = tk.Toplevel(root)
            screensaver.attributes('-fullscreen', True)
            screensaver.configure(bg='black', cursor='none')
            canvas = tk.Canvas(screensaver, bg='black', highlightthickness=0,
                               cursor='none')
            canvas.pack(fill='both', expand=True)
        # Freeze the pointer baseline *before* showing: any later motion is
        # measured against where the mouse was when nobody was at the desk.
        pos = _sample_pointer_tk()
        show_pointer = pos
        latest_pointer = pos
        dist_note = f"pointer={pos}" if pos else "pointer=unknown"
        log(f"idle threshold reached, showing screensaver (idle={idle_ms}ms, "
            f"{dist_note})")
        screensaver.deiconify()
        screensaver.lift()
        update_display()
    except (tk.TclError, RuntimeError) as e:
        log(f"ERROR showing screensaver (display gone?): {e}")


def update_display():
    global prev_title, prev_artist, last_player_gone
    try:
        _update_display_inner()
    except (tk.TclError, RuntimeError) as e:
        # Display went away mid-render (suspend / X restart). The outer
        # reconnect loop will rebuild the UI; just stop this refresh chain.
        log(f"display lost during update, stopping refresh: {e}")
    except Exception as e:
        # A rendering bug must never kill the Tk mainloop. Log, keep the
        # old frame, and retry on the next tick.
        log(f"ERROR in update_display: {e}")
        try:
            if screensaver is not None and not shutdown_requested.is_set():
                screensaver.after(10000, update_display)
        except Exception:
            pass


def _update_display_inner():
    global prev_title, prev_artist, last_player_gone
    info = get_spotify_info()
    if info is None:
        player_gone = True
        title, artist, art_url = (
            "Spotify not responding", "restart the Spotify app", "")
    else:
        player_gone = False
        title, artist, art_url = info
    if player_gone != last_player_gone:
        # Log outages once per transition, not on every 10s refresh.
        log("lost contact with Spotify player (no MPRIS player found)"
            if player_gone else "Spotify player back")
        last_player_gone = player_gone
    if prev_title == title and prev_artist == artist:
        screensaver.after(10000, update_display)
        return

    prev_title = title
    prev_artist = artist

    global canvas, album_art_image, album_art_pil, bg_photo, text_photo
    download_album_art(art_url)

    canvas.delete("all")
    canvas.update()  # Refresh dimensions
    cw, ch = canvas.winfo_width(), canvas.winfo_height()
    if cw < 2 or ch < 2:
        # Fullscreen window not yet laid out; fall back to screen size.
        cw = screensaver.winfo_screenwidth()
        ch = screensaver.winfo_screenheight()

    # Default background is plain black; if album art exists, cover the
    # whole canvas with a heavily blurred copy of the same image.
    bg_photo = None
    if album_art_pil is not None:
        try:
            bg_photo = make_blurred_background(album_art_pil, cw, ch)
        except Exception as e:
            log(f"Error blurring background: {e}")
            bg_photo = None
    if bg_photo is not None:
        canvas.create_image(cw // 2, ch // 2, anchor='center', image=bg_photo)

    if album_art_image:
        album_x = cw // 4
        album_y = ch // 2
        canvas.create_image(album_x, album_y, anchor='center', image=album_art_image)

    text_x = (3 * cw) // 4
    text_y = ch // 2
    wrap_width = cw // 2 - 100
    # Vertical breathing room so the block never runs off the screen.
    max_height = ch - TEXT_HEIGHT_MARGIN * 2
    text_photo = None
    try:
        text_photo = make_track_text(title, artist, wrap_width,
                                     max_height=max_height)
    except Exception as e:
        log(f"Error rendering text: {e}")
        text_photo = None
    if text_photo is not None:
        canvas.create_image(text_x, text_y, anchor='center', image=text_photo)

    # Schedule next update
    screensaver.after(10000, update_display)



def close_screensaver(idle_ms=None, reason=None):
    global is_screensaver_active
    try:
        if screensaver:
            screensaver.withdraw()
    except (tk.TclError, RuntimeError):
        pass
    if is_screensaver_active:
        detail = f" ({reason})" if reason else ""
        log(f"activity detected, hiding screensaver "
            f"(idle={idle_ms}ms{detail})")
    is_screensaver_active = False


def _should_hide(now, armed_at, armed_idle, idle, dist,
                 hide_grace_sec, mouse_threshold_px, fresh_tolerance_ms):
    """Pure hide decision, unit-testable without X or Tk.

    Returns (hide, reason, stray) where hide demands a real user return
    (significant pointer motion, or a *fresh* idle reset proving continued
    input), and stray marks a grace window that expired with only passive
    idle growth (single jitter / resume rebase: stay visible, re-baseline).
    """
    elapsed = now - armed_at
    if elapsed < hide_grace_sec:
        return False, None, False
    moved = (dist is not None and dist >= mouse_threshold_px)
    if moved:
        return True, f"pointer moved {dist:.0f}px", False
    expected = armed_idle + elapsed * 1000.0
    if idle < expected - fresh_tolerance_ms:
        return True, (f"fresh input (idle {armed_idle}ms -> {idle}ms in "
                      f"{elapsed:.1f}s)"), False
    return False, None, True


def idle_checker(poll_interval=2, threshold_ms=None, hide_grace_sec=None,
                 mouse_threshold_px=None, fresh_tolerance_ms=None,
                 stop_event=None, tk_root=None):
    """Single activity source: xprintidle, gated by pointer + fresh input.

    Showing is unchanged: idle rising past threshold activates.

    Hiding needs proof the user is back, because xprintidle resets on ANY
    X input event -- including the Synaptics touchpad's 1px sensor noise,
    a bumped laptop, or a suspend/resume that rebases the counter to 0.
    The old 0.3s grace alone could not filter those: after ONE stray event
    idle stays below threshold for the full threshold duration, so the
    grace always expired and the screensaver hid, forcing another full
    wait. Instead a below-threshold poll only *arms* suspicion; hiding
    additionally requires, once the grace expires:

    * the pointer moved MOUSE_MOVE_THRESHOLD_PX from where it was when
      the screensaver appeared (real mouse use), or
    * the idle counter was *freshly* reset during the window (idle grew
      far less than wall-clock time: a second keypress, auto-repeat,
      continued motion).

    A lone event grows passively (idle ~= armed_idle + elapsed) and is
    logged once as "ignored stray input" while the screensaver STAYS
    visible -- no more full-threshold re-wait. The baseline then
    re-arms, so a real return seconds later still hides promptly.
    A failed poll (None) never arms or hides.
    """
    global is_screensaver_active, show_pointer
    if threshold_ms is None:
        threshold_ms = IDLE_THRESHOLD_MS
    if hide_grace_sec is None:
        hide_grace_sec = HIDE_GRACE_SEC
    if mouse_threshold_px is None:
        mouse_threshold_px = MOUSE_MOVE_THRESHOLD_PX
    if fresh_tolerance_ms is None:
        fresh_tolerance_ms = FRESH_INPUT_TOLERANCE_MS
    if stop_event is None:
        stop_event = shutdown_requested
    if tk_root is None:
        tk_root = root
    # Monotonic stamp + idle value of the arming poll, or None when no
    # dismissal is pending.
    activity_since = None
    activity_idle = None
    stray_episode = False
    log(f"idle checker started (threshold={threshold_ms}ms, "
        f"poll={poll_interval}s, hide_grace={hide_grace_sec}s, "
        f"mouse_threshold={mouse_threshold_px}px)")
    while not stop_event.is_set():
        # Default cadence, shortened below when a dismissal is pending so the
        # grace period is honoured to the millisecond instead of being
        # quantised to poll_interval.
        sleep_for = poll_interval
        try:
            idle = get_idle_time()
            if idle is None:
                debug("idle=unknown (xprintidle error, keeping state "
                      f"active={is_screensaver_active})")
            elif idle >= threshold_ms:
                if activity_since is not None:
                    debug("idle recovered above threshold, "
                          "dismissal disarmed")
                activity_since = None
                activity_idle = None
                stray_episode = False
                debug(f"idle={idle}ms active={is_screensaver_active}")
                if not is_screensaver_active:
                    if _root_after_safe(show_screensaver, idle):
                        is_screensaver_active = True
                    else:
                        debug("cannot schedule show: display gone")
            elif is_screensaver_active:
                now = time.monotonic()
                dist = pointer_distance(show_pointer, latest_pointer)
                if activity_since is None:
                    activity_since = now
                    activity_idle = idle
                    debug(f"possible input (idle={idle}ms, "
                          f"pointer_dist={dist}), "
                          f"confirming over {hide_grace_sec}s")
                else:
                    debug(f"idle={idle}ms (armed {activity_idle}ms, "
                          f"{now - activity_since:.1f}s of "
                          f"{hide_grace_sec}s, pointer_dist={dist})")
                hide, reason, stray = _should_hide(
                    now, activity_since, activity_idle, idle, dist,
                    hide_grace_sec, mouse_threshold_px, fresh_tolerance_ms)
                if hide:
                    activity_since = None
                    activity_idle = None
                    stray_episode = False
                    if not _root_after_safe(close_screensaver, idle, reason):
                        # Display already gone: mark inactive anyway.
                        is_screensaver_active = False
                elif stray:
                    # Lone event, no proof of a user. Stay visible and
                    # re-baseline so the *next* real input still hides
                    # promptly; log the first one per episode at info.
                    if not stray_episode:
                        log(f"ignored stray input event "
                            f"(idle {activity_idle}ms -> {idle}ms in "
                            f"{now - activity_since:.1f}s, "
                            f"pointer_dist={dist}) - staying visible")
                        stray_episode = True
                    else:
                        debug(f"stray still passive "
                              f"({activity_idle}ms -> {idle}ms), "
                              f"staying visible")
                    # Rebase both baselines: accumulated 1px jitter must
                    # not eventually cross the pointer threshold.
                    if latest_pointer is not None:
                        show_pointer = latest_pointer
                    activity_since = now
                    activity_idle = idle
                    sleep_for = max(min(poll_interval, hide_grace_sec), 0.001)
                else:
                    remaining = hide_grace_sec - (now - activity_since)
                    # Wake up exactly when the grace expires, but no less
                    # often than the normal poll (a shorter grace than the
                    # poll interval should not mean re-polling in a tight
                    # loop).
                    sleep_for = max(min(poll_interval, remaining), 0.001)
            else:
                debug(f"idle={idle}ms active=False")
        except Exception as e:
            # Never let this thread die silently (previous "hang" symptom).
            log(f"ERROR in idle checker: {e}")
        stop_event.wait(sleep_for)


def _quit():
    log("shutting down")
    shutdown_requested.set()
    try:
        if root is not None:
            root.destroy()
    except Exception:
        pass
    finally:
        sys.exit(0)


def _on_signal(signum, frame):
    sig = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    log(f"received {sig}, exiting")
    shutdown_requested.set()
    try:
        if root is not None:
            root.after(0, _quit)
        else:
            sys.exit(0)
    except Exception:
        sys.exit(0)


def _install_signal_handlers():
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)


def _tick():
    # Periodic Tk callback so the Tcl mainloop regularly returns to Python.
    # Without this, Ctrl-C (SIGINT) is not processed until the next Tk event,
    # which is why several ^C presses seemed to "hang".
    try:
        if root is not None and not shutdown_requested.is_set():
            root.after(500, _tick)
    except Exception:
        pass


def _ensure_display():
    """If DISPLAY is unset (service started before the X session), adopt :0
    when its socket exists. Returns the DISPLAY value in use (may be None)."""
    disp = os.environ.get("DISPLAY")
    if disp:
        return disp
    try:
        sockets = os.listdir("/tmp/.X11-unix")
    except OSError:
        return None
    for sock in sorted(sockets):
        if sock.startswith("X"):
            try:
                int(sock[1:])
            except ValueError:
                continue
            cand = f":{sock[1:]}"
            # Prefer :0, otherwise take the first X socket found.
            if sock == "X0" or "DISPLAY" not in os.environ:
                os.environ["DISPLAY"] = ":0" if "X0" in sockets else cand
                log(f"DISPLAY was unset, adopted {os.environ['DISPLAY']} "
                    f"from /tmp/.X11-unix")
                return os.environ["DISPLAY"]
    return None


def _create_root_with_retry():
    """Create the Tk root, waiting for the X session instead of tracebacking.

    Fixes the boot-time crash loop seen in the journal: the user service
    starts before lightdm sets DISPLAY, so tk.Tk() raised
    'no display name and no $DISPLAY' and systemd restarted us every 5s.
    Now we log once and retry in-process until X answers.
    """
    warned = False
    while not shutdown_requested.is_set():
        _ensure_display()
        try:
            r = tk.Tk()
            r.withdraw()
            r.report_callback_exception = _tk_callback_error
            return r
        except tk.TclError as e:
            msg = str(e)
            if not warned or DEBUG:
                log(f"waiting for X display "
                    f"(DISPLAY={os.environ.get('DISPLAY')!r}): {msg}")
                warned = True
            else:
                debug(f"still waiting for X display: {msg}")
            shutdown_requested.wait(STARTUP_RETRY_SEC)
        except Exception as e:
            log(f"unexpected error creating Tk root: {e}, "
                f"retrying in {STARTUP_RETRY_SEC}s")
            shutdown_requested.wait(STARTUP_RETRY_SEC)
    return None


def run_service(poll_interval, threshold_ms, hide_grace_sec,
                mouse_threshold_px, once=False):
    """Own the Tk root lifetime: survive X restarts instead of exiting 1.

    Fixes 'X connection to :0 broken (explicit kill or server shutdown)'
    killing the process (and with it the visible screensaver) on every
    suspend/resume and X reset. Now the break is logged and the UI is
    rebuilt once X is back; systemd Restart= stays as a last resort.
    """
    global root, screensaver, canvas, show_pointer, latest_pointer
    global is_screensaver_active, prev_title, prev_artist, last_player_gone
    while not shutdown_requested.is_set():
        root = _create_root_with_retry()
        if root is None or shutdown_requested.is_set():
            break
        screensaver = None
        canvas = None
        show_pointer = None
        latest_pointer = _sample_pointer_tk()
        is_screensaver_active = False
        prev_title = None
        prev_artist = None
        last_player_gone = False
        _tick()
        root.after(POINTER_POLL_MS, _pointer_poller)

        if once:
            try:
                show_screensaver()
                root.update()
                log("one-shot frame rendered (--once), exiting")
            finally:
                try:
                    root.destroy()
                except Exception:
                    pass
            return 0

        stop_event = threading.Event()
        t = threading.Thread(
            target=idle_checker, daemon=True,
            kwargs={"poll_interval": poll_interval,
                    "threshold_ms": threshold_ms,
                    "hide_grace_sec": hide_grace_sec,
                    "mouse_threshold_px": mouse_threshold_px,
                    "stop_event": stop_event,
                    "tk_root": root})
        t.start()

        log("entering Tk mainloop (waiting for idle; this blocks and prints "
            "nothing until activation — that is normal, not a hang)")
        try:
            root.mainloop()
        except tk.TclError as e:
            log(f"X connection lost ({e}), rebuilding UI in "
                f"{STARTUP_RETRY_SEC}s")
        except Exception as e:
            log(f"ERROR in Tk mainloop: {e}, rebuilding UI in "
                f"{STARTUP_RETRY_SEC}s")
        finally:
            stop_event.set()
            for g in ("screensaver", "canvas"):
                try:
                    w = globals().get(g)
                    if w is not None:
                        w.destroy()
                except Exception:
                    pass
            try:
                if root is not None:
                    root.destroy()
            except Exception:
                pass
            root = None
        if shutdown_requested.is_set():
            break
        log(f"reconnecting to X in {STARTUP_RETRY_SEC}s")
        shutdown_requested.wait(STARTUP_RETRY_SEC)
    log("service loop ended")
    return 0


def run_diagnostics():
    """Non-blocking startup check: prints DISPLAY, xprintidle, playerctl,
    and album-art fetch results, then exits. Use when stdout looks empty."""
    log(f"DISPLAY={os.environ.get('DISPLAY')!r} "
        f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')!r}")
    idle = get_idle_time()
    if idle is None:
        log("xprintidle: unknown (poll failed, see ERROR above)")
    else:
        log(f"xprintidle: {idle}ms")
    info = get_spotify_info()
    if info is None:
        log("playerctl: no MPRIS players found")
        art_url = ""
    else:
        title, artist, art_url = info
        log(f"playerctl: title={title!r} artist={artist!r} artUrl={art_url!r}")
    if art_url and not art_url.startswith("file://"):
        try:
            r = requests.get(art_url, timeout=5)
            log(f"art fetch: HTTP {r.status_code} "
                f"{r.headers.get('Content-Type')} {len(r.content)} bytes")
        except Exception as e:
            log(f"art fetch FAILED: {e}")
    elif art_url:
        exists = os.path.exists(unquote(urlparse(art_url).path))
        log(f"local art file exists={exists}")
    try:
        test_root = tk.Tk()
        test_root.withdraw()
        test_root.update()
        test_root.destroy()
        log("tkinter: OK (can open display)")
    except Exception as e:
        log(f"tkinter FAILED: {e}")
        return 2
    log("diagnostics OK")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spotify idle screensaver")
    parser.add_argument("delay", nargs="?", type=int, default=None,
                        help="shorthand for the idle delay in seconds, e.g. "
                             "'python3 spotify_screensaver.py 3'. Overrides "
                             "--idle-threshold-sec and the file default.")
    parser.add_argument("--debug", action="store_true",
                        help="log every idle poll")
    parser.add_argument("--idle-threshold-sec", type=int, default=None,
                        help="seconds of inactivity before showing "
                             f"(default: IDLE_THRESHOLD_MS={IDLE_THRESHOLD_MS}ms from file)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="xprintidle poll interval in seconds (default 2)")
    parser.add_argument("--hide-grace-sec", type=float, default=None,
                        help="how long input must persist before it hides a "
                             "visible screensaver; filters out stray "
                             f"non-user input events (default {HIDE_GRACE_SEC})")
    parser.add_argument("--mouse-threshold-px", type=float, default=None,
                        help="pointer travel in pixels that counts as real "
                             "mouse activity "
                             f"(default {MOUSE_MOVE_THRESHOLD_PX})")
    parser.add_argument("--blur-radius", type=int, default=None,
                        help=f"background blur strength (default {BKG_BLUR_RADIUS})")
    parser.add_argument("--bg-dim", type=float, default=None,
                        help=f"background brightness 0-1, lower is darker "
                             f"(default {BKG_DARKNESS})")
    parser.add_argument("--once", action="store_true",
                        help="show one frame immediately and exit (smoke test)")
    parser.add_argument("--test-display", action="store_true",
                        help="run startup diagnostics and exit (no mainloop)")
    args = parser.parse_args()

    DEBUG = args.debug
    hide_grace_sec = HIDE_GRACE_SEC
    if args.hide_grace_sec is not None:
        hide_grace_sec = max(0.0, args.hide_grace_sec)
    mouse_threshold_px = MOUSE_MOVE_THRESHOLD_PX
    if args.mouse_threshold_px is not None:
        mouse_threshold_px = max(0.0, args.mouse_threshold_px)
    if args.delay is not None:
        IDLE_THRESHOLD_MS = int(args.delay * 1000)
    elif args.idle_threshold_sec is not None:
        IDLE_THRESHOLD_MS = int(args.idle_threshold_sec * 1000)
    if args.blur_radius is not None:
        BKG_BLUR_RADIUS = args.blur_radius
    if args.bg_dim is not None:
        BKG_DARKNESS = args.bg_dim

    if args.test_display:
        sys.exit(run_diagnostics())

    log(f"starting (pid={os.getpid()}, threshold={IDLE_THRESHOLD_MS}ms, "
        f"hide_grace={hide_grace_sec}s, mouse_threshold={mouse_threshold_px}px, "
        f"blur={BKG_BLUR_RADIUS}, dim={BKG_DARKNESS}, "
        f"DISPLAY={os.environ.get('DISPLAY')!r})")

    _install_signal_handlers()
    sys.exit(run_service(args.poll_interval, IDLE_THRESHOLD_MS,
                         hide_grace_sec, mouse_threshold_px,
                         once=args.once))
