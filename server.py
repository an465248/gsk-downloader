import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
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
    # SPEED: kam timeout + kam retry = fail fast, success path same speed.
    # (Retry sirf fail par lagta hai; success 1 attempt me nikalta hai.)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
        "http_headers": {"User-Agent": UA},
        "no_playlist": False,
    }
    if HAS_COOKIES:
        opts["cookiefile"] = COOKIES
    return opts


# ---------------- extract cache (repeat fetch = instant) ----------------
# Same URL dobara (Recent reopen, 403 self-heal, double-tap Get Video) to
# yt-dlp dobara network par nahi jata — memory se turant response.
_EXTRACT_CACHE = {}
_EXTRACT_CACHE_TTL = 300  # sec
_EXTRACT_CACHE_MAX = 256


def _cache_get(url):
    try:
        ts, resp = _EXTRACT_CACHE.get(url) or (0, None)
        if resp is not None and (time.time() - ts) < _EXTRACT_CACHE_TTL:
            return resp
        if resp is not None:
            _EXTRACT_CACHE.pop(url, None)
    except Exception:
        pass
    return None


def _cache_put(url, resp):
    try:
        if len(_EXTRACT_CACHE) >= _EXTRACT_CACHE_MAX:
            _EXTRACT_CACHE.pop(next(iter(_EXTRACT_CACHE)), None)
        _EXTRACT_CACHE[url] = (time.time(), resp)
    except Exception:
        pass


def _parse_youtube_id(url):
    """watch?v= / youtu.be / shorts / embed / live / v se video-id. Na mile to ''."""
    try:
        u = url or ""
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,32})", u)
        if m:
            return m.group(1)
        m = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,32})", u)
        if m:
            return m.group(1)
        m = re.search(r"youtube\.com/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{6,32})", u)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


# YouTube web client ki public key (youtubei Related API ke liye).
YT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"


def _dur_to_sec(t):
    try:
        s = 0
        for p in str(t).strip().split(":"):
            s = s * 60 + int(p)
        return s
    except Exception:
        return 0


def _parse_lockup(r):
    try:
        lv = r.get("lockupViewModel") or {}
        if (lv.get("contentType") or "") != "LOCKUP_CONTENT_TYPE_VIDEO":
            return None
        vid = lv.get("contentId") or ""
        if not vid:
            return None
        md = ((lv.get("metadata") or {}).get("lockupMetadataViewModel") or {})
        title = (md.get("title") or {}).get("content") or vid
        thumb, dur = "", 0
        try:
            ci = (lv.get("contentImage") or {}).get("thumbnailViewModel") or {}
            srcs = ((ci.get("image") or {}).get("sources") or [])
            if srcs:
                thumb = (srcs[-1] or {}).get("url") or ""
            for ov in (ci.get("overlays") or []):
                try:
                    badges = ((ov.get("thumbnailBottomOverlayViewModel") or {}).get("badges")) or []
                    for b in badges:
                        t = ((b.get("thumbnailBadgeViewModel") or {}).get("text")) or ""
                        if t and ":" in t:
                            dur = _dur_to_sec(t)
                            break
                    if dur:
                        break
                except Exception:
                    continue
        except Exception:
            pass
        if not thumb:
            thumb = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
        return {"id": vid, "title": title,
                "url": "https://www.youtube.com/watch?v=" + vid,
                "thumbnail": thumb, "duration": dur}
    except Exception:
        return None


def _parse_compact_video(r):
    try:
        cv = r.get("compactVideoRenderer") or {}
        vid = cv.get("videoId") or ""
        if not vid:
            return None
        title = ""
        try:
            t = cv.get("title") or {}
            title = t.get("simpleText") or "".join(
                [(x.get("text") or "") for x in (t.get("runs") or [])])
        except Exception:
            pass
        thumb = ""
        try:
            ths = ((cv.get("thumbnail") or {}).get("thumbnails") or [])
            if ths:
                thumb = (ths[-1] or {}).get("url") or ""
        except Exception:
            pass
        if not thumb:
            thumb = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
        dur = 0
        try:
            lt = (cv.get("lengthText") or {}).get("simpleText") or ""
            if lt:
                dur = _dur_to_sec(lt)
        except Exception:
            pass
        return {"id": vid, "title": title or vid,
                "url": "https://www.youtube.com/watch?v=" + vid,
                "thumbnail": thumb, "duration": dur}
    except Exception:
        return None


def _youtube_related_sync(video_id, limit=15):
    """Single YouTube video ke Related/Up-Next (internal API). Fail -> []."""
    try:
        vid = (video_id or "").strip()
        if not vid or len(vid) > 32:
            return []
        payload = json.dumps({
            "context": {"client": {"clientName": "WEB", "clientVersion": "2.20241201",
                                   "hl": "en", "gl": "US"}},
            "videoId": vid,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://www.youtube.com/youtubei/v1/next?key=" + YT_API_KEY + "&prettyPrint=false",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as res:
            data = json.loads(res.read().decode("utf-8", "replace"))
        sec = ((((data.get("contents") or {}).get("twoColumnWatchNextResults") or {})
                .get("secondaryResults") or {}).get("secondaryResults") or {})
        out, seen = [], set([vid])
        for r in (sec.get("results") or []):
            if not isinstance(r, dict) or len(out) >= limit:
                break
            if "lockupViewModel" in r:
                item = _parse_lockup(r)
            elif "compactVideoRenderer" in r:
                item = _parse_compact_video(r)
            else:
                item = None
            if item and item["id"] not in seen:
                seen.add(item["id"])
                out.append(item)
        return out
    except Exception:
        return []


def friendly_error(e: Exception) -> str:
    msg = str(e).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)  # yt-dlp prefix hatao
    low = msg.lower()
    if "sign in to confirm" in low or "bot" in low:
        return "YouTube ne bot-check lagaya hai. Server par cookies.txt update karo, phir retry karo."
    if "private" in low:
        return "Ye video private hai — sirf public videos download hoti hain."
    if "login required" in low or "log in" in low or "not logged in" in low:
        if "instagram" in low:
            return "Instagram ne login-wall lagaya hai. Server ke cookies (YOUTUBE_COOKIES env) me Instagram login cookies dalo, phir retry karo. Public reels bina login ke nahi nikalti."
        if "facebook" in low or "fb" in low:
            return "Ye Facebook video private hai ya login maang rahi hai. Public videos (SD+HD) bina login ke chalti hain."
        return "Is video ke liye login chahiye — supported nahi hai."
    if "empty media response" in low or ("cannot parse data" in low and "instagram" in low):
        return "Instagram ne login-wall lagaya hai. Server cookies me Instagram login dalo, phir retry karo."
    if "cannot parse data" in low and ("facebook" in low or "fb" in low):
        return "Facebook page poori load nahi hui — dobara Get Video dabao. (Public videos bina login ke chalti hain.)"
    if "snapchat" in low or "snapchat.com" in low:
        if "unsupported url" in low:
            return ("Ye Snapchat link support nahi hai. Sirf Spotlight links chalte hain "
                    "(snapchat.com/spotlight/...). Snapchat app me video kholo → Share → "
                    "Copy Link karke Spotlight link paste karo.")
        return ("Snapchat link nahi khula — Spotlight ka public link try karo "
                "(snapchat.com/spotlight/...). Private/story links supported nahi hain.")
    if "rate-limit" in low or "rate limited" in low or "try again later" in low:
        return "Site ne temporarily rok lagayi hai. 10-15 min ruk kar retry karo."
    if "age" in low and "confirm" in low:
        return "Age-restricted video supported nahi hai."
    if "unsupported url" in low:
        return "Ye URL supported nahi hai. Direct video link try karo."
    if "http error 403" in low or " 403" in low:
        return "Link expire ho gaya (403). Dobara Get Video dabao taaki fresh link mile, phir turant download karo."
    if "http error 416" in low or "416" in low:
        return "Resume fail (416). Dobara fresh link se download karo."
    if "timed out" in low or "timeout" in low:
        return "Site ne reply nahi diya (timeout). Thoda ruk kar dobara try karo."
    if "name or service not known" in low or "failed to resolve" in low or "network" in low:
        return "Network error — connection check karke retry karo."
    return msg[:300]


def safe_filename(name: str, default: str = "video") -> str:
    name = re.sub(r'[\\/:*?"<>|]', " ", name or "")
    name = re.sub(r"[^\x20-\x7E]", "_", name).strip(" _.") or default
    return name[:120]


def _with_bypass(u: str) -> str:
    """YouTube per-connection throttle bypass (yt-dlp bhi yehi lagata hai)."""
    if u and "googlevideo.com" in u and "ratebypass=" not in u:
        u += ("&" if "?" in u else "?") + "ratebypass=yes"
    return u


def normalize_snapchat_url(url: str) -> str:
    """Snapchat /t/ short-links redirect hokar asli Spotlight/story page par
    jate hain. yt-dlp sirf spotlight/<id> samajhta hai — isliye redirect
    resolve karke final URL do. Sirf /t/ par network call (5s cap);
    baaki sab (add/discover/...) turant wapas — yt-dlp khud fast-fail
    karega aur friendly_error sahi message dega. Fail-soft: original."""
    try:
        low = (url or "").lower()
        if "snapchat.com" not in low and "snapchat" not in low:
            return url
        # Pehle se spotlight link hai to chhedo mat
        if re.search(r"snapchat\.com/spotlight/\w+", url, re.I):
            return url
        # Sirf /t/ short-link resolve karo — yahi video par redirect hota hai
        if re.search(r"snapchat\.com/t/", url, re.I):
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            # HTTPRedirectHandler default opener me already hota hai
            with urllib.request.urlopen(req, timeout=5) as res:
                final = res.geturl() or url
            if final and final != url:
                return final
    except Exception:
        pass
    return url


def _is_snapchat_url(url: str) -> bool:
    try:
        return "snapchat.com" in (url or "").lower()
    except Exception:
        return False


def _fresh_format_url(page: str, format_id: str):
    """403/expire par page dobara extract karke usi format ka fresh URL nikalo."""
    try:
        with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
            info = ydl.extract_info(page, download=False)
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else {}
        for f in info.get("formats") or []:
            if str(f.get("format_id")) == str(format_id) and f.get("url"):
                return f["url"]
    except Exception:
        pass
    return None


BAD_PROTOCOLS = {"mhtml", "m3u8", "m3u8_native", "http_dash_segments", "rtmp", "rtsp", "f4m", "ism"}


def _codec_rank(vcodec):
    v = (vcodec or "").lower()
    if v.startswith("avc1") or v.startswith("h264"):
        return 3
    if "vp9" in v or v.startswith("vp09"):
        return 2
    if "av01" in v or "av1" in v:
        return 1
    if v not in ("none", ""):
        return 2
    return 0


def _probe_mp4_tracks(url, timeout=5):
    """MP4 ke pehle 1MB (Range) se (width, height, has_audio). Fail-soft.

    has_audio: True = audio track pakka hai, False = moov poora mila aur
    audio track NAHI hai (video-only file), None = pata nahi chala
    (network fail / adhura moov — purana assumption barkarar rakho).
    (FIX: Instagram music-reels ki video_versions files unknown-codec
    hote hue bhi video-ONLY hoti hain — bina verify direct download
    karne par BINA AAWAZ wali file milti thi.)
    NOTE: Range 1MB tak — kuch IG files ka moov thoda aage hota hai,
    300KB me 'moov' nahi milta tha aur silent-file pakdi nahi jati thi.
    """
    import struct
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA,
                          "Range": "bytes=0-1048575", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            d = res.read(1100000)
        if len(d) < 128 or b"moov" not in d:
            return None
        pos, best = 0, None
        while True:
            j = d.find(b"tkhd", pos)
            if j < 0:
                break
            try:
                ver = d[j + 4]
                base = j + 80 if ver == 0 else j + 92
                w, h = struct.unpack(">II", d[base:base + 8])
                w, h = w / 65536.0, h / 65536.0
                if w > 0 and h > 0 and (best is None or w * h > best[0] * best[1]):
                    best = (w, h)
            except Exception:
                pass
            pos = j + 4
        # audio track? hdlr box ka handler_type: 'vide' vs 'soun'.
        # 'soun' milna = audio pakka. 'vide'-only tabhi video-only mano
        # jab moov box POORA buffer me ho (warna adhure moov par galat faisla).
        handlers = set()
        pos = 0
        while True:
            j = d.find(b"hdlr", pos)
            if j < 0:
                break
            try:
                if j + 16 <= len(d):
                    handlers.add(d[j + 12:j + 16].decode("latin1"))
            except Exception:
                pass
            pos = j + 4
        if "soun" in handlers:
            has_audio = True
        elif "vide" in handlers and _moov_complete(d):
            has_audio = False
        else:
            has_audio = None
        if best:
            return (int(best[0]), int(best[1]), has_audio)
        if has_audio is not None:
            return (0, 0, has_audio)
        return None
    except Exception:
        return None


def _moov_complete(d):
    """moov box ka size padhkar batao poora buffer me hai ya kata hua."""
    import struct
    try:
        j = d.find(b"moov")
        while 0 <= j:
            if j >= 4:
                size = struct.unpack(">I", d[j - 4:j])[0]
                if 8 <= size <= 100 * 1024 * 1024:
                    return (j - 4 + size) <= len(d)
            j = d.find(b"moov", j + 4)
            if j < 0:
                return False
        return False
    except Exception:
        return False


def _probe_mp4_dims(url, timeout=4):
    """Purana wrapper (dims hi chahiye ho to) — audio flag chhod do."""
    try:
        r = _probe_mp4_tracks(url, timeout)
        if r and min(r[0], r[1]) > 0:
            return (r[0], r[1])
    except Exception:
        pass
    return None


def _fill_missing_heights(formats, limit=6):
    """Height-missing/guessed entries (Facebook sd/hd) ki EXACT resolution
    + codec-unknown (FB/IG guess-progressive) entries me ASLI audio-track
    verify karo. Video-only nikle to merge-path par bhejo (audio-sahit),
    warna BINA AAWAZ download hota hai.

    FIX (IG silent-reels): probe me video-only CONFIRM ho to HAMESHA
    merge-path par bhejo — chahe alag audio-track list me dikhe ya na
    dikhe. Pehle `has_any_audio` False hone par silent-file hi
    ★1-Tap bankar milti thi. Ab needs_merge=True hoga to downloader
    khud best audio-sahit file dega (fallback), silent file kabhi nahi.

    SPEED: probes PARALLEL (3 workers) — sequential me 2-3 entries par
    10-20s lag jata tha, ab ~2-4s me ho jata hai. Har probe max ~5s.
    """
    try:
        has_any_audio = any((x.get("type") == "audio" and x.get("url"))
                            for x in (formats or []))
    except Exception:
        has_any_audio = False
    targets = []
    for c in formats or []:
        try:
            if c.get("type") != "video":
                c.pop("_guessed", None)
                c.pop("_verify_audio", None)
                continue
            need_dims = (c.get("height") or 0) <= 0 or c.get("_guessed")
            need_audio = bool(c.get("_verify_audio"))
            if (not need_dims and not need_audio) \
                    or (c.get("ext") or "") not in ("mp4", "m4v", "mov"):
                c.pop("_guessed", None)
                c.pop("_verify_audio", None)
                continue
            u = c.get("url") or ""
            if not u.startswith("http"):
                c.pop("_guessed", None)
                c.pop("_verify_audio", None)
                continue
            if limit <= 0:
                c.pop("_guessed", None)
                c.pop("_verify_audio", None)
                continue
            limit -= 1
            targets.append((c, need_dims))
        except Exception:
            try:
                c.pop("_guessed", None)
                c.pop("_verify_audio", None)
            except Exception:
                pass
            continue
    if targets:
        ex = None
        try:
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=3)
            futs = {ex.submit(_probe_mp4_tracks, (c.get("url") or ""), 3): (c, nd)
                        for c, nd in targets}
            for fut, (c, need_dims) in futs.items():
                try:
                    d = fut.result(timeout=4)
                except Exception:
                    d = None
                try:
                    if d:
                        w, h, ha = d
                        if need_dims and w and h and min(w, h) > 0:
                            c["height"] = min(w, h)
                            c["label"] = "%dp" % min(w, h)
                        # Video-only CONFIRM = merge-path (HAMESHA).
                        # Alag audio ho to browser/server merge karega;
                        # na ho to downloader fallback best 1-Tap dega.
                        # (direct download BINA AAWAZ deta — yahi asli bug tha).
                        if c.pop("_verify_audio", None):
                            if ha is False:
                                c["progressive"] = False
                                c["needs_merge"] = True
                                c["one_tap"] = False
                                if not has_any_audio:
                                    c["label"] = (c.get("label") or "") + " (audio-merge)"
                    else:
                        c.pop("_verify_audio", None)
                    c.pop("_guessed", None)
                except Exception:
                    try:
                        c.pop("_verify_audio", None)
                        c.pop("_guessed", None)
                    except Exception:
                        pass
        except Exception:
            for c, _nd in targets:
                try:
                    c.pop("_verify_audio", None)
                    c.pop("_guessed", None)
                except Exception:
                    pass
        finally:
            try:
                if ex is not None:
                    ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
    try:
        formats.sort(key=lambda x: (1 if x.get("progressive") else 0,
                                    x.get("height") or 0, x.get("tbr") or 0),
                     reverse=True)
    except Exception:
        pass


def pick_formats(formats_raw, duration=None):
    """Filter junk, dedupe per quality, sort best-first.

    FIX: HLS/DASH manifest URLs (m3u8_native / manifest.googlevideo.com)
    direct-download nahi hote — inhe skip karo, warna 'failed' aata hai.
    Har height par mp4-avc (merge-safe) + best-quality dono rakho.
    """
    cands = []
    for f in formats_raw:
        fid = str(f.get("format_id") or "")
        proto = (f.get("protocol") or "").lower()
        url = f.get("url") or ""
        if not url:
            continue
        if proto in BAD_PROTOCOLS:
            continue
        if "manifest.googlevideo.com" in url:
            continue
        if ("storyboard" in fid or fid.startswith("sb")) and not f.get("height"):
            continue
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        if vcodec == "unknown":
            vcodec = "none"
        if acodec == "unknown":
            acodec = "none"
        # raw codec unknown hai ya explicitly-none? (Instagram video_versions me
        # acodec=None = "pata nahi, lekin audio ho sakta hai", jabki DASH
        # video-only me acodec="none" explicit hota hai.)
        raw_acodec_unknown = f.get("acodec") is None
        raw_vcodec_unknown = f.get("vcodec") is None
        has_video = vcodec not in ("none", None, "")
        has_audio = acodec not in ("none", None, "")
        ext = (f.get("ext") or "").lower()
        if not ext:
            # EXT-INFER (Instagram video_versions me ext nahi aata): URL se nikalo.
            try:
                _path = (url.split("?", 1)[0].rsplit("/", 1)[-1] or "")
                if "." in _path:
                    _e = _path.rsplit(".", 1)[-1].lower()
                    if _e in ("mp4", "m4v", "mov", "webm", "mkv", "m4a",
                              "mp3", "wav", "ogg", "opus", "flac", "aac"):
                        ext = _e
            except Exception:
                pass
        # SPARSE-FIX (Facebook sd/hd): codec/height nahi aate, lekin https-mp4
        # progressive hota hai (video+audio ek file, BINA LOGIN). Phenko mat.
        # NOTE: kuch files (IG music-reels) video-ONLY hoti hain — _verify_audio
        # flag se _fill_missing_heights me asli audio-track verify hoga.
        verify_audio = False
        if not has_video and not has_audio and ext in ("mp4", "m4v", "mov") \
                and proto in ("https", "http"):
            has_video, has_audio = True, True
            vcodec, acodec = "avc1", "mp4a"
            verify_audio = True
        # ext ab bhi khaali ho aur URL Instagram/FB CDN ka ho to mp4 mano
        # (signed-URL me kabhi extension nahi hota, phir bhi progressive mp4 hai).
        if not ext and has_video and proto in ("https", "http"):
            try:
                _u = url.lower()
                if ".mp4" in _u or "instagram" in _u or "fbcdn" in _u or "scontent" in _u:
                    ext = "mp4"
            except Exception:
                pass
        # INSTAGRAM-FIX (video_versions direct-mp4): vcodec to hai (h264/avc),
        # lekin acodec=None (unknown) aata hai jabki audio FILE ME HI hota hai.
        # yt-dlp DASH video-only me acodec="none" EXPLICIT deta hai — wahan ye
        # fix lagna nahi chahiye. Sirf unknown-acodec + direct https-mp4 +
        # height/width wali entry ko progressive mano (audio-sahit, 1-tap).
        if has_video and not has_audio and raw_acodec_unknown \
                and ext in ("mp4", "m4v", "mov") and proto in ("https", "http") \
                and (f.get("height") or f.get("width")):
            has_audio = True
            acodec = "mp4a"
            verify_audio = True
        if not has_video and not has_audio:
            continue
        if ext == "mp4" and not has_video and not has_audio:
            continue
        height = f.get("height")
        _guessed_h = False
        if not height:
            fl = fid.lower()
            if fl == "hd":
                height, _guessed_h = 720, True
            elif fl == "sd":
                height, _guessed_h = 360, True
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

        is_drc = fid.endswith("-drc") or "drc" in fid.lower()
        cands.append({
            "format_id": fid, "ext": ext, "type": ftype,
            "label": label, "height": height, "abr": abr,
            "filesize": filesize, "tbr": tbr, "url": url,
            "progressive": progressive, "needs_merge": has_video and not has_audio,
            # VidMate style 1-tap: progressive = single download, merge-zero-fail
            "one_tap": progressive,
            "vcodec": vcodec, "acodec": acodec, "protocol": proto,
            "_vrank": _codec_rank(vcodec) if has_video else 0,
            "_crank": 1 if ext in ("mp4", "m4a") else 0,
            "_drc": 1 if is_drc else 0,
            "_guessed": 1 if _guessed_h else 0,
            "_verify_audio": 1 if (verify_audio and progressive) else 0,
        })

    # dedupe: keep best per (type, height, progressive, container-family)
    best = {}
    for c in cands:
        if c["type"] == "video":
            fam = "mp4" if c["ext"] in ("mp4", "m4v") else c["ext"]
            key = ("v", c.get("height") or 0, c["progressive"], fam)
        else:
            key = ("a", int((c.get("abr") or 0) // 32), c["ext"])
        old = best.get(key)
        score = (c["_vrank"], c["_crank"], -c["_drc"], c["tbr"] or 0)
        oscore = (old["_vrank"], old["_crank"], -old["_drc"], old["tbr"] or 0) if old else None
        if old is None or score > oscore:
            best[key] = c
    formats = list(best.values())
    by_h = {}
    for c in formats:
        if c["type"] == "video":
            by_h.setdefault(c.get("height") or 0, []).append(c)
    keep = set()
    for h, lst in by_h.items():
        lst.sort(key=lambda x: (x["_vrank"], x["_crank"], -x["_drc"], x["tbr"] or 0), reverse=True)
        for c in lst[:2]:
            keep.add(id(c))
    formats = [c for c in formats if c["type"] != "video" or id(c) in keep]
    for c in formats:
        for k in ("_vrank", "_crank", "_drc"):
            c.pop(k, None)

    def _sort(x):
        return (1 if x.get("progressive") else 0, (x.get("height") or 0), x.get("tbr") or 0)
    formats.sort(key=_sort, reverse=True)
    return formats[:60]


def best_audio(formats):
    aud = [f for f in formats if f["type"] == "audio"]
    if not aud:
        return None
    aud.sort(key=lambda x: (x.get("abr") or 0, x.get("tbr") or 0,
                             0 if x["ext"] == "m4a" else 1), reverse=True)
    a = aud[0]
    return {"url": a["url"], "ext": a["ext"] or "m4a", "abr": a.get("abr"),
            "format_id": a.get("format_id", "")}


def pick_preview_and_compat(formats):
    prog = [f for f in formats if f.get("progressive")]
    # Preview HALKA hona chahiye: sabse CHHOTA progressive (low height, low bitrate)
    # taaki preview turant chale aur data kam lage. prog[0] mat lo — formats
    # best-first sort hote hain, isliye prog[0] sabse BADA (heavy) hota hai.
    preview = min(prog, key=lambda x: ((x.get("height") or 0), x.get("tbr") or 0)) if prog else None
    if preview is None:
        vids = sorted([f for f in formats if f["type"] == "video"],
                      key=lambda x: ((x.get("height") or 0), x.get("tbr") or 0))
        preview = vids[0] if vids else None
    aud = [f for f in formats if f["type"] == "audio"]
    mp4a = sorted([f for f in aud if f["ext"] in ("m4a", "mp4")],
                  key=lambda x: (x.get("abr") or 0, x.get("tbr") or 0), reverse=True)
    webma = sorted([f for f in aud if f["ext"] in ("webm", "weba", "opus")],
                   key=lambda x: (x.get("abr") or 0, x.get("tbr") or 0), reverse=True)

    def _mini(f):
        return {"url": f["url"], "ext": f["ext"] or "m4a", "abr": f.get("abr"),
                "format_id": f.get("format_id", "")} if f else None
    return (
        preview["url"] if preview else "",
        preview["ext"] if preview else "",
        _mini(mp4a[0]) if mp4a else best_audio(formats),
        _mini(webma[0]) if webma else best_audio(formats),
        bool(preview.get("progressive")) if preview else False,
    )


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


_YT_AUTH_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
}


def _cookie_status():
    """cookies.txt / YOUTUBE_COOKIES ka health-check (names+expiry only, values kabhi nahi).

    Bar-bar AUTH_REQUIRED aane ka #1 kaaran mri hui ya adhoori YouTube cookies
    hoti hain — ye endpoint 2 second me bata deta hai cookies zinda hain ya nahi.
    """
    out = {
        "source": "env:YOUTUBE_COOKIES" if _ENV_COOKIES else "file:cookies.txt",
        "file_exists": False,
        "total_entries": 0,
        "youtube_entries": 0,
        "youtube_auth_valid": [],
        "youtube_auth_expired": [],
        "youtube_auth_missing": sorted(_YT_AUTH_NAMES),
        "valid": False,
        "message": "",
    }
    path = COOKIES
    try:
        out["file_exists"] = os.path.exists(path) and os.path.getsize(path) > 2
        if not out["file_exists"]:
            out["message"] = (
                "Koi cookies nahi mili (na YOUTUBE_COOKIES env, na cookies.txt). "
                "YouTube datacenter-IP bot-check dega. Fresh YouTube login cookies "
                "YOUTUBE_COOKIES env me dalo.")
            return out
        now = time.time()
        seen_valid, seen_expired = set(), set()
        total, yt_total = 0, 0
        with open(path, "r", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                try:
                    total += 1
                    domain, name = parts[0].lower(), parts[5]
                    exp = int(parts[4])
                except Exception:
                    continue
                if "youtube.com" not in domain:
                    continue
                yt_total += 1
                if name not in _YT_AUTH_NAMES:
                    continue
                if exp > now:
                    seen_valid.add(name)
                else:
                    seen_expired.add(name)
        out["total_entries"] = total
        out["youtube_entries"] = yt_total
        out["youtube_auth_valid"] = sorted(seen_valid)
        out["youtube_auth_expired"] = sorted(seen_expired - seen_valid)
        out["youtube_auth_missing"] = sorted(n for n in _YT_AUTH_NAMES
                                            if n not in seen_valid)
        need = {"SID", "HSID", "SSID"}
        if need.issubset(seen_valid):
            out["valid"] = True
            out["message"] = "YouTube auth cookies zinda hain."
        else:
            missing = sorted(need - seen_valid)
            out["message"] = (
                "YouTube auth cookies MISSING/EXPIRED: %s. Isi liye bar-bar "
                "'Sign in to confirm you are not a bot' aata hai. Browser me "
                "YouTube login karo → Get cookies.txt se export → YOUTUBE_COOKIES "
                "env (production) ya cookies.txt (local) replace karo → restart."
                % ", ".join(missing))
    except Exception as e:
        out["message"] = "Cookie check fail: %s" % str(e)[:120]
    return out


@app.get("/api/cookies/status")
def cookies_status():
    return _cookie_status()


@app.on_event("startup")
def _log_cookie_status():
    try:
        st = _cookie_status()
        print("cookies: source=%s valid=%s yt_entries=%d msg=%s" % (
            st["source"], st["valid"], st["youtube_entries"], st["message"][:160]))
    except Exception:
        pass


def _search_sync(query, limit=12):
    """YouTube search (Watch-tab). Blocking — caller thread me chalao."""
    q = (query or "").strip()
    if not q:
        return {"error": "Search text khaali hai."}
    try:
        limit = max(1, min(int(limit or 12), 25))
    except Exception:
        limit = 12
    fopts = dict(base_ydl_opts())
    fopts.update({
        "skip_download": True,
        "extract_flat": True,
        "socket_timeout": 10,
    })
    # NOTE: custom UA par YouTube search khaali milta hai — default UA rakho.
    try:
        fopts.pop("http_headers", None)
    except Exception:
        pass
    try:
        with yt_dlp.YoutubeDL(fopts) as ydl:
            info = ydl.extract_info("ytsearch%d:%s" % (limit, q), download=False)
    except Exception as e:  # noqa: BLE001
        return {"error": friendly_error(e)}
    out = []
    try:
        for e in (info.get("entries") or [])[:limit]:
            if not isinstance(e, dict):
                continue
            vid = e.get("id") or ""
            if not vid:
                continue
            url = e.get("url") or e.get("webpage_url") or ""
            if not url or not url.startswith("http"):
                url = "https://www.youtube.com/watch?v=" + vid
            title = e.get("title") or vid
            thumb = ""
            try:
                ths = e.get("thumbnails") or []
                if ths:
                    thumb = (ths[-1] or {}).get("url") or ""
            except Exception:
                pass
            if not thumb:
                thumb = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
            try:
                dur = int(e.get("duration") or 0)
            except Exception:
                dur = 0
            try:
                views = int(e.get("view_count") or 0)
            except Exception:
                views = 0
            out.append({"id": vid, "title": title, "url": url,
                        "thumbnail": thumb, "duration": dur,
                        "channel": e.get("channel") or e.get("uploader") or "",
                        "views": views})
    except Exception:
        pass
    return {"results": out, "count": len(out)}


@app.post("/api/search")
async def api_search(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    q = ((body.get("q") or body.get("query") or "")).strip()
    if not q:
        return JSONResponse({"error": "Search text khaali hai."}, status_code=400)
    try:
        limit = int(body.get("limit") or 12)
    except Exception:
        limit = 12
    try:
        res = await asyncio.to_thread(_search_sync, q, limit)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": friendly_error(e)})
    return JSONResponse(res)


def _flat_entries_from_pinfo(pinfo, url):
    """Flat playlist info -> Up-Next entries. Fail -> khaali."""
    out, title, count = [], "", 0
    try:
        if not pinfo or pinfo.get("_type") != "playlist":
            return out, title, count
        title = pinfo.get("title") or ""
        count = pinfo.get("playlist_count") or 0
        for e in (pinfo.get("entries") or [])[:20]:
            if not e:
                continue
            vid = e.get("id") or ""
            wurl = e.get("webpage_url") or e.get("url") or ""
            if wurl.startswith("/"):
                wurl = "https://www.youtube.com" + wurl
            if not wurl and vid and "youtube" in (pinfo.get("extractor") or "").lower():
                wurl = "https://www.youtube.com/watch?v=" + vid
            thumbs = e.get("thumbnails") or []
            thumb = ""
            try:
                if thumbs:
                    thumb = (thumbs[-1] or {}).get("url") or ""
            except Exception:
                thumb = ""
            if not thumb:
                thumb = e.get("thumbnail") or ""
            if not thumb and vid and "youtube" in (pinfo.get("extractor") or "").lower():
                thumb = "https://i.ytimg.com/vi/%s/hqdefault.jpg" % vid
            out.append({
                "id": vid,
                "title": e.get("title") or vid,
                "url": wurl or url,
                "thumbnail": thumb,
                "duration": e.get("duration") or 0,
            })
        if not count:
            count = len(out)
    except Exception:
        pass
    return out, title, count


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
    # Snapchat /t/ short-link ho to redirect resolve karke asli page nikalo
    # (blocking network — event-loop free rakhne ke liye thread me)
    try:
        if _is_snapchat_url(url):
            url = await asyncio.to_thread(normalize_snapchat_url, url)
    except Exception:
        pass
    # Snapchat: yt-dlp SIRF spotlight/<id> samajhta hai. Baaki sab pattern
    # (/add/, /discover/, /t/ jo spotlight par na ruke...) par yt-dlp ko
    # bhejna time waste hai (8-10s latak kar fail hota) — turant saaf jawab do.
    try:
        if _is_snapchat_url(url) and not re.search(
                r"snapchat\.com/spotlight/\w+", url, re.I):
            return JSONResponse({"error": (
                "Ye Snapchat link support nahi hai. Sirf Spotlight links chalte hain "
                "(snapchat.com/spotlight/...). Snapchat app me video kholo → Share → "
                "Copy Link karke Spotlight link paste karo.")}, status_code=400)
    except Exception:
        pass

    # Repeat URL = turant (memory cache, 5 min) — Recent reopen, 403 self-heal,
    # double-tap Get Video par yt-dlp dobara network par nahi jata.
    try:
        hit = _cache_get(url)
        if hit is not None:
            return hit
    except Exception:
        pass

    # SPEED: flat-playlist + related-videos main extract ke SAATH parallel me
    # chalao — pehle ye sequential the (2-3 extra round-trip = kayi second).
    def _looks_playlist(u):
        ul = u.lower()
        return ("list=" in ul or "/playlist" in ul or "/channel/" in ul
                or "/@" in ul or "/c/" in ul or "/user/" in ul)

    def _run_flat():
        fo = base_ydl_opts()
        fo.update({"extract_flat": True, "playlistend": 20})
        with yt_dlp.YoutubeDL(fo) as ydl:
            return ydl.extract_info(url, download=False)

    flat_task = None
    if _looks_playlist(url):
        try:
            flat_task = asyncio.create_task(asyncio.to_thread(_run_flat))
        except Exception:
            flat_task = None

    # YouTube id URL se hi pata hai to Related API bhi saath me chalao
    spec_vid = ""
    try:
        ul = url.lower()
        if "youtube.com" in ul or "youtu.be" in ul:
            spec_vid = _parse_youtube_id(url)
    except Exception:
        spec_vid = ""
    rel_task = None
    if spec_vid:
        try:
            rel_task = asyncio.create_task(
                asyncio.to_thread(_youtube_related_sync, spec_vid, 15))
        except Exception:
            rel_task = None

    def _cancel_bg():
        for t in (flat_task, rel_task):
            try:
                if t is not None and not t.done():
                    t.cancel()
            except Exception:
                pass

    def _run():
        with yt_dlp.YoutubeDL(base_ydl_opts()) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_run)
    except Exception as e:
        _cancel_bg()
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    if not info:
        _cancel_bg()
        return JSONResponse({"error": "Video info nahi mila"}, status_code=400)

    # Up-Next: speculative flat-task ka result uthao (pehle se chal raha tha)
    # FAIL-FAST (live fix): 4s me na aaye to khaali — main video turant do.
    # Pehle `await flat_task` bina timeout tha = playlist/channel slow hone
    # par poora /api/extract wahin atka rehta tha ("fetch par atka").
    playlist_entries, playlist_title, playlist_count = [], "", 0
    if flat_task is not None:
        try:
            pinfo = await asyncio.wait_for(flat_task, timeout=4)
        except Exception:
            pinfo = None
            try:
                if not flat_task.done():
                    flat_task.cancel()
            except Exception:
                pass
        try:
            playlist_entries, playlist_title, playlist_count = \
                _flat_entries_from_pinfo(pinfo, url)
        except Exception:
            playlist_entries, playlist_title, playlist_count = [], "", 0

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

    # Single YouTube video ho (playlist entries nahi mili) to Related videos
    # nikalo — speculative task pehle se chal raha tha, bas result uthao.
    # FAIL-FAST: Related ke liye main response ko 10s+ mat roko (max ~4s).
    if not playlist_entries and info.get("_type") != "playlist":
        try:
            ext = (info.get("extractor") or "").lower()
            if "youtube" in ext and info.get("id"):
                rel = None
                if rel_task is not None and spec_vid and spec_vid == info.get("id"):
                    try:
                        rel = await asyncio.wait_for(rel_task, timeout=4)
                    except Exception:
                        rel = None
                        try:
                            if not rel_task.done():
                                rel_task.cancel()
                        except Exception:
                            pass
                if rel is None:
                    # speculative id match nahi hua — purana task hatao, fresh nikalo
                    try:
                        if rel_task is not None and not rel_task.done():
                            rel_task.cancel()
                    except Exception:
                        pass
                    try:
                        rel = await asyncio.wait_for(
                            asyncio.to_thread(_youtube_related_sync, info.get("id"), 15),
                            timeout=7)
                    except Exception:
                        rel = []
                if rel:
                    playlist_entries, playlist_title, playlist_count = (
                        rel, "Related videos", len(rel))
        except Exception:
            pass
    else:
        _cancel_bg()

    duration = info.get("duration")
    formats = pick_formats(info.get("formats") or [], duration)
    # HLS-only sites (1600+ wada): m3u8 entries alag rakho — server ffmpeg se mp4 banega
    hls_list = []
    try:
        for f in info.get("formats") or []:
            proto = (f.get("protocol") or "").lower()
            u = f.get("url") or ""
            if not u:
                continue
            if proto in ("m3u8", "m3u8_native") or u.endswith(".m3u8") or ".m3u8?" in u:
                h = f.get("height") or 0
                note = f.get("format_note") or (f"{h}p" if h else "HLS")
                hls_list.append({
                    "format_id": str(f.get("format_id") or "hls"),
                    "ext": "mp4", "label": note, "height": h,
                    "url": u, "tbr": f.get("tbr") or 0,
                })
                if len(hls_list) >= 10:
                    break
    except Exception:
        hls_list = []
    if not formats and info.get("url"):
        # DIRECT-FIX (Snapchat Spotlight / direct .mp4): yt-dlp "formats" list
        # nahi deta — seedha top-level "url" deta hai. Use 1-tap progressive banao.
        u = (info.get("url") or "").strip()
        if u and "manifest.googlevideo.com" not in u:
            ext0 = (info.get("ext") or "mp4").lower()
            formats = [{
                "format_id": "direct", "ext": ext0, "type": "video",
                "label": info.get("format_note") or "Direct",
                "height": info.get("height") or 0, "abr": info.get("abr") or 0,
                "filesize": info.get("filesize") or info.get("filesize_approx"),
                "tbr": info.get("tbr") or 0, "url": u,
                "progressive": True, "one_tap": True, "needs_merge": False,
                "vcodec": info.get("vcodec") or "avc1",
                "acodec": info.get("acodec") or "mp4a",
                "protocol": info.get("protocol") or "https",
            }]
    if not formats:
        if hls_list:
            # HLS-only: direct nahi, par server se mp4 ban sakta hai
            resp = {
                "title": info.get("title", "Video"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": duration,
                "views": info.get("view_count"),
                "author": info.get("uploader") or info.get("channel") or "",
                "source": info.get("extractor", ""),
                "ext": "mp4",
                "webpage_url": info.get("webpage_url") or url,
                "formats": [],
                "best_audio": None,
                "best_audio_mp4": None,
                "best_audio_webm": None,
                "preview_url": "",
                "preview_ext": "",
                "preview_has_audio": False,
                "server_merge": HAS_FFMPEG,
                "hls": hls_list,
                "hls_only": True,
                "playlist": playlist_entries,
                "playlist_title": playlist_title,
                "playlist_count": playlist_count,
            }
            _cache_put(url, resp)
            return resp
        return JSONResponse(
            {"error": "Is URL se koi direct-download link nahi mila (HLS-only/private ho sakta hai). Dusri quality ya video try karo."},
            status_code=400,
        )
    # Facebook sd/hd ki EXACT resolution probe karo (label sahi aaye).
    # to_thread me — event-loop block na ho (parallel probes, ~2-4s).
    # FAIL-FAST: probe me atke to video rokna nahi — max 10s.
    try:
        await asyncio.wait_for(asyncio.to_thread(_fill_missing_heights, formats), timeout=10)
    except Exception:
        pass

    preview_url, preview_ext, compat_mp4, compat_webm, preview_has_audio = pick_preview_and_compat(formats)
    resp = {
        "title": info.get("title", "Video"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration,
        "views": info.get("view_count"),
        "author": info.get("uploader") or info.get("channel") or "",
        "source": info.get("extractor", ""),
        "ext": info.get("ext", "mp4"),
        "webpage_url": info.get("webpage_url") or url,
        "channel_id": info.get("channel_id") or "",
        "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
        "formats": formats,
        "best_audio": best_audio(formats),
        "best_audio_mp4": compat_mp4,
        "best_audio_webm": compat_webm,
        "preview_url": preview_url,
        "preview_ext": preview_ext,
        "preview_has_audio": preview_has_audio,
        "server_merge": HAS_FFMPEG,
        "hls": hls_list,
        "hls_only": False,
        "playlist": playlist_entries,
        "playlist_title": playlist_title,
        "playlist_count": playlist_count,
    }
    _cache_put(url, resp)
    return resp


@app.get("/api/server-download")
async def server_download(request: Request):
    """1600+ sites fallback: HLS/DASH/special pages jinka direct link nahi
    milta, unhe server yt-dlp+ffmpeg se mp4 banakar deta hai.
    (Server bandwidth/disk lagti hai — direct 1-tap mile to wahi use karo.)"""
    from urllib.parse import unquote
    if not HAS_FFMPEG:
        return JSONResponse(
            {"error": "Server par ffmpeg nahi hai — direct quality use karo."},
            status_code=501,
        )
    page = request.query_params.get("page", "")
    filename = request.query_params.get("filename", "video")
    format_id = request.query_params.get("format_id", "")
    if not page or not re.match(r"^https?://", page, re.I):
        return JSONResponse({"error": "page URL required"}, status_code=400)
    safe_name = safe_filename(unquote(filename), "video")
    if not safe_name.lower().endswith(".mp4"):
        safe_name += ".mp4"
    tmp_dir = tempfile.mkdtemp()
    outtmpl = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    def _run():
        opts = base_ydl_opts()
        # Chuni hui quality (HD merge): us format + best audio, warna best
        fmt_str = f"{format_id}+bestaudio/best" if format_id else "bestvideo+bestaudio/best"
        opts.update({
            "outtmpl": outtmpl,
            "format": fmt_str,
            "merge_output_format": "mp4",
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(page, download=True)
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
        filename=safe_name,
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


@app.get("/api/stream")
async def stream_file(request: Request):
    """VidMate-style MAIN path: browser ka Download Manager (notification bar)
    is URL ko pakadta hai — % progress, speed, pause/resume sab native milta hai.

    Iske liye zaroori headers forward hote hain:
    - Content-Length / Content-Type (warna % atka rehta hai)
    - Range -> upstream (206 + Content-Range, resume + multi-connection)
    - 403/expire par page+format_id se FRESH link self-heal
    - read-timeout NONE (badi file beech me na kate), ratebypass upstream
    """
    from urllib.parse import unquote, quote
    url = request.query_params.get("url", "")
    filename = request.query_params.get("filename", "video.mp4")
    page = request.query_params.get("page", "")
    format_id = request.query_params.get("format_id", "")
    if not url:
        return JSONResponse({"error": "URL required"}, status_code=400)
    safe_name = safe_filename(unquote(filename), "video.mp4")
    url = _with_bypass(url)

    client_range = request.headers.get("range")

    async def _open(u, range_hdr):
        c = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0, read=None),  # badi file: read kabhi timeout na ho
        )
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if range_hdr:
            h["Range"] = range_hdr
        try:
            r = await c.send(c.build_request("GET", u, headers=h), stream=True)
        except Exception:
            await c.aclose()
            raise
        return c, r

    client, upstream = None, None
    try:
        client, upstream = await _open(url, client_range)
        if upstream.status_code in (403, 410) and page:
            # Link expire — fresh link nikalo aur wahin se retry (user ko pata bhi na chale)
            fresh = await asyncio.to_thread(_fresh_format_url, page, format_id)
            if fresh:
                try:
                    await upstream.aclose()
                    await client.aclose()
                except Exception:
                    pass
                url = _with_bypass(fresh)
                client, upstream = await _open(url, client_range)
        if upstream.status_code in (403, 410):
            raise RuntimeError("Link expire ho gaya (403). Dobara Get Video dabao, phir turant download karo.")
        upstream.raise_for_status()
    except RuntimeError as e:
        try:
            if upstream is not None:
                await upstream.aclose()
        except Exception:
            pass
        try:
            if client is not None:
                await client.aclose()
        except Exception:
            pass
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        try:
            if upstream is not None:
                await upstream.aclose()
        except Exception:
            pass
        try:
            if client is not None:
                await client.aclose()
        except Exception:
            pass
        return JSONResponse({"error": friendly_error(e)}, status_code=502)

    status = 206 if upstream.status_code == 206 else 200
    ctype = (upstream.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
    out_headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{quote(safe_name)}',
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if upstream.headers.get("Content-Range") and status == 206:
        out_headers["Content-Range"] = upstream.headers["Content-Range"]
    if upstream.headers.get("Content-Length"):
        out_headers["Content-Length"] = upstream.headers["Content-Length"]

    async def gen():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=256 * 1024):
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass

    return StreamingResponse(gen(), status_code=status, media_type=ctype or "application/octet-stream",
                             headers=out_headers)


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
