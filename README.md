# Spotify Screensaver

A fullscreen idle screensaver showing the current Spotify track with album art, artist and title over a blurred version of the album art as backdrop.

It runs as a systemd user service that launches a Python script once the X session has been idle for a while.

## Usage

```bash
make run      # try it out (3 second idle delay)
make install  # install, enable and start the service
```
