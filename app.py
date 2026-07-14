#!/usr/bin/env python3
"""
VIDEODROP — a small local video downloader.

A thin local server that wraps yt-dlp (download) + ffmpeg (fps re-encode / chunking)
behind a Carbon-styled single page. Nothing leaves your machine except the yt-dlp
requests to the video host.

Run:  python app.py   (then open http://127.0.0.1:7654)
Deps: yt-dlp, ffmpeg, node (JS runtime for YouTube) — all detected at startup.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
PORT = 7654
DEFAULT_OUTDIR = str(Path.home() / "Downloads")

# ── yt-dlp invocation ────────────────────────────────────────────────
# These flags are what actually gets past YouTube's bot-check on this machine:
#   --js-runtimes node          use Node as the JS challenge solver
#   --remote-components ejs:github  fetch the EJS solver lib
#   --cookies-from-browser BROWSER  authenticate as a signed-in browser
COOKIES_BROWSER = os.environ.get("VIDEODROP_BROWSER", "firefox")
BROWSER_CHOICES = ["firefox", "chrome", "edge", "brave", "none"]

def ytdlp_base():
    cmd = [sys.executable, "-m", "yt_dlp",
           "--js-runtimes", "node",
           "--remote-components", "ejs:github",
           "--no-warnings"]
    if COOKIES_BROWSER and COOKIES_BROWSER.lower() != "none":
        cmd += ["--cookies-from-browser", COOKIES_BROWSER]
    return cmd

# ── in-memory job registry ───────────────────────────────────────────
JOBS = {}          # job_id -> dict(status, stage, percent, message, files, outdir)
JOB_QUEUES = {}    # job_id -> queue.Queue for the per-job SSE stream
JOB_CTL = {}       # job_id -> {"proc": Popen, "cancel": threading.Event} (not JSON)
GLOBAL_SUBS = []   # list[queue.Queue] — subscribers to the all-jobs event stream
TASK_Q = queue.Queue()   # pending (job_id, kwargs) for the worker pool
WORKERS = 2        # concurrent downloads

class Cancelled(Exception):
    pass

def safe_name(s, maxlen=60):
    s = re.sub(r'[\\/:*?"<>|]+', "_", (s or "").strip())
    return (s[:maxlen] or "chapter").strip("._ ")

def emit(job_id, **fields):
    """Update job state and fan out to the per-job and all-jobs streams."""
    job = JOBS.setdefault(job_id, {})
    job.update(fields)
    q = JOB_QUEUES.get(job_id)
    if q:
        q.put(dict(job))
    snap = dict(job)
    snap["job"] = job_id
    for gq in list(GLOBAL_SUBS):
        try:
            gq.put(snap)
        except Exception:
            pass

def enqueue(url, opts, title=None):
    """Register a job and hand it to the worker pool. Returns the job id."""
    job_id = uuid.uuid4().hex[:12]
    JOB_QUEUES[job_id] = queue.Queue()
    JOB_CTL[job_id] = {"cancel": threading.Event()}
    JOBS[job_id] = {"status": "queued", "title": title or url, "url": url}
    kwargs = dict(
        url=url,
        height=opts.get("height") or None,
        fps=opts.get("fps") or None,
        outdir=opts.get("outdir") or DEFAULT_OUTDIR,
        trim_start=opts.get("trim_start") if opts.get("trim_start") not in (None, "") else None,
        trim_end=opts.get("trim_end") if opts.get("trim_end") not in (None, "") else None,
        chunk_mode=opts.get("chunk_mode") or "none",
        chunk_value=opts.get("chunk_value"),
        audio_only=bool(opts.get("audio_only")),
        audio_format=opts.get("audio_format") or "mp3",
        subs=bool(opts.get("subs")),
        sub_langs=opts.get("sub_langs") or "en",
        chapters=opts.get("chapters") or None,
        title=title,
    )
    emit(job_id, status="queued", message="Queued", title=title or url)
    TASK_Q.put((job_id, kwargs))
    return job_id

def worker_loop():
    while True:
        job_id, kwargs = TASK_Q.get()
        try:
            ctl = JOB_CTL.get(job_id)
            if ctl and ctl["cancel"].is_set():
                emit(job_id, status="cancelled", message="Cancelled")
            else:
                run_download(job_id=job_id, **kwargs)
        except Exception as e:
            emit(job_id, status="error", message=str(e))
        finally:
            TASK_Q.task_done()

def expand_playlist(url):
    """Return [{url,title}] — one entry for a video, many for a playlist."""
    cmd = ytdlp_base() + ["--flat-playlist", "-J", url]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        # fall back to treating it as a single URL
        return [{"url": url, "title": None}]
    info = json.loads(proc.stdout)
    entries = info.get("entries")
    if not entries:
        return [{"url": info.get("webpage_url") or url, "title": info.get("title")}]
    out = []
    for e in entries:
        if not e:
            continue
        eu = e.get("url") or e.get("webpage_url")
        if eu and not eu.startswith("http"):
            eu = f"https://www.youtube.com/watch?v={eu}"
        out.append({"url": eu or url, "title": e.get("title")})
    return out

# ── environment check ────────────────────────────────────────────────
def check_env():
    problems = []
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                       capture_output=True, check=True)
    except Exception:
        problems.append("yt-dlp not importable (pip install -U yt-dlp)")
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg not on PATH")
    if not shutil.which("node"):
        problems.append("node not on PATH (needed for YouTube JS challenge)")
    return problems

# ── probe: list available qualities ──────────────────────────────────
def probe(url):
    cmd = ytdlp_base() + ["-J", "--no-playlist", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip()
                           else "probe failed")
    info = json.loads(proc.stdout)
    # collapse formats to a per-height summary
    heights = {}
    for f in info.get("formats", []):
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        fps = f.get("fps")
        entry = heights.setdefault(h, {"height": h, "fps": set()})
        if fps:
            entry["fps"].add(int(round(fps)))
    quality = []
    for h in sorted(heights, reverse=True):
        e = heights[h]
        quality.append({"height": h, "fps": sorted(e["fps"], reverse=True)})
    chapters = []
    for c in (info.get("chapters") or []):
        if c.get("start_time") is not None and c.get("end_time") is not None:
            chapters.append({"start_time": c["start_time"], "end_time": c["end_time"],
                             "title": c.get("title") or ""})
    # available subtitle languages (manual + auto)
    subs = sorted(set(list((info.get("subtitles") or {}).keys()) +
                      list((info.get("automatic_captions") or {}).keys())))
    return {
        "title": info.get("title") or info.get("id"),
        "id": info.get("id"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "thumbnail": info.get("thumbnail"),
        "qualities": quality,
        "chapters": chapters,
        "subtitle_langs": subs[:40],
    }

# ── download + post-process ──────────────────────────────────────────
PROG_RE = re.compile(r"^DLPROG\|")

def ffprobe_duration(path):
    """Return media duration in seconds (float), or None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True)
        return float(r.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None

def fmt_speed(b):
    try:
        b = float(b)
    except (ValueError, TypeError):
        return ""
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}/s"
        b /= 1024
    return f"{b:.1f} TB/s"

def fmt_eta(s):
    try:
        s = int(float(s))
    except (ValueError, TypeError):
        return ""
    m, ss = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"ETA {h}:{m:02d}:{ss:02d}" if h else f"ETA {m}:{ss:02d}"

def run_ff(job_id, cancel, cmd, errlabel):
    """Run an ffmpeg command as a cancellable subprocess."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    JOB_CTL.setdefault(job_id, {})["proc"] = p
    _, err = p.communicate()
    if cancel.is_set():
        raise Cancelled()
    if p.returncode != 0:
        tail = err.strip().splitlines()[-1] if err.strip() else ""
        raise RuntimeError(f"{errlabel}: {tail}")

def run_download(job_id, url, height, fps, outdir,
                 trim_start=None, trim_end=None,
                 chunk_mode="none", chunk_value=None,
                 audio_only=False, audio_format="mp3",
                 subs=False, sub_langs="en", chapters=None, title=None):
    ctl = JOB_CTL.setdefault(job_id, {})
    cancel = ctl.setdefault("cancel", threading.Event())
    try:
        if chunk_mode == "chapters" and (trim_start is not None or trim_end is not None):
            raise RuntimeError("chapter split can't be combined with trim (timelines differ)")
        os.makedirs(outdir, exist_ok=True)
        emit(job_id, status="running", stage="download", percent=0,
             message="Fetching…", files=[], outdir=outdir,
             title=title or JOBS.get(job_id, {}).get("title") or url)

        final_path_file = HERE / f".final_{job_id}.txt"
        final_path_file.unlink(missing_ok=True)

        prog_tmpl = ("download:DLPROG|%(progress.downloaded_bytes)s|"
                     "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
                     "%(progress.fragment_index)s|%(progress.fragment_count)s|"
                     "%(progress.speed)s|%(progress.eta)s")
        cmd = ytdlp_base() + [
            "--no-playlist", "--newline",
            "--progress-template", prog_tmpl,
            "--print-to-file", "after_move:filepath", str(final_path_file),
            "-o", os.path.join(outdir, "%(title)s [%(id)s].%(ext)s"),
        ]
        if audio_only:
            cmd += ["-f", "bestaudio/best", "-x",
                    "--audio-format", audio_format, "--audio-quality", "0"]
            dl_label = f"Downloading audio ({audio_format})…"
        else:
            fmt = (f"bv*[height<={height}]+ba/b[height<={height}]" if height else "bv*+ba/b")
            cmd += ["-f", fmt, "--merge-output-format", "mp4"]
            dl_label = f"Downloading {height}p…" if height else "Downloading…"
        if subs:
            cmd += ["--write-subs", "--write-auto-subs",
                    "--sub-langs", sub_langs or "en", "--convert-subs", "srt"]
        cmd.append(url)

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        ctl["proc"] = proc
        last_err = ""
        for line in proc.stdout:
            line = line.rstrip("\n")
            if PROG_RE.match(line):
                _, dl, tot, tote, fi, fc, spd, eta = line.split("|")
                pct = None
                try:
                    total = float(tot) if tot not in ("NA", "") else float(tote)
                    if total > 0:
                        pct = min(99.9, float(dl) / total * 100)
                except (ValueError, ZeroDivisionError):
                    pct = None
                bits = [fmt_speed(spd), fmt_eta(eta)]
                if fc not in ("NA", ""):
                    bits.append(f"frag {fi}/{fc}")
                emit(job_id, stage="download",
                     percent=round(pct, 1) if pct is not None else None,
                     message=dl_label, detail="  ·  ".join(b for b in bits if b))
            elif line.strip().lower().startswith("error"):
                last_err = line
        proc.wait()
        if cancel.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            raise RuntimeError(last_err or "download failed — see server log")

        if not final_path_file.exists():
            raise RuntimeError("could not determine downloaded file path")
        src = Path(final_path_file.read_text(encoding="utf-8").strip().splitlines()[0])
        final_path_file.unlink(missing_ok=True)
        if not src.exists():
            raise RuntimeError(f"downloaded file missing: {src}")

        produced = [str(src)]
        ext = src.suffix or ".mp4"

        # sidecar subtitle files land next to the media
        # (match by prefix, not glob — the filename contains "[id]" which glob
        #  would interpret as a character class)
        if subs:
            for sub in sorted(src.parent.iterdir()):
                if sub.name.startswith(src.stem) and sub.suffix.lower() in (".srt", ".vtt"):
                    produced.append(str(sub))

        work = src

        # ── optional trim to [start, end] ──
        if trim_start is not None or trim_end is not None:
            start = float(trim_start or 0)
            emit(job_id, stage="trim", percent=None, message="Trimming clip…", detail="")
            clip = src.with_name(f"{src.stem}_clip{ext}")
            ff = ["ffmpeg", "-y", "-ss", str(start), "-i", str(src)]
            if trim_end is not None:
                ff += ["-t", str(max(0.1, float(trim_end) - start))]
            ff += ["-c", "copy", "-map", "0", str(clip)]
            run_ff(job_id, cancel, ff, "ffmpeg trim failed")
            produced.append(str(clip))
            work = clip

        # ── optional fps re-encode (video only) ──
        if fps and not audio_only:
            emit(job_id, stage="reencode", percent=None,
                 message=f"Re-encoding to {fps} fps…", detail="")
            reenc = work.with_name(f"{work.stem}_{fps}fps.mp4")
            ff = ["ffmpeg", "-y", "-i", str(work), "-r", str(fps),
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                  "-c:a", "aac", str(reenc)]
            run_ff(job_id, cancel, ff, "ffmpeg re-encode failed")
            produced.append(str(reenc))
            work = reenc

        # ── optional chunking ──
        if chunk_mode and chunk_mode != "none":
            parts_dir = work.with_name(f"{work.stem}_parts")
            parts_dir.mkdir(exist_ok=True)

            if chunk_mode == "chapters":
                chs = chapters or []
                if not chs:
                    raise RuntimeError("this video has no chapters")
                emit(job_id, stage="chunk", percent=None,
                     message=f"Splitting into {len(chs)} chapters…", detail="")
                for i, ch in enumerate(chs):
                    if cancel.is_set():
                        raise Cancelled()
                    start = float(ch["start_time"])
                    dur = float(ch["end_time"]) - start
                    title = safe_name(ch.get("title") or f"chapter{i+1}")
                    outp = parts_dir / f"{work.stem}_ch{i+1:02d}_{title}{ext}"
                    ff = ["ffmpeg", "-y", "-ss", str(start), "-i", str(work),
                          "-t", str(max(0.1, dur)), "-c", "copy", "-map", "0", str(outp)]
                    run_ff(job_id, cancel, ff, "ffmpeg chapter split failed")
                produced.extend(sorted(str(p) for p in parts_dir.iterdir()
                                       if p.name.startswith(f"{work.stem}_ch")))
            elif chunk_mode == "count":
                # exact N parts: cut each explicitly (the segment muxer only cuts
                # at keyframes, so it can't guarantee a precise count)
                n = max(2, int(round(float(chunk_value or 0))))
                dur = ffprobe_duration(work)
                if not dur:
                    raise RuntimeError("could not read duration for count-based split")
                emit(job_id, stage="chunk", percent=None,
                     message=f"Splitting into {n} parts…", detail="")
                step = dur / n
                for i in range(n):
                    if cancel.is_set():
                        raise Cancelled()
                    start = i * step
                    length = (dur - start) if i == n - 1 else step
                    outp = parts_dir / f"{work.stem}_part{i:03d}{ext}"
                    ff = ["ffmpeg", "-y", "-ss", str(start), "-i", str(work),
                          "-t", str(max(0.1, length)), "-c", "copy", "-map", "0", str(outp)]
                    run_ff(job_id, cancel, ff, "ffmpeg count split failed")
                produced.extend(sorted(str(p) for p in parts_dir.iterdir()
                                       if p.name.startswith(f"{work.stem}_part")))
            else:
                v = float(chunk_value or 0)
                dur = ffprobe_duration(work)
                if chunk_mode == "duration":
                    secs = max(1, int(round(v * 60)))
                    msg = f"Splitting into {v:g}-minute chunks…"
                elif chunk_mode == "size":
                    size_bytes = work.stat().st_size
                    if not dur or size_bytes <= 0:
                        raise RuntimeError("could not estimate bitrate for size-based split")
                    secs = max(1, int((v * 1024 * 1024) / (size_bytes / dur)))
                    msg = f"Splitting into ~{v:g} MB chunks…"
                else:
                    raise RuntimeError(f"unknown chunk mode: {chunk_mode}")
                emit(job_id, stage="chunk", percent=None, message=msg, detail="")
                pattern = str(parts_dir / f"{work.stem}_part%03d{ext}")
                ff = ["ffmpeg", "-y", "-i", str(work), "-c", "copy", "-map", "0",
                      "-f", "segment", "-segment_time", str(secs),
                      "-reset_timestamps", "1", pattern]
                run_ff(job_id, cancel, ff, "ffmpeg chunking failed")
                produced.extend(sorted(str(p) for p in parts_dir.iterdir()
                                       if p.name.startswith(f"{work.stem}_part")))

        emit(job_id, status="done", stage="done", percent=100,
             message="Complete", detail="", files=produced, outdir=outdir)
    except Cancelled:
        emit(job_id, status="cancelled", message="Cancelled", detail="")
    except Exception as e:
        emit(job_id, status="error", message=str(e))
    finally:
        JOB_CTL.pop(job_id, None)

# ── HTTP layer ───────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter console
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif parsed.path == "/api/env":
            self._send_json({"problems": check_env(), "browser": COOKIES_BROWSER,
                             "browser_choices": BROWSER_CHOICES,
                             "default_outdir": DEFAULT_OUTDIR})
        elif parsed.path == "/api/events":
            self._sse(parse_qs(parsed.query).get("job", [""])[0])
        elif parsed.path == "/api/events-all":
            self._sse_all()
        elif parsed.path == "/api/jobs":
            self._send_json({"jobs": [dict(v, job=k) for k, v in JOBS.items()]})
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/probe":
                data = self._read_json()
                self._send_json(probe(data["url"].strip()))
            elif parsed.path == "/api/download":
                data = self._read_json()
                job_id = enqueue(data["url"].strip(), data, title=data.get("title"))
                self._send_json({"job": job_id})
            elif parsed.path == "/api/playlist":
                data = self._read_json()
                self._send_json({"entries": expand_playlist(data["url"].strip())})
            elif parsed.path == "/api/batch":
                data = self._read_json()
                opts = data.get("options", {})
                created = []
                for u in data.get("urls", []):
                    u = (u or "").strip()
                    if not u:
                        continue
                    for e in expand_playlist(u):
                        created.append({"job": enqueue(e["url"], opts, title=e.get("title")),
                                        "title": e.get("title") or e["url"]})
                self._send_json({"jobs": created})
            elif parsed.path == "/api/cancel":
                data = self._read_json()
                ctl = JOB_CTL.get(data.get("job"))
                if ctl:
                    ev = ctl.get("cancel")
                    if ev:
                        ev.set()
                    p = ctl.get("proc")
                    if p and p.poll() is None:
                        try:
                            p.terminate()
                        except Exception:
                            pass
                self._send_json({"ok": True})
            elif parsed.path == "/api/config":
                global COOKIES_BROWSER
                data = self._read_json()
                b = (data.get("browser") or "").lower()
                if b in BROWSER_CHOICES:
                    COOKIES_BROWSER = b
                self._send_json({"browser": COOKIES_BROWSER})
            elif parsed.path == "/api/open":
                data = self._read_json()
                target = data.get("path") or DEFAULT_OUTDIR
                p = Path(target)
                folder = str(p if p.is_dir() else p.parent)
                if sys.platform == "win32":
                    os.startfile(folder)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
                self._send_json({"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            self._send_json({"error": str(e)}, code=400)

    def _sse(self, job_id):
        q = JOB_QUEUES.get(job_id)
        if q is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        # send current snapshot first
        try:
            self._sse_write(JOBS.get(job_id, {}))
            while True:
                try:
                    state = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(state)
                if state.get("status") in ("done", "error", "cancelled"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse_all(self):
        gq = queue.Queue()
        GLOBAL_SUBS.append(gq)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            # replay current state so a fresh connection is caught up
            for jid, st in list(JOBS.items()):
                self._sse_write(dict(st, job=jid))
            while True:
                try:
                    state = gq.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_write(state)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                GLOBAL_SUBS.remove(gq)
            except ValueError:
                pass

    def _sse_write(self, state):
        self.wfile.write(f"data: {json.dumps(state)}\n\n".encode("utf-8"))
        self.wfile.flush()


def main():
    try:  # avoid UnicodeEncodeError on legacy Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    probs = check_env()
    print("-" * 52)
    print("  VIDEODROP  ->  http://127.0.0.1:%d" % PORT)
    print("  cookies-from-browser: %s  (set VIDEODROP_BROWSER to change)" % COOKIES_BROWSER)
    if probs:
        print("  [!] environment issues:")
        for p in probs:
            print("     -", p)
    print("  workers: %d concurrent downloads" % WORKERS)
    print("-" * 52)
    for _ in range(WORKERS):
        threading.Thread(target=worker_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
