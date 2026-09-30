
import os
import json
import time
import requests
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from ytmusicapi import YTMusic

# ------------------------------------------------------------------
# Inisialisasi Aplikasi
# ------------------------------------------------------------------
app = FastAPI(title="NRN Music API", version="2.1.0")

# CORS: izinkan semua origin (karena frontend static di Vercel juga)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache instance YTMusic anonymous (visitor_data) — dibuat sekali per cold start
_ytm_anon: Optional[YTMusic] = None

def get_anon_ytm() -> YTMusic:
    """Dapatkan instance YTMusic anonymous dengan visitor_data.
    Ini SOLUSI UTAMA mengurangi HTTP 403 Google dibanding YTMusic() kosong.
    Crosscheck: issue ytmusicapi #487, #512 — visitor_data mengurangi bot flag."""
    global _ytm_anon
    if _ytm_anon is None:
        try:
            _ytm_anon = YTMusic(auth="visitor_data")
        except Exception as e:
            # Fallback kalau visitor_data gagal di region tertentu
            _ytm_anon = YTMusic()
    return _ytm_anon

def get_authed_ytm(token_json_str: str) -> YTMusic:
    """Buat instance YTMusic dengan token user dari localStorage.
    Token berisi: access_token, refresh_token, client_id, client_secret, dll."""
    try:
        creds = json.loads(token_json_str)
        return YTMusic(auth=creds)
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="Format token tidak valid")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Gagal inisialisasi auth: {str(e)[:80]}")

# ------------------------------------------------------------------
# Helper: Pilih URL streaming terbaik
# Prioritas: itag 251 (opus 160kbps) > 140 (aac 128kbps) > 18 (mp4 audio)
# Lebih suka yang punya field `url` langsung, bukan signatureCipher
# ------------------------------------------------------------------
def pick_best_stream(adaptive_formats: list) -> Optional[str]:
    ORDER = [251, 140, 18, 250, 249, 139]
    url_map = {int(f.get("itag", 0)): f.get("url") for f in adaptive_formats if f.get("url")}
    for itag in ORDER:
        if itag in url_map and url_map[itag]:
            return url_map[itag]
    # Fallback: ambil format audio apapun yang ada URL-nya
    for f in adaptive_formats:
        if f.get("mimeType", "").startswith("audio") and f.get("url"):
            return f["url"]
    return None

# ------------------------------------------------------------------
# ENDPOINT: Health Check
# ------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "version": "2.1.0"}

# ------------------------------------------------------------------
# ENDPOINT: OAuth Device Flow (sesuai RFC 8628)
# TIDAK ADA HARDCODE Client ID/Secret — semuanya dari user
# ------------------------------------------------------------------
GOOGLE_OAUTH_DEVICE = "https://oauth2.googleapis.com/device/code"
GOOGLE_OAUTH_TOKEN  = "https://oauth2.googleapis.com/token"
YTMUSIC_SCOPE = "https://www.googleapis.com/auth/youtube"

@app.post("/api/auth/device")
async def auth_device(req: Request):
    """Langkah 1: Minta kode device ke Google.
    Body: { client_id: string, client_secret: string }"""
    try:
        body = await req.json()
    except:
        raise HTTPException(status_code=400, detail="Body harus JSON")
    cid = body.get("client_id")
    csc = body.get("client_secret")
    if not cid or not csc:
        raise HTTPException(status_code=400, detail="client_id dan client_secret wajib")
    try:
        r = requests.post(GOOGLE_OAUTH_DEVICE, data={
            "client_id": cid, "scope": YTMUSIC_SCOPE
        }, timeout=15)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Google: {r.text[:150]}")
        data = r.json()
        # Simpan client_secret sementara di response — frontend kirim lagi saat polling
        data["_client_secret"] = csc
        return JSONResponse(data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal hubungi Google: {str(e)[:80]}")

@app.post("/api/auth/token")
async def auth_token(req: Request):
    """Langkah 2: Polling sampai user konfirmasi login.
    Body: { client_id, client_secret, device_code, grant_type: "urn:ietf:params:oauth:grant-type:device_code" }"""
    try:
        body = await req.json()
    except:
        raise HTTPException(status_code=400, detail="Body harus JSON")
    required = ["client_id","client_secret","device_code"]
    for k in required:
        if not body.get(k):
            raise HTTPException(status_code=400, detail=f"{k} wajib")
    try:
        r = requests.post(GOOGLE_OAUTH_TOKEN, data={
            "client_id": body["client_id"],
            "client_secret": body["client_secret"],
            "device_code": body["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=15)
        if r.status_code == 400 and "authorization_pending" in r.text:
            # Normal: user belum klik Allow
            return JSONResponse({"status": "pending"}, status_code=202)
        if r.status_code == 400 and "slow_down" in r.text:
            return JSONResponse({"status": "slow_down"}, status_code=202)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"Google: {r.text[:150]}")
        token = r.json()
        # Tambahkan client_id + secret ke token agar bisa refresh nanti
        token["client_id"] = body["client_id"]
        token["client_secret"] = body["client_secret"]
        token["expires_at"] = int(time.time()) + int(token.get("expires_in", 3599))
        return JSONResponse(token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:80])

# ------------------------------------------------------------------
# ENDPOINT: Data Publik (bisa anonymous / authed)
# ------------------------------------------------------------------
def _token_header(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None

@app.get("/api/home")
def home(authorization: Optional[str] = Header(None)):
    """Feed halaman utama — trending + mood + playlist rekomendasi."""
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        return {"ok": True, "data": ytm.get_home()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal muat home: {str(e)[:120]}")

@app.get("/api/search")
def search(q: str, filter: str = "songs", authorization: Optional[str] = Header(None)):
    """Pencarian. Filter: songs / videos / albums / artists / playlists."""
    if not q or len(q.strip()) < 1:
        raise HTTPException(status_code=400, detail="Query pencarian kosong")
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        res = ytm.search(q, filter=filter if filter != "all" else None, limit=30)
        return {"ok": True, "query": q, "filter": filter, "data": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal cari: {str(e)[:120]}")

@app.get("/api/song/{video_id}")
def get_song(video_id: str, authorization: Optional[str] = Header(None)):
    """Ambil metadata + URL streaming TERBAIK untuk videoId.
    SELALU panggil ini tepat sebelum play — URL expired ~6 jam."""
    if not video_id or len(video_id) < 5:
        raise HTTPException(status_code=400, detail="videoId tidak valid")
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        details = ytm.get_song(video_id)
        sf = details.get("streamingData", {}).get("adaptiveFormats", [])
        url = pick_best_stream(sf)
        if not url:
            raise HTTPException(status_code=410, detail="Tidak ada stream yang tersedia untuk lagu ini")
        return {
            "ok": True,
            "video_id": video_id,
            "title": details.get("videoDetails", {}).get("title", video_id),
            "artist": details.get("videoDetails", {}).get("author", "Unknown"),
            "thumbnail": (details.get("videoDetails", {}).get("thumbnail", {}).get("thumbnails", [{}])[-1:][{}].get("url") if details.get("videoDetails", {}).get("thumbnail") else None),
            "duration_sec": int(details.get("videoDetails", {}).get("lengthSeconds", 0)),
            "stream_url": url,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal muat lagu: {str(e)[:120]}")

@app.get("/api/playlist/{playlist_id}")
def get_playlist(playlist_id: str, limit: int = 200, authorization: Optional[str] = Header(None)):
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        return {"ok": True, "data": ytm.get_playlist(playlist_id, limit=limit)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal muat playlist: {str(e)[:120]}")

@app.get("/api/album/{browse_id}")
def get_album(browse_id: str, authorization: Optional[str] = Header(None)):
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        return {"ok": True, "data": ytm.get_album(browse_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal muat album: {str(e)[:120]}")

@app.get("/api/artist/{browse_id}")
def get_artist(browse_id: str, authorization: Optional[str] = Header(None)):
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        return {"ok": True, "data": ytm.get_artist(browse_id)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal muat artis: {str(e)[:120]}")

@app.get("/api/suggestions")
def suggestions(video_id: str, authorization: Optional[str] = Header(None)):
    """Lagu terkait untuk autoplay selanjutnya."""
    try:
        tk = _token_header(authorization)
        ytm = get_authed_ytm(tk) if tk else get_anon_ytm()
        return {"ok": True, "data": ytm.get_watch_playlist(video_id, limit=30)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:120])

# ------------------------------------------------------------------
# ENDPOINT: Private (WAJIB Login)
# ------------------------------------------------------------------
@app.get("/api/library")
def get_library(authorization: Optional[str] = Header(None)):
    """Library user: Liked Songs, Playlist, Album, Artis tersimpan.
    TANPA token = 401 jelas, bukan 'library failed' doang."""
    tk = _token_header(authorization)
    if not tk:
        raise HTTPException(status_code=401, detail="LOGIN_REQUIRED")
    try:
        ytm = get_authed_ytm(tk)
        return {
            "ok": True,
            "liked_songs": ytm.get_liked_songs(limit=500),
            "playlists":   ytm.get_library_playlists(limit=200),
            "albums":      ytm.get_library_albums(limit=200),
            "artists":     ytm.get_library_artists(limit=200),
            "subscriptions": ytm.get_library_subscriptions(limit=200),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal muat library: {str(e)[:150]}")
