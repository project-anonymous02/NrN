"""
NRN Music - Backend API
FastAPI serverless backend on Vercel, powered by ytmusicapi.

Endpoints:
  GET /api/health                        - Liveness check
  GET /api/search?q=&type=               - Search (type: songs|videos|albums|artists|playlists|all)
  GET /api/song/{videoId}                - Song metadata + audio stream URL
  GET /api/playlist/{playlistId}         - Playlist details + full tracklist
  GET /api/artist/{artistId}             - Artist details + top tracks
  GET /api/album/{albumId}               - Album details + full tracklist
  GET /api/trending                      - Home / trending / mood playlists
  GET /api/suggestions?videoId=          - Related tracks (autoplay next)
  GET /api/library                       - User library (requires YTMUSIC_OAUTH_JSON)

All endpoints return JSON. Streaming URLs expire ~6h; call /api/song/ right before playback.
"""

from __future__ import annotations

import json
import os
import sys
import time
import tempfile
from functools import lru_cache
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Bootstrap: ytmusicapi needs an auth file on disk. In serverless we can't
# rely on a persisted filesystem, so we materialize the env-var JSON into a
# temp file at import time and point YTMusic at it.
# ---------------------------------------------------------------------------

_AUTH_PATH: Optional[str] = None


def _materialize_auth() -> Optional[str]:
    """Write OAuth JSON from env into a temp file; return path or None."""
    raw = os.environ.get("YTMUSIC_OAUTH_JSON", "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    fd, path = tempfile.mkstemp(suffix=".json", prefix="ytm_oauth_")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    return path


_AUTH_PATH = _materialize_auth()


def _get_yt(auth: bool = False):
    """Return a YTMusic instance. Unauthenticated by default (faster)."""
    from ytmusicapi import YTMusic  # lazy import to shave cold-start ms

    if auth and _AUTH_PATH:
        try:
            return YTMusic(_AUTH_PATH)
        except Exception:
            return YTMusic()  # fall back gracefully
    return YTMusic()


# ---------------------------------------------------------------------------
# In-memory response cache (per-process, best-effort).
# ---------------------------------------------------------------------------

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300  # seconds


def _cache_get(key: str) -> Optional[Any]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, val = entry
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key: str, value: Any) -> None:
    if len(_CACHE) > 512:  # simple size cap
        _CACHE.clear()
    _CACHE[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="NRN Music API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _error_handler(request: Request, call_next):
    """Convert unhandled exceptions into tidy JSON 500s."""
    try:
        return await call_next(request)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - safety net
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": "upstream_error",
                "detail": f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_best_audio(adaptive_formats: list[dict]) -> Optional[dict]:
    """Pick the highest-quality audio-only format available."""
    audio = [
        f for f in adaptive_formats or []
        if f.get("mimeType", "").startswith("audio/")
    ]
    if not audio:
        return None
    # Prefer Opus (251 ~160kbps), fall back to AAC (140 ~128kbps)
    by_itag = {str(f.get("itag")): f for f in audio}
    for itag in ("251", "141", "140", "250", "249"):
        if itag in by_itag and by_itag[itag].get("url"):
            return by_itag[itag]
    # Generic fallback: highest bitrate
    audio.sort(key=lambda f: int(f.get("bitrate", 0)), reverse=True)
    return audio[0] if audio[0].get("url") else None


def _thumb(thumbs: list[dict], prefer: int = 500) -> str:
    """Return the thumbnail URL closest to `prefer` width."""
    if not thumbs:
        return ""
    best = thumbs[0]
    best_score = abs(int(thumbs[0].get("width", 0)) - prefer)
    for t in thumbs[1:]:
        score = abs(int(t.get("width", 0)) - prefer)
        if score < best_score:
            best_score = score
            best = t
    return best.get("url", "")


def _duration_sec(item: dict) -> int:
    """Normalize duration_seconds / length fields to int seconds."""
    if item.get("duration_seconds"):
        try:
            return int(item["duration_seconds"])
        except Exception:
            pass
    if item.get("length"):
        parts = str(item["length"]).split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            pass
    return 0


def _fmt_track(t: dict) -> dict:
    """Normalize a ytmusicapi track dict into our stable schema."""
    artists = t.get("artists") or []
    artist_names = ", ".join(a.get("name", "") for a in artists if a.get("name")) or t.get("artist", "")
    artist_ids = [a.get("id") for a in artists if a.get("id")]
    album = t.get("album") or {}
    return {
        "videoId": t.get("videoId"),
        "title": t.get("title", ""),
        "artist": artist_names,
        "artistIds": artist_ids,
        "album": album.get("name") if isinstance(album, dict) else str(album or ""),
        "albumId": album.get("id") if isinstance(album, dict) else None,
        "duration": _duration_sec(t),
        "thumbnail": _thumb(t.get("thumbnails", [])),
        "year": t.get("year"),
        "isExplicit": bool(t.get("isExplicit")),
        "resultType": t.get("resultType", "song"),
        "category": t.get("category"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "nrn-music-api",
        "auth_configured": _AUTH_PATH is not None,
        "python": sys.version.split()[0],
    }


@app.get("/api/search")
def search(
    q: str = Query(..., min_length=1, max_length=200),
    type: str = Query("all", regex="^(songs|videos|albums|artists|playlists|all)$"),
    limit: int = Query(20, ge=1, le=50),
):
    q = q.strip()
    key = f"s:{type}:{limit}:{q.lower()}"
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "cached": True, "query": q, "type": type, "items": cached}

    yt = _get_yt()
    filter_map = {
        "songs": "songs", "videos": "videos", "albums": "albums",
        "artists": "artists", "playlists": "playlists", "all": None,
    }
    raw = yt.search(q, filter=filter_map[type], limit=limit)

    items: list[dict] = []
    for r in raw:
        rt = r.get("resultType")
        if rt in ("song", "video"):
            items.append({**_fmt_track(r), "resultType": rt})
        elif rt == "album":
            items.append({
                "resultType": "album",
                "browseId": r.get("browseId"),
                "title": r.get("title", ""),
                "artist": ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name")),
                "year": r.get("year"),
                "thumbnail": _thumb(r.get("thumbnails", [])),
                "isExplicit": bool(r.get("isExplicit")),
            })
        elif rt == "artist":
            items.append({
                "resultType": "artist",
                "browseId": r.get("browseId"),
                "name": r.get("artist", r.get("title", "")),
                "shuffleId": r.get("shuffleId"),
                "radioId": r.get("radioId"),
                "thumbnail": _thumb(r.get("thumbnails", [])),
                "subscribers": r.get("subscribers"),
            })
        elif rt == "playlist":
            items.append({
                "resultType": "playlist",
                "browseId": r.get("browseId"),
                "playlistId": r.get("playlistId"),
                "title": r.get("title", ""),
                "description": r.get("description", ""),
                "count": r.get("itemCount"),
                "author": r.get("author", ""),
                "thumbnail": _thumb(r.get("thumbnails", [])),
            })

    _cache_set(key, items)
    return {"ok": True, "cached": False, "query": q, "type": type, "items": items}


@app.get("/api/song/{videoId}")
def get_song(videoId: str):
    if not videoId or len(videoId) > 16:
        raise HTTPException(400, "invalid videoId")
    key = f"song:{videoId}"
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "cached": True, **cached}

    yt = _get_yt()
    try:
        details = yt.get_song(videoId)
    except Exception as exc:
        raise HTTPException(502, f"ytmusicapi error: {exc}")

    ps = (details.get("playabilityStatus") or {}).get("status", "ERROR")
    if ps != "OK":
        raise HTTPException(403, f"Track not playable: {ps}")

    streaming = details.get("streamingData") or {}
    formats = streaming.get("adaptiveFormats") or streaming.get("formats") or []
    best = _pick_best_audio(formats)
    if not best:
        raise HTTPException(404, "No audio stream available")

    expires_in = int(streaming.get("expiresInSeconds", "21600"))
    video = (details.get("videoDetails") or {})
    micro = (details.get("microformat") or {}).get("microformatDataRenderer", {})

    result = {
        "videoId": videoId,
        "title": video.get("title", ""),
        "artist": video.get("author", ""),
        "channelId": video.get("channelId"),
        "thumbnail": _thumb(video.get("thumbnail", {}).get("thumbnails", [])),
        "duration": int(video.get("lengthSeconds", 0)),
        "viewCount": video.get("viewCount"),
        "streamUrl": best["url"],
        "mimeType": best.get("mimeType"),
        "bitrate": int(best.get("bitrate", 0)),
        "itag": best.get("itag"),
        "expiresAt": int(time.time()) + expires_in,
        "description": micro.get("description", ""),
    }
    _cache_set(key, result)
    return {"ok": True, "cached": False, **result}


@app.get("/api/playlist/{playlistId}")
def get_playlist(playlistId: str, limit: int = Query(100, ge=1, le=500)):
    if not playlistId or len(playlistId) > 64:
        raise HTTPException(400, "invalid playlistId")
    key = f"pl:{playlistId}:{limit}"
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "cached": True, **cached}

    yt = _get_yt()
    try:
        data = yt.get_playlist(playlistId, limit=limit)
    except Exception as exc:
        raise HTTPException(502, f"ytmusicapi error: {exc}")

    tracks = [_fmt_track(t) for t in (data.get("tracks") or []) if t.get("videoId")]
    result = {
        "id": playlistId,
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "author": (data.get("author") or {}).get("name") if isinstance(data.get("author"), dict) else str(data.get("author", "")),
        "year": data.get("year"),
        "duration": int(data.get("duration_seconds", 0)),
        "trackCount": int(data.get("trackCount", len(tracks))),
        "thumbnail": _thumb(data.get("thumbnails", [])),
        "tracks": tracks,
    }
    _cache_set(key, result)
    return {"ok": True, "cached": False, **result}


@app.get("/api/artist/{artistId}")
def get_artist(artistId: str):
    if not artistId or len(artistId) > 64:
        raise HTTPException(400, "invalid artistId")
    key = f"ar:{artistId}"
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "cached": True, **cached}

    yt = _get_yt()
    try:
        data = yt.get_artist(artistId)
    except Exception as exc:
        raise HTTPException(502, f"ytmusicapi error: {exc}")

    top = [_fmt_track(t) for t in (data.get("songs") or {}).get("results", []) if t.get("videoId")]
    albums = []
    for a in (data.get("albums") or {}).get("results", []):
        albums.append({
            "browseId": a.get("browseId"),
            "title": a.get("title", ""),
            "year": a.get("year"),
            "type": a.get("type"),
            "thumbnail": _thumb(a.get("thumbnails", [])),
            "isExplicit": bool(a.get("isExplicit")),
        })

    result = {
        "id": artistId,
        "name": data.get("name", ""),
        "description": data.get("description", ""),
        "subscribers": data.get("subscribers"),
        "shuffleId": data.get("shuffleId"),
        "radioId": data.get("radioId"),
        "thumbnail": _thumb(data.get("thumbnails", [])),
        "topTracks": top,
        "albums": albums,
    }
    _cache_set(key, result)
    return {"ok": True, "cached": False, **result}


@app.get("/api/album/{albumId}")
def get_album(albumId: str):
    if not albumId or len(albumId) > 64:
        raise HTTPException(400, "invalid albumId")
    key = f"al:{albumId}"
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "cached": True, **cached}

    yt = _get_yt()
    try:
        data = yt.get_album(albumId)
    except Exception as exc:
        raise HTTPException(502, f"ytmusicapi error: {exc}")

    tracks = [_fmt_track(t) for t in (data.get("tracks") or []) if t.get("videoId")]
    result = {
        "id": albumId,
        "title": data.get("title", ""),
        "artist": ", ".join(a.get("name", "") for a in (data.get("artists") or []) if a.get("name")),
        "year": data.get("year"),
        "duration": int(data.get("duration_seconds", 0)),
        "trackCount": int(data.get("trackCount", len(tracks))),
        "thumbnail": _thumb(data.get("thumbnails", [])),
        "description": data.get("description", ""),
        "isExplicit": bool(data.get("isExplicit")),
        "tracks": tracks,
    }
    _cache_set(key, result)
    return {"ok": True, "cached": False, **result}


@app.get("/api/trending")
def trending():
    cached = _cache_get("trending")
    if cached is not None:
        return {"ok": True, "cached": True, "sections": cached}

    yt = _get_yt()
    sections: list[dict] = []
    try:
        home = yt.get_home(limit=6)
        for sec in home or []:
            title = sec.get("title", "")
            contents = sec.get("contents") or []
            items: list[dict] = []
            for c in contents[:12]:
                if c.get("videoId"):
                    items.append(_fmt_track(c))
                elif c.get("playlistId"):
                    items.append({
                        "resultType": "playlist",
                        "playlistId": c.get("playlistId"),
                        "browseId": c.get("browseId"),
                        "title": c.get("title", ""),
                        "description": c.get("description", ""),
                        "count": c.get("itemCount"),
                        "author": c.get("author", {}).get("name") if isinstance(c.get("author"), dict) else str(c.get("author", "")),
                        "thumbnail": _thumb(c.get("thumbnails", [])),
                    })
                elif c.get("browseId") and c.get("resultType") == "album":
                    items.append({
                        "resultType": "album",
                        "browseId": c.get("browseId"),
                        "title": c.get("title", ""),
                        "artist": ", ".join(a.get("name", "") for a in (c.get("artists") or []) if a.get("name")),
                        "year": c.get("year"),
                        "thumbnail": _thumb(c.get("thumbnails", [])),
                    })
            if items:
                sections.append({"title": title, "items": items})
    except Exception:
        # Fall back to a few hardcoded well-known YTM mood/genre playlists
        moods = [
            ("Energy Boost", "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"),
            ("Chill Vibes", "PLFgquLnL59alcyTM2lkWJU34KtfPXQDaX"),
            ("Today's Biggest Hits", "PLFgquLnL59alxIWnf4ivu5bjPeTlsd_Xh"),
        ]
        for title, pid in moods:
            sections.append({"title": title, "items": [{"resultType": "playlist", "playlistId": pid, "title": title, "thumbnail": "", "author": "YouTube Music"}]})

    _cache_set("trending", sections)
    return {"ok": True, "cached": False, "sections": sections}


@app.get("/api/suggestions")
def suggestions(videoId: str = Query(..., min_length=1, max_length=16)):
    key = f"rec:{videoId}"
    cached = _cache_get(key)
    if cached is not None:
        return {"ok": True, "cached": True, "tracks": cached}

    yt = _get_yt()
    tracks: list[dict] = []
    try:
        # watch_playlist returns an autoplay queue seeded on videoId
        wp = yt.get_watch_playlist(videoId=videoId, limit=30)
        for t in (wp.get("tracks") or []):
            if t.get("videoId") and t.get("videoId") != videoId:
                tracks.append(_fmt_track(t))
    except Exception:
        tracks = []

    _cache_set(key, tracks[:25])
    return {"ok": True, "cached": False, "tracks": tracks[:25]}


@app.get("/api/library")
def library():
    if not _AUTH_PATH:
        raise HTTPException(
            424,
            "Library requires authentication. Set YTMUSIC_OAUTH_JSON env var with your OAuth JSON.",
        )
    cached = _cache_get("lib")
    if cached is not None:
        return {"ok": True, "cached": True, "tracks": cached}

    yt = _get_yt(auth=True)
    try:
        raw = yt.get_liked_songs(limit=200)
    except Exception as exc:
        raise HTTPException(502, f"ytmusicapi auth error: {exc}")

    tracks = [_fmt_track(t) for t in (raw.get("tracks") or []) if t.get("videoId")]
    _cache_set("lib", tracks)
    return {"ok": True, "cached": False, "tracks": tracks}


# ---------------------------------------------------------------------------
# Vercel ASGI entrypoint: the `app` object above is picked up automatically
# when api/index.py is used as the handler. For local dev:
#     uvicorn api.index:app --reload --port 8000
# ---------------------------------------------------------------------------
