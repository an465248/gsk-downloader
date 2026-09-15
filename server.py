import asyncio
import os
import re
import shutil
import tempfile
import yt_dlp
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
import uvicorn

app = FastAPI(title="gsk-downloader")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DIR = os.path.dirname(os.path.abspath(__file__))

# Render/Docker: cookies.txt git me nahi jata (.gitignore/.dockerignore),
# isliye production me YOUTUBE_COOKIES env var se cookies load karo.
# Local me cookies.txt file se kaam chalta hai.
_ENV_COOKIES = os.environ.get("YOUTUBE_COOKIES", "").strip()
_RUNTIME_COOKIES = os.path.join(DIR, "cookies_runtime.txt")
if _ENV_COOKIES:
    try:
        with open(_RUNTIME_COOKIES, "w") as _cf:
            _cf.write(_ENV_COOKIES + ("\n" if not _ENV_COOKIES.endswith("\n") else ""))
        COOKIES = _RUNTIME_COOKIES
    except Exception:
        COOKIES = os.path.join(DIR, "cookies.txt")
else:
    COOKIES = os.path.join(DIR, "cookies.txt")
HAS_COOKIES = os.path.exists(COOKIES) and os.path.getsize(COOKIES) > 2
HAS_FFMPEG = shutil.which("ffmpeg") is not None
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "http_headers": {"User-Agent": UA},
        "no_playlist": False,
    }
    if HAS_COOKIES:
        opts["cookiefile"] = COOKIES
    return opts


def friendly_error(e: Exception) -> str:
    msg = str(e).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)  # yt-dlp prefix hatao
    low = msg.lower()
    if "sign in to confirm" in low or "bot" in low:
        return "YouTube ne bot-check lagaya hai. Server par cookies.txt update karo, phir retry karo."
    if "private" in low:
        return "Ye video private hai — sirf public videos download hoti hain."
    if "login required" in low or "log in" in low:
        return "Is video ke liye login chahiye — supported nahi hai."
    if "age" in low and "confirm" in low:
        return "Age-restricted video supported nahi hai."
    if "unsupported url" in low:
        return "Ye URL supported nahi hai. Direct video link try karo."
    if "timed out" in low or "timeout" in low:
        return "Site ne reply nahi diya (timeout). Thoda ruk kar dobara try karo."
    if "name or service not known" in low or "failed to resolve" in low or "network" in low:
        return "Network error — connection check karke retry karo."
    return msg[:300]


def safe_filename(name: str, default: str = "video") -> str:
    name = re.sub(r'[\\/:*?"<>|]', " ", name or "")
    name = re.sub(r"[^\x20-\x7E]", "_", name).strip(" _.") or default
    return name[:120]


def pick_formats(formats_raw, duration=None):
    """Filter junk, dedupe per quality, sort best-first."""
    cands = []
    for f in formats_raw:
        fid = str(f.get("format_id") or "")
        if f.get("protocol") == "mhtml":
            continue
        if ("storyboard" in fid or fid.startswith("sb")) and not f.get("height"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        has_video = vcodec not in ("none", None, "")
        has_audio = acodec not in ("none", None, "")
        if not has_video and not has_audio:
            continue
        url = f.get("url") or ""
        if not url:
            continue
        ext = (f.get("ext") or "").lower()
        height = f.get("height")
        abr = f.get("abr") or 0
        tbr = f.get("tbr") or 0
        filesize = f.get("filesize") or f.get("filesize_approx")
        if not filesize and tbr and duration:
            try:
                filesize = int(tbr * 1000 / 8 * duration)
            except Exception:
                filesize = None

        if has_video and has_audio:
            ftype, progressive = "video", True
            label = f"{height}p" if height else (f.get("format_note") or ext or "Video")
        elif has_video:
            ftype, progressive = "video", False
            label = f"{height}p" if height else (f.get("format_note") or ext or "Video")
        else:
            ftype, progressive = "audio", False
            label = f.get("format_note") or (f"Audio {int(abr)}kbps" if abr else "Audio")

        # quality rank: mp4/m4a preferred on tie
        pref = 0 if ext in ("mp4", "m4a") else 1
        cands.append({
            "format_id": fid, "ext": ext, "type": ftype,
            "label": label, "height": height, "abr": abr,
            "filesize": filesize, "tbr": tbr, "url": url,
            "progressive": progressive, "needs_merge": has_video and not has_audio,
            "_pref": pref,
        })

    # dedupe: keep best tbr per (type, height/abr-bucket, progressive)
    best = {}
    for c in cands:
        if c["type"] == "video":
            key = ("v", c.get("height") or 0, c["progressive"])
        else:
            key = ("a", int((c.get("abr") or 0) // 32), c["ext"])
        old = best.get(key)
        if old is None or (c["tbr"], -c["_pref"]) > (old["tbr"], -old["_pref"]):
            best[key] = c
    formats = list(best.values())
    for c in formats:
        c.pop("_pref", None)
    formats.sort(key=lambda x: ((x.get("height") or 0), x.get("tbr") or 0), reverse=True)
    return formats[:60]


def best_audio(formats):
    aud = [f for f in formats if f["type"] == "audio"]
    if not aud:
        return None
    aud.sort(key=lambda x: (x.get("abr") or 0, x.get("tbr") or 0,
                             0 if x["ext"] == "m4a" else 1), reverse=True)
    a = aud[0]
    return {"url": a["url"], "ext": a["ext"] or "m4a", "abr": a.get("abr")}


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(DIR, "index.html"), "r") as f:
        return f.read()


@app.get("/manifest.json")
def manifest():
    return FileResponse(os.path.join(DIR, "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
def sw():
    return FileResponse(os.path.join(DIR, "sw.js"), media_type="application/javascript")


@app.get("/icon.svg")
def icon():
    return FileResponse(os.path.join(DIR, "icon.svg"), media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "ffmpeg": HAS_FFMPEG,
        "cookies": HAS_COOKIES,
        "mode": "user-device-download",
    }


@app.post("/api/extract")
async def extract(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "URL required hai"}, status_code=400)
    if not re.match(r"^https?://", url, re.I):
        return JSONResponse({"error": "URL 'http://' ya 'https://' se shuru hona chahiye."}, status_code=400)

    def _run():
        with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_run)
    except Exception as e:
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    if not info:
        return JSONResponse({"error": "Video info nahi mila"}, status_code=400)

    # playlist URL -> pehla video uthao
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return JSONResponse({"error": "Playlist khaali hai ya readable nahi hai."}, status_code=400)
        info = entries[0]
        if not info.get("formats") and (info.get("webpage_url") or info.get("url")):
            sub_url = info.get("webpage_url") or info["url"]

            def _run2():
                with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
                    return ydl.extract_info(sub_url, download=False)

            try:
                info = await asyncio.to_thread(_run2)
            except Exception as e:
                return JSONResponse({"error": friendly_error(e)}, status_code=400)

    duration = info.get("duration")
    formats = pick_formats(info.get("formats") or [], duration)
    if not formats:
        return JSONResponse(
            {"error": "Is URL se koi downloadable format nahi mila (private/login-wall ho sakta hai)."},
            status_code=400,
        )

    return {
        "title": info.get("title", "Video"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration,
        "views": info.get("view_count"),
        "author": info.get("uploader") or info.get("channel") or "",
        "source": info.get("extractor", ""),
        "ext": info.get("ext", "mp4"),
        "formats": formats,
        "best_audio": best_audio(formats),
        "server_merge": HAS_FFMPEG,
    }


@app.get("/api/stream")
async def stream_file(request: Request):
    """FALLBACK only: CORS-blocked hosts ke liye server se relay.
    Default flow browser direct download hai (user ka bandwidth/RAM)."""
    from urllib.parse import unquote
    url = request.query_params.get("url", "")
    filename = request.query_params.get("filename", "video.mp4")
    if not url:
        return JSONResponse({"error": "URL required"}, status_code=400)
    safe_name = safe_filename(unquote(filename), "video.mp4")

    async def gen():
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", url, headers={"User-Agent": UA}) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                        yield chunk
        except Exception:
            return

    return StreamingResponse(
        gen(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/merge-download")
async def merge_download(request: Request):
    """FALLBACK only: jab browser-merge (ffmpeg.wasm) fail ho.
    Heavy kaam default browser me hota hai (user ka CPU/RAM)."""
    from urllib.parse import unquote
    if not HAS_FFMPEG:
        return JSONResponse(
            {"error": "Server par ffmpeg nahi hai — browser wala 'HD Merge' button use karo."},
            status_code=501,
        )
    url = request.query_params.get("url", "")
    filename = request.query_params.get("filename", "video")
    format_id = request.query_params.get("format_id", "")
    if not url:
        return JSONResponse({"error": "URL required"}, status_code=400)

    safe_name = safe_filename(unquote(filename))
    tmp_dir = tempfile.mkdtemp()
    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")
    fmt_str = f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best"

    def _run():
        opts = base_ydl_opts()
        opts.update({
            "outtmpl": outtmpl,
            "format": fmt_str,
            "merge_output_format": "mp4",
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # sabse badi bani hui media file dhoondo
            found = None
            for root, _, files in os.walk(tmp_dir):
                for fn in files:
                    if fn.rsplit(".", 1)[-1].lower() in ("mp4", "mkv", "webm", "m4v"):
                        p = os.path.join(root, fn)
                        if found is None or os.path.getsize(p) > os.path.getsize(found):
                            found = p
            return found

    try:
        file_path = await asyncio.to_thread(_run)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    if not file_path or not os.path.exists(file_path):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return JSONResponse({"error": "File download nahi ho payi"}, status_code=500)

    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=safe_name + ".mp4",
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"GSK Downloader Server starting on http://0.0.0.0:{port}")
    print(f"ffmpeg={'yes' if HAS_FFMPEG else 'no (server-merge off)'} cookies={'yes' if HAS_COOKIES else 'no'}")
    uvicorn.run(app, host="0.0.0.0", port=port)
