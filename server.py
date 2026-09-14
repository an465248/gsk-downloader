import os
import json
import re
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import subprocess
import tempfile

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DIR = os.path.dirname(os.path.abspath(__file__))

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(DIR, "index.html"), "r") as f:
        return f.read()

@app.post("/api/extract")
async def extract(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return {"error": "URL required hai"}

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return {"error": str(e)}

    if not info:
        return {"error": "Video info nahi mila"}

    formats_raw = info.get("formats", [])
    formats = []

    for f in formats_raw:
        fmt_id = f.get("format_id", "")
        ext = f.get("ext", "")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        filesize = f.get("filesize") or f.get("filesize_approx")
        url_val = f.get("url", "")
        tbr = f.get("tbr", 0)

        has_video = vcodec != "none" and vcodec
        has_audio = acodec != "none" and acodec

        if not url_val:
            continue

        if has_video and has_audio:
            ftype = "video"
            label = f"{height}p" if height else f.get("format_note", ext)
        elif has_video:
            ftype = "video"
            label = f"{height}p" if height else f.get("format_note", ext)
        elif has_audio:
            ftype = "audio"
            abr = f.get("abr", 0)
            label = f.get("format_note", f"Audio {abr}kbps") if ext != "m4a" else f"Audio {abr}kbps"
        else:
            continue

        formats.append({
            "format_id": fmt_id,
            "ext": ext,
            "type": ftype,
            "label": label,
            "height": height,
            "filesize": filesize,
            "url": url_val,
            "tbr": tbr,
        })

    formats.sort(key=lambda x: (x.get("height") or 0), reverse=True)

    return {
        "title": info.get("title", "Video"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": info.get("duration"),
        "views": info.get("view_count"),
        "author": info.get("uploader", info.get("channel", "")),
        "source": info.get("extractor", ""),
        "ext": info.get("ext", "mp4"),
        "formats": formats,
    }

@app.post("/api/download")
async def download(request: Request):
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        return {"error": "URL required hai"}
    return {"url": url}

@app.get("/api/stream")
async def stream(request: Request):
    url = request.query_params.get("url", "")
    filename = request.query_params.get("filename", "video.mp4")
    if not url:
        return {"error": "URL required"}

    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.youtube.com/",
    })

    def file_generator():
        with urllib.request.urlopen(req) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        file_generator(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"GSK Downloader Server starting on http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
