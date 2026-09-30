from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from ytmusicapi import YTMusic, OAuthCredentials
import httpx
import json
import time
from typing import Optional, Dict, Any
from urllib.parse import unquote

# ----------------------------------------------------------------
# KONSTANTA
# ----------------------------------------------------------------
GOOGLE_DEVICE_CODE = "https://oauth2.googleapis.com/device/code"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache module-level — hidup selama instance Vercel hangat
_yt_anon: Optional[YTMusic] = None
_sig_ts: Optional[int] = None


def get_anon_yt() -> YTMusic:
    global _yt_anon, _sig_ts
    if _yt_anon is None:
        try:
            _yt_anon = YTMusic()
        except Exception:
            _yt_anon = YTMusic()
        try:
            _sig_ts = int(time.time())
        except Exception:
            _sig_ts = int(time.time())
    return _yt_anon


def get_auth_yt(token_dict: Dict[str, Any], client_id: str, client_secret: str) -> YTMusic:
    creds = OAuthCredentials(client_id=client_id, client_secret=client_secret)
    return YTMusic(token_dict, oauth_credentials=creds)


# ----------------------------------------------------------------
# AUTH (hanya untuk Library)
# ----------------------------------------------------------------
@app.get("/api/auth/device")
async def auth_device(client_id: str):
    if not client_id:
        raise HTTPException(400, "client_id wajib")
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(GOOGLE_DEVICE_CODE, data={
                "client_id": client_id, "scope": OAUTH_SCOPE
            })
            d = r.json()
            if r.status_code != 200:
                raise HTTPException(400, d.get("error_description") or d.get("error") or "gagal minta kode")
            return JSONResponse(d)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gagal minta kode device: {e}")


@app.post("/api/auth/token")
async def auth_token(req: Request):
    try:
        b = await req.json()
    except Exception:
        raise HTTPException(400, "body tidak valid")
    for k in ("client_id", "client_secret", "device_code"):
        if not b.get(k):
            raise HTTPException(400, f"{k} wajib")
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(GOOGLE_TOKEN, data={
                "client_id": b["client_id"],
                "client_secret": b["client_secret"],
                "device_code": b["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            d = r.json()
            if r.status_code == 200:
                d["expires_at"] = int(time.time()) + int(d.get("expires_in", 3600))
                return JSONResponse(d)
            err = d.get("error")
            if err in ("authorization_pending", "slow_down"):
                return JSONResponse({"pending": True, "error": err}, status_code=202)
            return JSONResponse(
                {"error": err, "error_description": d.get("error_description", "")},
                status_code=400,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gagal polling: {e}")


@app.post("/api/auth/refresh")
async def auth_refresh(req: Request):
    try:
        b = await req.json()
    except Exception:
        raise HTTPException(400, "bad body")
    for k in ("client_id", "client_secret", "refresh_token"):
        if not b.get(k):
            raise HTTPException(400, f"{k} wajib")
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post(GOOGLE_TOKEN, data={
                "client_id": b["client_id"],
                "client_secret": b["client_secret"],
                "refresh_token": b["refresh_token"],
                "grant_type": "refresh_token",
            })
            if r.status_code != 200:
                return JSONResponse({"error": "refresh_gagal"}, status_code=401)
            d = r.json()
            d["expires_at"] = int(time.time()) + int(d.get("expires_in", 3600))
            if "refresh_token" not in d:
                d["refresh_token"] = b["refresh_token"]
            return JSONResponse(d)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"refresh gagal: {e}")


# ----------------------------------------------------------------
# UTILS
# ----------------------------------------------------------------
def _parse_token(x: Optional[str]) -> Optional[Dict]:
    if not x:
        return None
    try:
        return json.loads(x)
    except Exception:
        return None


def _need_login(x: Optional[str]):
    t = _parse_token(x)
    if not t:
        raise HTTPException(401, "LOGIN_REQUIRED")
    return t


def _pick_best_stream(song: Dict[str, Any]) -> Optional[str]:
    """Pilih stream audio BITRATE TERTINGGI yang punya URL.
    TIDAK hardcode itag — tahan perubahan Google."""
    if not song:
        return None
    ps = song.get("playabilityStatus") or {}
    if ps.get("status") != "OK":
        return None
    sd = song.get("streamingData") or {}
    af = sd.get("adaptiveFormats") or []
    fm = sd.get("formats") or []
    candidates = []
    for f in af + fm:
        mt = (f.get("mimeType") or "").lower()
        if "audio" not in mt:
            continue
        url = f.get("url")
        if not url:
            sc = f.get("signatureCipher") or ""
            if not sc:
                continue
            for part in sc.split("&"):
                if part.startswith("url="):
                    url = unquote(part.split("=", 1)[1])
                    break
        if not url:
            continue
        br = int(f.get("averageBitrate") or f.get("bitrate") or 0)
        candidates.append((br, url))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _thumb(obj, idx=-1) -> str:
    arr = (obj or {}).get("thumbnails") or []
    if not arr:
        return ""
    try:
        return arr[idx]["url"]
    except Exception:
        return arr[0]["url"]


def _artists_str(a) -> str:
    if not a:
        return ""
    if isinstance(a, str):
        return a
    if isinstance(a, list):
        return ", ".join([(x.get("name") or "") for x in a if isinstance(x, dict)])
    return str(a)


# ----------------------------------------------------------------
# ANONYMOUS ENDPOINTS — TANPA LOGIN = LANGSUNG JALAN
# ----------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@app.get("/api/home")
def home():
    try:
        yt = get_anon_yt()
        shelves = yt.get_home() or []
        out = []
        for s in shelves[:10]:
            title = s.get("title") or "Pilihan"
            items = s.get("contents") or []
            if not items:
                continue
            clean = []
            for it in items:
                try:
                    clean.append({
                        "title": it.get("title") or "",
                        "subtitle": _artists_str(it.get("subtitle") or it.get("artists")),
                        "thumb": _thumb(it),
                        "playlistId": it.get("playlistId"),
                        "browseId": it.get("browseId"),
                        "videoId": it.get("videoId"),
                        "type": it.get("resultType") or it.get("type") or "playlist",
                    })
                except Exception:
                    pass
            if clean:
                out.append({"title": title, "items": clean[:10]})
        return JSONResponse({"shelves": out})
    except Exception as e:
        raise HTTPException(500, f"Gagal muat home: {e}")


@app.get("/api/search")
def search(q: str):
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"items": []})
    try:
        yt = get_anon_yt()
        res = yt.search(q, limit=25) or []
        out = []
        for it in res:
            try:
                out.append({
                    "resultType": it.get("resultType") or "song",
                    "title": it.get("title") or "",
                    "artist": _artists_str(it.get("artists")),
                    "thumb": _thumb(it),
                    "videoId": it.get("videoId"),
                    "browseId": it.get("browseId"),
                    "playlistId": it.get("playlistId"),
                    "album": (it.get("album") or {}).get("name") if isinstance(it.get("album"), dict) else "",
                    "duration": it.get("duration") or "",
                })
            except Exception:
                pass
        return JSONResponse({"items": out})
    except Exception as e:
        raise HTTPException(500, f"Gagal cari: {e}")


@app.get("/api/song/{video_id}")
def get_song(video_id: str):
    """Dapatkan fresh stream URL.
    URL dikembalikan LANGSUNG ke browser (HP user = residential IP),
    BUKAN lewat proxy Vercel (data center IP = selalu 403)."""
    try:
        yt = get_anon_yt()
        song = yt.get_song(video_id, signatureTimestamp=_sig_ts)
        url = _pick_best_stream(song)
        if not url:
            song2 = yt.get_song(video_id)
            url = _pick_best_stream(song2)
        vd = (song or {}).get("videoDetails") or {}
        return JSONResponse({
            "videoId": video_id,
            "title": vd.get("title") or "",
            "author": vd.get("author") or "",
            "thumb": _thumb(vd.get("thumbnail")),
            "duration": int(vd.get("lengthSeconds") or 0),
            "streamUrl": url,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gagal muat lagu: {e}")


@app.get("/api/playlist/{pid}")
def get_playlist(pid: str, limit: int = 150):
    try:
        yt = get_anon_yt()
        pl = yt.get_playlist(pid, limit=limit) or {}
        tracks = []
        for t in pl.get("tracks") or []:
            try:
                if not t.get("videoId"):
                    continue
                if t.get("isAvailable") is False:
                    continue
                tracks.append({
                    "videoId": t["videoId"],
                    "title": t.get("title") or "",
                    "artist": _artists_str(t.get("artists")),
                    "thumb": _thumb(t),
                    "duration": t.get("duration") or "",
                })
            except Exception:
                pass
        return JSONResponse({
            "title": pl.get("title") or "",
            "description": pl.get("description") or "",
            "thumb": _thumb(pl),
            "trackCount": len(tracks),
            "tracks": tracks,
        })
    except Exception as e:
        raise HTTPException(500, f"Gagal muat playlist: {e}")


@app.get("/api/artist/{bid}")
def get_artist(bid: str):
    try:
        yt = get_anon_yt()
        a = yt.get_artist(bid) or {}
        tops = []
        for t in (a.get("songs") or {}).get("results") or []:
            try:
                if not t.get("videoId"):
                    continue
                tops.append({
                    "videoId": t["videoId"],
                    "title": t.get("title") or "",
                    "artist": _artists_str(t.get("artists")),
                    "thumb": _thumb(t),
                    "duration": t.get("duration") or "",
                })
            except Exception:
                pass
        return JSONResponse({
            "name": a.get("name") or "",
            "thumb": _thumb(a),
            "description": a.get("description") or "",
            "topSongs": tops[:20],
        })
    except Exception as e:
        raise HTTPException(500, f"Gagal muat artis: {e}")


@app.get("/api/album/{bid}")
def get_album(bid: str):
    try:
        yt = get_anon_yt()
        al = yt.get_album(bid) or {}
        tracks = []
        for t in al.get("tracks") or []:
            try:
                if not t.get("videoId"):
                    continue
                tracks.append({
                    "videoId": t["videoId"],
                    "title": t.get("title") or "",
                    "artist": _artists_str(t.get("artists")),
                    "thumb": _thumb(t),
                    "duration": t.get("duration") or "",
                })
            except Exception:
                pass
        return JSONResponse({
            "title": al.get("title") or "",
            "artists": _artists_str(al.get("artists")),
            "thumb": _thumb(al),
            "year": str(al.get("year") or ""),
            "trackCount": len(tracks),
            "tracks": tracks,
        })
    except Exception as e:
        raise HTTPException(500, f"Gagal muat album: {e}")


@app.get("/api/suggestions")
def suggestions(videoId: Optional[str] = None):
    if not videoId:
        return JSONResponse({"tracks": []})
    try:
        yt = get_anon_yt()
        w = yt.get_watch_playlist(videoId, limit=30) or {}
        out = []
        for t in w.get("tracks") or []:
            try:
                if not t.get("videoId") or t["videoId"] == videoId:
                    continue
                out.append({
                    "videoId": t["videoId"],
                    "title": t.get("title") or "",
                    "artist": _artists_str(t.get("artists")),
                    "thumb": _thumb(t),
                    "duration": t.get("duration") or "",
                })
            except Exception:
                pass
        return JSONResponse({"tracks": out[:25]})
    except Exception as e:
        raise HTTPException(500, f"Gagal rekomendasi: {e}")


# ----------------------------------------------------------------
# LIBRARY ENDPOINTS (butuh login + Client ID/Secret user di header)
# ----------------------------------------------------------------
@app.get("/api/library")
def library(
    x_ytm_token: Optional[str] = Header(None),
    x_ytm_cid: Optional[str] = Header(None),
    x_ytm_csec: Optional[str] = Header(None),
):
    t = _need_login(x_ytm_token)
    if not x_ytm_cid or not x_ytm_csec:
        raise HTTPException(400, "client_id/client_secret hilang di header")
    now = int(time.time())
    if t.get("expires_at", 0) - now < 90 and t.get("refresh_token"):
        try:
            r = httpx.post(GOOGLE_TOKEN, data={
                "client_id": x_ytm_cid,
                "client_secret": x_ytm_csec,
                "refresh_token": t["refresh_token"],
                "grant_type": "refresh_token",
            }, timeout=10)
            if r.status_code == 200:
                nd = r.json()
                t.update(nd)
                t["expires_at"] = now + int(nd.get("expires_in", 3600))
                if "refresh_token" not in nd and t.get("refresh_token"):
                    pass  # tetap pakai yang lama
        except Exception:
            pass
    try:
        yt = get_auth_yt(t, x_ytm_cid, x_ytm_csec)
        playlists, liked, albums = [], [], []
        try:
            for p in yt.get_library_playlists(limit=100) or []:
                try:
                    playlists.append({
                        "playlistId": p.get("playlistId"),
                        "title": p.get("title") or "",
                        "count": str(p.get("count") or ""),
                        "thumb": _thumb(p),
                    })
                except Exception:
                    pass
        except Exception:
            pass
        try:
            ls = yt.get_liked_songs(limit=200) or {}
            for tr in ls.get("tracks") or []:
                try:
                    if not tr.get("videoId"):
                        continue
                    liked.append({
                        "videoId": tr["videoId"],
                        "title": tr.get("title") or "",
                        "artist": _artists_str(tr.get("artists")),
                        "thumb": _thumb(tr),
                        "duration": tr.get("duration") or "",
                    })
                except Exception:
                    pass
        except Exception:
            pass
        try:
            for a in yt.get_library_albums(limit=50) or []:
                try:
                    albums.append({
                        "browseId": a.get("browseId"),
                        "title": a.get("title") or "",
                        "artists": _artists_str(a.get("artists")),
                        "thumb": _thumb(a),
                        "year": str(a.get("year") or ""),
                    })
                except Exception:
                    pass
        except Exception:
            pass
        return JSONResponse({
            "playlists": playlists, "liked": liked, "albums": albums, "token": t,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gagal muat library: {e}")


@app.get("/api/library/playlist/{pid}")
def lib_playlist(
    pid: str,
    x_ytm_token: Optional[str] = Header(None),
    x_ytm_cid: Optional[str] = Header(None),
    x_ytm_csec: Optional[str] = Header(None),
    limit: int = 200,
):
    t = _need_login(x_ytm_token)
    if not x_ytm_cid or not x_ytm_csec:
        raise HTTPException(400, "credential hilang")
    try:
        yt = get_auth_yt(t, x_ytm_cid, x_ytm_csec)
        pl = yt.get_playlist(pid, limit=limit) or {}
        tracks = []
        for tr in pl.get("tracks") or []:
            try:
                if not tr.get("videoId"):
                    continue
                tracks.append({
                    "videoId": tr["videoId"],
                    "title": tr.get("title") or "",
                    "artist": _artists_str(tr.get("artists")),
                    "thumb": _thumb(tr),
                    "duration": tr.get("duration") or "",
                })
            except Exception:
                pass
        return JSONResponse({
            "title": pl.get("title") or "",
            "thumb": _thumb(pl),
            "tracks": tracks,
            "token": t,
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Gagal muat playlist: {e}")
