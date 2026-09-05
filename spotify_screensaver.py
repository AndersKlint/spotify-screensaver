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

IDLE_THRESHOLD_MS = 300000  # 5 minutes
BKG_BLUR_RADIUS = 8
BKG_DARKNESS = 0.55
TEXT_FONT_SIZE = 98
TITLE_FONT_SIZE = 98
ARTIST_FONT_SIZE = 86
TITLE_ARTIST_GAP = 36
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
DEBUG = False


def log(msg):
    print(f"[spotify-screensaver] {msg}", flush=True)


def debug(msg):
    if DEBUG:
        log(f"DEBUG: {msg}")


def get_idle_time():
    """Return X idle time in ms. Never raises: returns 0 on error."""
    try:
        out = subprocess.check_output(["xprintidle"], timeout=5).strip()
        return int(out)
    except FileNotFoundError:
        log("ERROR: 'xprintidle' not found. Is it installed? (sudo apt install xprintidle)")
        return 0
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired) as e:
        log(f"ERROR: xprintidle failed: {e}")
        return 0


def _playerctl(*args):
    return subprocess.check_output(
        ["playerctl"] + list(args), timeout=5
    ).decode().strip()


def get_spotify_info():
    """Return (title, artist, art_url). Works with deb/flatpak/snap Spotify."""
    # Prefer the spotify player explicitly; fall back to default player.
    for prefix in (["-p", "spotify"], []):
        try:
            title = _playerctl(*prefix, "metadata", "title")
            artist = _playerctl(*prefix, "metadata", "artist")
            art_url = _playerctl(*prefix, "metadata", "mpris:artUrl")
            if title:
                return title, artist, art_url
        except FileNotFoundError:
            log("ERROR: 'playerctl' not found. Is it installed? (sudo apt install playerctl)")
            return "Nothing Playing", "", ""
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
    return "Nothing Playing", "", ""


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


def make_track_text(title, artist, max_width, title_size=None,
                    artist_size=None, gap=None, offset=None, blur=None,
                    opacity=None):
    """Render title (bold) stacked over artist (regular) with one soft shadow.

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
    title_font = _load_text_font(title_size, bold=True)
    artist_font = _load_text_font(artist_size, bold=False)
    measurer = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
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
    text_h = sum(line_height * len(lines) for lines, _, line_height, _ in laid_out)
    text_h += gap * (len(laid_out) - 1)
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



def show_screensaver():
    global screensaver, canvas
    if screensaver is None:
        screensaver = tk.Toplevel(root)
        screensaver.attributes('-fullscreen', True)
        screensaver.configure(bg='black', cursor='none')
        canvas = tk.Canvas(screensaver, bg='black', highlightthickness=0,
                           cursor='none')
        canvas.pack(fill='both', expand=True)
    log("idle threshold reached, showing screensaver")
    screensaver.deiconify()
    screensaver.lift()
    update_display()


def update_display():
    global prev_title, prev_artist
    title, artist, art_url = get_spotify_info()
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
    text_photo = None
    try:
        text_photo = make_track_text(title, artist, wrap_width)
    except Exception as e:
        log(f"Error rendering text: {e}")
        text_photo = None
    if text_photo is not None:
        canvas.create_image(text_x, text_y, anchor='center', image=text_photo)

    # Schedule next update
    screensaver.after(10000, update_display)



def close_screensaver():
    global is_screensaver_active
    if screensaver:
        screensaver.withdraw()
    if is_screensaver_active:
        log("activity detected, hiding screensaver")
    is_screensaver_active = False


def idle_checker(poll_interval=2, threshold_ms=None):
    """Single activity source: xprintidle. Idle rising past threshold
    activates; any drop below threshold while active means user input."""
    global is_screensaver_active
    if threshold_ms is None:
        threshold_ms = IDLE_THRESHOLD_MS
    log(f"idle checker started (threshold={threshold_ms}ms, poll={poll_interval}s)")
    while True:
        try:
            idle = get_idle_time()
            debug(f"idle={idle}ms active={is_screensaver_active}")
            if idle >= threshold_ms and not is_screensaver_active:
                is_screensaver_active = True
                root.after(0, show_screensaver)
            elif idle < threshold_ms and is_screensaver_active:
                root.after(0, close_screensaver)
        except Exception as e:
            # Never let this thread die silently (previous "hang" symptom).
            log(f"ERROR in idle checker: {e}")
        time.sleep(poll_interval)


def _quit():
    log("shutting down")
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
        if root is not None:
            root.after(500, _tick)
    except Exception:
        pass


def run_diagnostics():
    """Non-blocking startup check: prints DISPLAY, xprintidle, playerctl,
    and album-art fetch results, then exits. Use when stdout looks empty."""
    log(f"DISPLAY={os.environ.get('DISPLAY')!r} "
        f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')!r}")
    idle = get_idle_time()
    log(f"xprintidle: {idle}ms")
    title, artist, art_url = get_spotify_info()
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
        f"blur={BKG_BLUR_RADIUS}, dim={BKG_DARKNESS}, "
        f"DISPLAY={os.environ.get('DISPLAY')!r})")

    root = tk.Tk()
    root.withdraw()
    _install_signal_handlers()
    _tick()

    if args.once:
        show_screensaver()
        root.update()
        log("one-shot frame rendered (--once), exiting")
        root.destroy()
        sys.exit(0)

    threading.Thread(target=idle_checker, daemon=True,
                     kwargs={"poll_interval": args.poll_interval,
                             "threshold_ms": IDLE_THRESHOLD_MS}).start()

    log("entering Tk mainloop (waiting for idle; this blocks and prints nothing "
        "until activation — that is normal, not a hang)")
    root.mainloop()
