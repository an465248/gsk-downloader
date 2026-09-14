import os
import re
import httpx
import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DIR = os.path.dirname(os.path.abspath(__file__))
COBALT_API = os.environ.get("COBALT_API_URL", "")

def is_youtube(url):
    return bool(re.search(r"(youtube\.com|youtu\.be|youtube-nocookie\.com)", url))

def is_instagram(url):
    return bool(re.search(r"(instagram\.com|instagr\.am)", url))

def is_tiktok(url):
    return bool(re.search(r"tiktok\.com", url))

def is_twitter(url):
    return bool(re.search(r"(twitter\.com|x\.com)", url))

def cobalt_supported(url):
    return is_youtube(url) or is_instagram(url) or is_tiktok(url) or is_twitter(url)

async def cobalt_extract(url, audio_only=False):
    if not COBALT_API:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            body = {
                "url": url,
                "videoQuality": "1080",
                "filenameStyle": "pretty",
            }
            if audio_only:
                body["downloadMode"] = "audio"
                body["audioFormat"] = "mp3"
            resp = await client.post(
                COBALT_API.rstrip("/") + "/",
                json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            data = resp.json()
            if data.get("status") == "tunnel" or data.get("status") == "redirect":
                dl_url = data.get("url", "")
                if dl_url:
                    return {
                        "title": data.get("filename", "video"),
                        "thumbnail": "",
                        "duration": None,
                        "views": None,
                        "author": "",
                        "source": "cobalt",
                        "ext": "mp4",
                        "formats": [{
                            "format_id": "cobalt",
                            "ext": "mp3" if audio_only else "mp4",
                            "type": "audio" if audio_only else "video",
                            "label": "Audio (MP3)" if audio_only else "Best Quality",
                            "height": None,
                            "filesize": None,
                            "url": dl_url,
                            "tbr": 0,
                        }],
                    }
            return None
    except Exception:
        return None

def ytdl_extract(url):
    cookies_path = os.path.join(DIR, "cookies.txt")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
        },
    }
    if os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        return None

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

    result = None
    if cobalt_supported(url) and COBALT_API:
        result = await cobalt_extract(url)

    if not result:
        try:
            result = ytdl_extract(url)
        except Exception as e:
            if cobalt_supported(url) and COBALT_API:
                result = await cobalt_extract(url)
            if not result:
                return {"error": str(e)}

    if not result:
        return {"error": "Video info nahi mila"}

    return result

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

    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        req = client.build_request(
            "GET", url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.youtube.com/",
            }
        )
        resp = await client.send(req, stream=True)

        async def file_generator():
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                yield chunk

        return StreamingResponse(
            file_generator(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/octet-stream"),
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"GSK Downloader Server starting on http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
