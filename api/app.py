from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from ytmusicapi import YTMusic
import os
import json
import base64
import tempfile
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# ---------------------------------------------------------------------------
# Initialize ytmusicapi
# Priority:
#   1. YTMUSIC_OAUTH_BASE64 env var (base64-encoded oauth.json content)
#   2. oauth.json file in project root
#   3. YTMUSIC_AUTH_JSON env var (raw JSON string)
# ---------------------------------------------------------------------------

def _init_ytmusic():
    oauth_b64 = os.environ.get('YTMUSIC_OAUTH_BASE64')
    if oauth_b64:
        try:
            data = base64.b64decode(oauth_b64).decode('utf-8')
            fd, path = tempfile.mkstemp(suffix='.json')
            with os.fdopen(fd, 'w') as f:
                f.write(data)
            return YTMusic(path)
        except Exception as e:
            print(f'[warn] Failed to init from YTMUSIC_OAUTH_BASE64: {e}')

    auth_json = os.environ.get('YTMUSIC_AUTH_JSON')
    if auth_json:
        try:
            fd, path = tempfile.mkstemp(suffix='.json')
            with os.fdopen(fd, 'w') as f:
                f.write(auth_json)
            return YTMusic(path)
        except Exception as e:
            print(f'[warn] Failed to init from YTMUSIC_AUTH_JSON: {e}')

    if os.path.exists('oauth.json'):
        try:
            return YTMusic('oauth.json')
        except Exception as e:
            print(f'[warn] Failed to init from oauth.json: {e}')

    print('[warn] No ytmusicapi credentials found. Running in guest mode.')
    return YTMusic()  # guest mode (limited endpoints)


yt = _init_ytmusic()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _err(message, status=500):
    return jsonify({'error': str(message)}), status


def _ok(payload, status=200):
    return jsonify(payload), status


def _song_summary(item):
    """Normalize a ytmusicapi search result into a compact song object."""
    video_id = (item.get('videoId')
                or item.get('video_id')
                or (item.get('id') if isinstance(item.get('id'), str) else None))
    title = item.get('title', 'Unknown title')
    artists = item.get('artists') or item.get('artist') or []
    if isinstance(artists, list):
        artist_names = [a.get('name', '') if isinstance(a, dict) else str(a) for a in artists]
        artist_str = ', '.join([n for n in artist_names if n])
    else:
        artist_str = str(artists)
    album = item.get('album')
    if isinstance(album, dict):
        album = album.get('name')
    thumbnails = item.get('thumbnails') or []
    thumbnail = thumbnails[-1]['url'] if thumbnails else None
    duration = item.get('duration') or item.get('length')
    if isinstance(duration, dict):
        duration = duration.get('text')
    return {
        'videoId': video_id,
        'title': title,
        'artist': artist_str,
        'album': album,
        'thumbnail': thumbnail,
        'duration': duration,
        'type': item.get('resultType') or item.get('type'),
    }

# ---------------------------------------------------------------------------
# Routes - Frontend
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def index():
    return send_from_directory('.', 'index.html')

# ---------------------------------------------------------------------------
# Routes - API
# ---------------------------------------------------------------------------

@app.route('/api/health', methods=['GET'])
def health():
    return _ok({'status': 'ok', 'service': 'nrn-music'})


@app.route('/api/search', methods=['GET'])
def search():
    q = request.args.get('q', '').strip()
    type_filter = request.args.get('type', 'songs')
    limit = request.args.get('limit', 20, type=int)

    if not q:
        return _err('Query required', 400)

    try:
        results = yt.search(q, filter=type_filter)
        normalized = [_song_summary(r) for r in results[:limit]]
        return _ok({'results': normalized, 'query': q, 'type': type_filter})
    except Exception as e:
        return _err(e)


@app.route('/api/song/<video_id>', methods=['GET'])
def get_song(video_id):
    try:
        song = yt.get_song(video_id)
        # Flatten the most useful fields for the frontend
        video_details = song.get('videoDetails', {})
        microformat = song.get('microformat', {}).get('microformatDataRenderer', {})
        payload = {
            'videoId': video_details.get('videoId', video_id),
            'title': video_details.get('title'),
            'artist': video_details.get('author'),
            'channelId': video_details.get('channelId'),
            'lengthSeconds': video_details.get('lengthSeconds'),
            'thumbnail': (video_details.get('thumbnail', {}).get('thumbnails') or [{}])[-1].get('url'),
            'description': microformat.get('description'),
        }
        return _ok(payload)
    except Exception as e:
        return _err(e)


@app.route('/api/playlist/<playlist_id>', methods=['GET'])
def get_playlist(playlist_id):
    limit = request.args.get('limit', 50, type=int)
    try:
        playlist = yt.get_playlist(playlist_id, limit=limit)
        tracks = playlist.get('tracks', [])
        normalized = [_song_summary(t) for t in tracks]
        return _ok({
            'id': playlist.get('id'),
            'title': playlist.get('title'),
            'author': playlist.get('author'),
            'thumbnail': (playlist.get('thumbnails') or [{}])[-1].get('url'),
            'trackCount': len(normalized),
            'tracks': normalized,
        })
    except Exception as e:
        return _err(e)


@app.route('/api/library/liked', methods=['GET'])
def get_liked_songs():
    limit = request.args.get('limit', 50, type=int)
    try:
        liked = yt.get_liked_songs(limit=limit)
        contents = liked.get('contents', [])
        normalized = [_song_summary(c) for c in contents]
        return _ok({'songs': normalized})
    except Exception as e:
        return _err(e)


@app.route('/api/library/recent', methods=['GET'])
def get_recent():
    try:
        history = yt.get_history()
        normalized = [_song_summary(h) for h in history[:50]]
        return _ok({'songs': normalized})
    except Exception as e:
        return _err(e)


@app.route('/api/song/rate', methods=['POST'])
def rate_song():
    data = request.get_json(silent=True) or {}
    video_id = data.get('videoId')
    rating = data.get('rating')  # 'LIKE' or 'INDIFFERENT'

    if not video_id or not rating:
        return _err('Missing params: videoId and rating are required', 400)

    if rating not in ('LIKE', 'INDIFFERENT', 'DISLIKE'):
        return _err("rating must be one of 'LIKE', 'INDIFFERENT', 'DISLIKE'", 400)

    try:
        yt.rate_song(video_id, rating)
        return _ok({'status': 'ok', 'videoId': video_id, 'rating': rating})
    except Exception as e:
        return _err(e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
