from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from ytmusicapi import YTMusic
from typing import Optional, Dict, Any
import os, json, traceback

app = FastAPI(title="NRN Music API")

# CORS PALING LONGGAR — pastikan preflight tidak gagal
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Cache instance anonymous (dengan visitor_data untuk kurangi 403 bot flag)
_yt_anon: Optional[YTMusic] = None

def get_anon() -> YTMusic:
    global _yt_anon
    if _yt_anon is None:
        try:
            _yt_anon = YTMusic(auth="visitor_data")
        except Exception:
            # Fallback kalau visitor_data gagal
            _yt_anon = YTMusic()
    return _yt_anon

def get_authed(token_json: Optional[str]) -> YTMusic:
    """Kalau ada token, buat instance authed. Kalau tidak, return anonymous."""
    if not token_json:
        return get_anon()
    try:
        creds = json.loads(token_json)
        # ytmusicapi menerima dict token langsung
        return YTMusic(creds)
    except Exception:
        # Kalau token rusak, fallback ke anon agar app tidak mati total
        return get_anon()

# ============== UTIL: PILIH STREAM PALING BAGUS ==============
def _pick_best_stream(song: Dict[str, Any]) -> Optional[str]:
    """
    Iterasi SEMUA format audio. Pilih bitrate tertinggi yang punya url tidak kosong.
    TIDAK terpaku itag tertentu — Google sering ubah-ubah.
    """
    if not song:
        return None
    streaming = song.get("streamingData") or {}
    candidates = []

    # 1. Cek adaptiveFormats (biasanya audio murni)
    for f in streaming.get("adaptiveFormats", []) or []:
        mt = (f.get("mimeType") or "").lower()
        if "audio" not in mt:
            continue
        url = f.get("url") or f.get("signatureCipher") or ""
        if not url:
            continue
        br = int(f.get("bitrate") or 0)
        candidates.append((br, url))

    # 2. Fallback ke formats (audio+video, tapi kalau cuma ini yang ada)
    for f in streaming.get("formats", []) or []:
        mt = (f.get("mimeType") or "").lower()
        if "audio" not in mt:
            continue
        url = f.get("url") or f.get("signatureCipher") or ""
        if not url:
            continue
        br = int(f.get("bitrate") or 0)
        candidates.append((br, url))

    if not candidates:
        return None

    # Urutkan bitrate DESCENDING — ambil paling jernih
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

# ============== ENDPOINT ==============

@app.get("/api/health")
def health():
    return {"ok": True}

@app.get("/api/home")
def home(token: Optional[str] = Header(None, alias="X-YTM-Token")):
    try:
        yt = get_authed(token)
        data = yt.get_home()
        # Ambil 5 shelf pertama saja — ringan buat HP
        shelves = []
        for s in (data or [])[:5]:
            title = s.get("title") or ""
            items = []
            for c in s.get("contents", []) or []:
                items.append(_slim_card(c))
            shelves.append({"title": title, "items": items})
        return {"shelves": shelves}
    except Exception as e:
        return JSONResponse({"shelves": [], "error": str(e)}, status_code=200)

@app.get("/api/search")
def search(q: str, token: Optional[str] = Header(None, alias="X-YTM-Token")):
    if not q or not q.strip():
        return {"tracks": []}
    try:
        yt = get_authed(token)
        res = yt.search(q.strip(), filter="songs", limit=20) or []
        tracks = [_slim_track(r) for r in res if r.get("videoId")]
        return {"tracks": tracks}
    except Exception as e:
        return JSONResponse({"tracks": [], "error": str(e)}, status_code=200)

@app.get("/api/song/{video_id}")
def get_song(video_id: str, token: Optional[str] = Header(None, alias="X-YTM-Token")):
    try:
        yt = get_authed(token)
        # get_song = streaming data lengkap + decrypt url
        song = yt.get_song(video_id)
        url = _pick_best_stream(song)
        if not url:
            raise HTTPException(status_code=404, detail="NO_STREAM")
        return {
            "videoId": video_id,
            "title": (song.get("videoDetails") or {}).get("title") or video_id,
            "artist": (song.get("videoDetails") or {}).get("author") or "",
            "thumbnail": _thumb((song.get("videoDetails") or {}).get("thumbnail", {})),
            "duration": int((song.get("videoDetails") or {}).get("lengthSeconds") or 0),
            "url": url,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/playlist/{pl_id}")
def playlist(pl_id: str, token: Optional[str] = Header(None, alias="X-YTM-Token")):
    try:
        yt = get_authed(token)
        limit = 100
        data = yt.get_playlist(pl_id, limit=limit) or {}
        tracks = []
        for t in data.get("tracks", []) or []:
            if t.get("videoId"):
                tracks.append(_slim_track(t))
        return {
            "title": data.get("title") or "",
            "description": data.get("description") or "",
            "count": len(tracks),
            "thumbnail": _thumb(data.get("thumbnails", [])),
            "tracks": tracks,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/album/{album_id}")
def album(album_id: str, token: Optional[str] = Header(None, alias="X-YTM-Token")):
    try:
        yt = get_authed(token)
        data = yt.get_album(album_id) or {}
        tracks = []
        for t in data.get("tracks", []) or []:
            if t.get("videoId"):
                tracks.append(_slim_track(t))
        return {
            "title": data.get("title") or "",
            "artist": (data.get("artists", [{}]) or [{}])[0].get("name", ""),
            "count": len(tracks),
            "thumbnail": _thumb(data.get("thumbnails", [])),
            "tracks": tracks,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/library")
def library(token: Optional[str] = Header(None, alias="X-YTM-Token")):
    if not token:
        raise HTTPException(status_code=401, detail="LOGIN_REQUIRED")
    try:
        yt = get_authed(token)
        liked = yt.get_liked_songs(limit=200) or {}
        playlists = yt.get_library_playlists(limit=50) or []
        albums = yt.get_library_albums(limit=50) or []
        return {
            "liked": {
                "title": "Liked Songs",
                "count": int(liked.get("trackCount") or 0),
                "thumbnail": _thumb(liked.get("thumbnails", [])),
                "tracks": [_slim_track(t) for t in (liked.get("tracks") or []) if t.get("videoId")],
            },
            "playlists": [_slim_card(p) for p in playlists if p.get("playlistId")],
            "albums": [_slim_card(a) for a in albums if a.get("browseId")],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/suggestions")
def suggestions(videoId: str, token: Optional[str] = Header(None, alias="X-YTM-Token")):
    try:
        yt = get_authed(token)
        res = yt.get_watch_playlist(videoId, limit=30) or {}
        tracks = [_slim_track(t) for t in (res.get("tracks") or []) if t.get("videoId") and t.get("videoId") != videoId]
        return {"tracks": tracks[:25]}
    except Exception as e:
        return JSONResponse({"tracks": [], "error": str(e)}, status_code=200)

# ============== OAUTH DEVICE FLOW (RFC 8628) ==============
# User bikin Client ID + Secret sendiri tipe "TVs and Limited Input devices"
# Kita HANYA meneruskan request — TIDAK ADA hardcode credential apapun.

import urllib.request, urllib.parse

@app.post("/api/auth/device")
def auth_device(body: Dict[str, Any]):
    cid = (body.get("client_id") or "").strip()
    csec = (body.get("client_secret") or "").strip()
    if not cid or not csec:
        raise HTTPException(status_code=400, detail="client_id dan client_secret wajib")
    try:
        data = urllib.parse.urlencode({
            "client_id": cid,
            "scope": "https://www.googleapis.com/auth/youtube",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/device/code",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/auth/token")
def auth_token(body: Dict[str, Any]):
    cid = (body.get("client_id") or "").strip()
    csec = (body.get("client_secret") or "").strip()
    code = (body.get("device_code") or "").strip()
    if not cid or not csec or not code:
        raise HTTPException(status_code=400, detail="parameter kurang")
    try:
        data = urllib.parse.urlencode({
            "client_id": cid,
            "client_secret": csec,
            "device_code": code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.loads(r.read().decode())
            # Tambahkan client_id + secret ke token JSON agar ytmusicapi bisa refresh
            tok["client_id"] = cid
            tok["client_secret"] = csec
            return tok
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        try:
            parsed = json.loads(body_err)
            # error: authorization_pending = user belum klik Allow — NORMAL, bukan error
            return JSONResponse(parsed, status_code=200)
        except Exception:
            raise HTTPException(status_code=e.code, detail=body_err)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============== HELPERS ==============
def _thumb(obj) -> str:
    """Ambil thumbnail terbesar yang masuk akal."""
    arr = []
    if isinstance(obj, list):
        arr = obj
    elif isinstance(obj, dict):
        arr = obj.get("thumbnails") or []
    best = ""
    best_w = 0
    for t in arr or []:
        w = int(t.get("width") or 0)
        url = t.get("url") or ""
        if url and w > best_w and w <= 500:
            best = url
            best_w = w
    if not best and arr:
        best = (arr[-1] or {}).get("url") or ""
    return best

def _slim_track(t: Dict[str, Any]) -> Dict[str, Any]:
    artists = t.get("artists") or []
    if isinstance(artists, list):
        artist_str = ", ".join([(a or {}).get("name", "") for a in artists if a])
    else:
        artist_str = str(artists or "")
    dur = t.get("duration_seconds") or t.get("duration") or 0
    if isinstance(dur, str) and ":" in dur:
        try:
            parts = dur.split(":")
            dur = sum(int(x) * 60 ** i for i, x in enumerate(reversed(parts)))
        except Exception:
            dur = 0
    return {
        "videoId": t.get("videoId"),
        "title": t.get("title") or "",
        "artist": artist_str,
        "thumbnail": _thumb(t.get("thumbnails", [])),
        "duration": int(dur or 0),
        "album": (t.get("album") or {}).get("name") if isinstance(t.get("album"), dict) else "",
    }

def _slim_card(c: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": c.get("playlistId") or c.get("browseId") or "",
        "title": c.get("title") or "",
        "subtitle": c.get("subtitle") or c.get("author") or c.get("type") or "",
        "thumbnail": _thumb(c.get("thumbnails", [])),
        "count": int(c.get("count") or c.get("trackCount") or 0),
        "kind": "playlist" if c.get("playlistId") else ("album" if c.get("type") == "Album" else "mixed"),
    }

# Static hosting untuk frontend
try:
    app.mount("/", StaticFiles(directory="public", html=True), name="static")
except Exception:
    pass
