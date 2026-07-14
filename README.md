# VIDEODROP

A small, local video downloader with a Carbon-styled UI (matching the SYNAPSIS /
MaskingOPS look). Paste a URL, pick resolution and frame rate, optionally split the
result into fixed-duration chunks. Everything runs on your machine — the only network
traffic is yt-dlp talking to the video host.

## Run

```powershell
cd C:\Users\User\tools\videodrop
python app.py
```

It prints the URL and opens `http://127.0.0.1:7654` in your browser. Press `Ctrl+C`
in the terminal to stop. (`run.ps1` does the same thing.)

## How it works

- **Backend** (`app.py`) — Python standard-library HTTP server, no pip install needed.
  It shells out to:
  - **yt-dlp** for probing formats and downloading, invoked with the flags that get
    past YouTube's bot-check on this machine:
    `--js-runtimes node --remote-components ejs:github --cookies-from-browser firefox`
  - **ffmpeg** for optional fps re-encoding and duration-based chunking.
- **Frontend** (`index.html`) — single Carbon page, talks to the backend over JSON +
  Server-Sent Events for live progress.

## Features

- **Fetch** resolves the available resolutions, chapters, and subtitle languages for
  a URL before you commit.
- **Resolution** — download at any height the source offers (144p → 1080p+).
- **Frame rate** — *Source* keeps the original stream (fast, lossless). Any other
  value (30 / 24 / 15) re-encodes with ffmpeg (`libx264`, veryfast, CRF 20).
- **Audio only** — extract just the sound as **MP3** or **M4A** (`-x`, best quality).
- **Subtitles** — fetch SRT captions (including auto-generated) in the languages you
  name (e.g. `en` or `en,nl`); saved as sidecar files next to the media.
- **Trim** — extract a single start→end clip (`mm:ss` or `h:mm:ss`).
- **Chunking** — split the finished file four ways:
  - **By time** — N-minute segments.
  - **By count** — N equal parts.
  - **By size** — parts under ~N MB (estimated from bitrate).
  - **By chapters** — one file per chapter marker, named after the chapter.

  All are stream-copied (fast, lossless); parts land in a `<title>_parts/` subfolder.
- **Cancel** a running job; the download aborts and cleans up.
- **Live progress** — percentage, download speed, ETA, and fragment count.
- **Retry** a failed download with one click.
- **Cookie source picker** — switch between Firefox / Chrome / Edge / Brave / none in
  the UI (no restart).
- **Remembered settings** — output folder, quality, fps, audio format, subtitle
  languages, and cookie source persist across sessions (browser localStorage).
- **Session history** — every download this session, with a jump-to-folder button.
- **Save to folder** — defaults to your Downloads folder; editable per download.
- **Batch / playlist** — paste many URLs (one per line) or a single playlist link;
  each expands and enters a **job queue** processed by a worker pool (2 concurrent
  downloads). The queue shows per-job progress with a cancel button on each row.
- **Dark mode** — light/dark toggle in the header; follows your system theme by
  default and remembers your choice. The brand bar stays dark in both.

## Notes on combinations

- **Trim** and **chapter split** are mutually exclusive (a trim shifts the timeline,
  so chapter timestamps would no longer line up).
- **Audio only** hides the resolution/fps controls; chunking, trim, and subtitles
  still apply to the audio.

## Requirements (all already present on this machine)

| Tool   | Why                                   |
|--------|---------------------------------------|
| yt-dlp | download + format probe               |
| ffmpeg | fps re-encode, chunking               |
| node   | solves YouTube's JS challenge         |

## Configuration

- **Cookies browser** — YouTube requires a signed-in cookie jar. Defaults to Firefox
  (Chrome/Edge lock their cookie DB while running). Override:
  ```powershell
  $env:VIDEODROP_BROWSER = "chrome"   # or edge, brave, none
  python app.py
  ```
- **Port** — edit `PORT` at the top of `app.py` (default 7654).

## Notes / limits

- First fetch of a new video takes ~10s while yt-dlp downloads the site's challenge
  solver; subsequent ones are quick.
- Chunk boundaries snap to the nearest keyframe (a property of lossless stream-copy),
  so a "5 minute" chunk may be off by a second or two. Re-encode first if you need
  frame-exact cuts.
- Single video only (`--no-playlist`).
