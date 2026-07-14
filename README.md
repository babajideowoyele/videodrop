# VIDEODROP

A small, local video downloader with a clean single-page UI. Paste a URL (or a whole
playlist), pick resolution / frame rate / audio-only, grab subtitles, trim to a clip,
and optionally split the result into chunks. Everything runs on your own machine — the
only network traffic is yt-dlp talking to the video host.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/),
with a zero-dependency Python standard-library server and a static HTML front end.

## Prerequisites

You need three tools on your `PATH`:

| Tool     | Why                              | Install |
|----------|----------------------------------|---------|
| Python 3.8+ | runs the local server         | [python.org](https://www.python.org/downloads/) |
| yt-dlp   | download + format probing        | `pip install -U yt-dlp` |
| ffmpeg   | fps re-encode, trim, chunking    | Windows: `winget install Gyan.FFmpeg` · macOS: `brew install ffmpeg` · Linux: `apt install ffmpeg` |
| Node.js  | solves YouTube's JS challenge    | [nodejs.org](https://nodejs.org/) |

(Node is only needed for sites like YouTube that require solving a JS challenge.)

## Quick start

```bash
git clone https://github.com/babajideowoyele/videodrop.git
cd videodrop
python app.py          # use python3 on macOS/Linux
```

It prints the address and opens `http://127.0.0.1:7654` in your browser. Press
`Ctrl+C` in the terminal to stop. On Windows you can also run `run.ps1`.

Downloads go to your **Downloads** folder by default; change the target per download
in the UI.

## Features

- **Fetch** resolves the available resolutions, chapters, and subtitle languages for
  a URL before you commit.
- **Resolution** — download at any height the source offers (144p → 2160p).
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

  Parts land in a `<title>_parts/` subfolder.
- **Batch / playlist** — paste many URLs (one per line) or a single playlist link;
  each expands and enters a **job queue** processed by a worker pool (2 concurrent
  downloads). The queue shows per-job progress with a cancel button on each row.
- **Cancel** a running job; the download aborts and cleans up.
- **Live progress** — percentage, download speed, ETA, and fragment count.
- **Retry** a failed download with one click.
- **Cookie source picker** — switch between Firefox / Chrome / Edge / Brave / none in
  the UI (no restart).
- **Remembered settings** — output folder, quality, fps, audio format, subtitle
  languages, cookie source, and theme persist across sessions (browser localStorage).
- **Session history** — every download this session, with a jump-to-folder button.
- **Dark mode** — light/dark toggle in the header; follows your system theme by default.

## How it works

- **Backend** (`app.py`) — Python standard-library HTTP server (no pip packages). It
  shells out to **yt-dlp** for probing/downloading and **ffmpeg** for fps re-encoding,
  trimming, and chunking. Downloads run in a small worker pool.
- **Frontend** (`index.html`) — a single self-contained page that talks to the backend
  over JSON + Server-Sent Events for live progress.

## Notes on combinations

- **Trim** and **chapter split** are mutually exclusive (a trim shifts the timeline,
  so chapter timestamps would no longer line up).
- **Audio only** hides the resolution/fps controls; chunking, trim, and subtitles
  still apply to the audio.

## Configuration

- **Cookies from browser** — many sites (YouTube) require a signed-in cookie jar.
  Pick the source in the UI, or set the default before launch:
  ```bash
  # macOS/Linux
  VIDEODROP_BROWSER=chrome python app.py     # or firefox, edge, brave, none
  ```
  ```powershell
  # Windows PowerShell
  $env:VIDEODROP_BROWSER = "chrome"; python app.py
  ```
  Chrome/Edge lock their cookie database while the browser is running — if extraction
  fails, close the browser or use Firefox (which doesn't lock).
- **Port** — edit `PORT` at the top of `app.py` (default 7654).
- **Concurrent downloads** — edit `WORKERS` at the top of `app.py` (default 2).

## Notes / limits

- First fetch of a new site takes a few seconds while yt-dlp downloads the challenge
  solver; subsequent fetches are quick.
- **By time / size** chunk boundaries snap to the nearest keyframe (a property of
  lossless stream-copy), so a "5 minute" chunk may be off by a second or two. **By
  count** and **by chapters** cut each part explicitly for an exact result.

## Troubleshooting / FAQ

**"Sign in to confirm you're not a bot" — or resolutions are missing.**
YouTube requires solving a JavaScript challenge and a signed-in cookie jar.
Make sure **Node.js is installed** (VideoDrop uses it plus `--remote-components
ejs:github` to solve the challenge) and pick a **cookie source** in the UI. If you
just installed Node, restart the server.

**"Could not copy Chrome cookie database".**
Chrome and Edge lock their cookie database while the browser is open. Either close
the browser, or switch the cookie source to **Firefox** (it doesn't lock) — the
default.

**"Video unavailable" / private / age-restricted.**
The video is removed, private, region-locked, or age-gated. For age-gated content,
sign into the browser whose cookies you selected; that's usually enough.

**Port already in use (`WinError 10048` / "Address already in use").**
Another VideoDrop instance is already running on port 7654, or something else holds
the port. Stop the other instance, or change `PORT` at the top of `app.py`.

**`ffmpeg not on PATH` / `node not on PATH` banner at the top of the page.**
A prerequisite is missing — see [Prerequisites](#prerequisites). Install it, then
restart the server. (yt-dlp problems usually mean `pip install -U yt-dlp`.)

**First fetch is slow (~a few seconds).**
The first time per session, yt-dlp downloads the challenge-solver library. Later
fetches are quick.

**"this video has no chapters".**
You asked for a chapter split on a video without chapter markers. Use *by time /
count / size* instead. (The UI disables the chapters option when a video has none;
you'll only see this via the batch queue.)

**"chapter split can't be combined with trim".**
Chapter timestamps refer to the full timeline, so a trim would misalign them. Pick
one or the other.

**Chunk lengths are a second or two off.**
*By time* and *by size* stream-copy and can only cut on keyframes, so a boundary may
drift slightly. Use *by count* or *by chapters* for exact cuts, or re-encode first.

**A download failed but I want to try again.**
Use the **Retry** button (single downloads) or re-add the URL to the batch queue.

## License

MIT — see [LICENSE](LICENSE).
