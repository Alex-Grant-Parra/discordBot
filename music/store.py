# SQLite persistence for the music feature. Holds the Spotify OAuth token, feature config,
# and the Spotify track to YouTube video cache.

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

dbPath = Path(os.getenv("MUSIC_DB_PATH", "music.db"))

schemaSql = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spotifyToken (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tokenJson TEXT NOT NULL,
    updatedAt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trackCache (
    spotifyTrackId TEXT PRIMARY KEY,
    youtubeVideoId TEXT NOT NULL,
    youtubeTitle TEXT,
    score REAL,
    cachedAt TEXT NOT NULL
);
"""

# Config keys the feature understands, with their defaults.
configDefaults = {
    # "cursor" leaves played tracks in the playlist and advances an internal pointer.
    # "consume" deletes each track from the Spotify playlist once it has finished playing.
    "queueMode": "cursor",
    # Spotify playlist the bot treats as the queue. Filled in on first run if not set in .env.
    "playlistId": "",
}


@contextmanager
def connect():
    # One short lived connection per operation. Keeps things safe across the asyncio
    # event loop and the executor threads spotipy and yt_dlp run in.
    conn = sqlite3.connect(dbPath, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initDb():
    with connect() as conn:
        conn.executescript(schemaSql)


def nowStamp():
    return datetime.now(timezone.utc).isoformat()


# Config helpers

def getConfig(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    if default is not None:
        return default
    return configDefaults.get(key)


def setConfig(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def getAllConfig():
    merged = dict(configDefaults)
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM config"):
            merged[row["key"]] = row["value"]
    return merged


# Spotify token helpers. spotipy hands us a plain dict, we keep it as JSON.

def loadToken():
    with connect() as conn:
        row = conn.execute("SELECT tokenJson FROM spotifyToken WHERE id = 1").fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["tokenJson"])
    except (ValueError, TypeError):
        return None


def saveToken(tokenInfo):
    with connect() as conn:
        conn.execute(
            "INSERT INTO spotifyToken (id, tokenJson, updatedAt) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET tokenJson = excluded.tokenJson, "
            "updatedAt = excluded.updatedAt",
            (json.dumps(tokenInfo), nowStamp()),
        )


def clearToken():
    with connect() as conn:
        conn.execute("DELETE FROM spotifyToken WHERE id = 1")


# Track cache helpers. Used from stage three onward.

def getCachedMatch(spotifyTrackId):
    with connect() as conn:
        row = conn.execute(
            "SELECT youtubeVideoId, youtubeTitle, score FROM trackCache WHERE spotifyTrackId = ?",
            (spotifyTrackId,),
        ).fetchone()
    if row is None:
        return None
    return {
        "youtubeVideoId": row["youtubeVideoId"],
        "youtubeTitle": row["youtubeTitle"],
        "score": row["score"],
    }


def saveCachedMatch(spotifyTrackId, youtubeVideoId, youtubeTitle=None, score=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO trackCache (spotifyTrackId, youtubeVideoId, youtubeTitle, score, cachedAt) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(spotifyTrackId) DO UPDATE SET youtubeVideoId = excluded.youtubeVideoId, "
            "youtubeTitle = excluded.youtubeTitle, score = excluded.score, cachedAt = excluded.cachedAt",
            (spotifyTrackId, youtubeVideoId, youtubeTitle, score, nowStamp()),
        )


def forgetCachedMatch(spotifyTrackId):
    with connect() as conn:
        conn.execute("DELETE FROM trackCache WHERE spotifyTrackId = ?", (spotifyTrackId,))
