# SQLite persistence for the music feature. Holds the Spotify Web API token and feature config.

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
"""

# Config keys the feature understands, with their defaults.
configDefaults = {
    # The shared collaborative playlist /playlist plays. Filled in by spotifyLogin.py if not set in .env.
    "playlistId": "",
    # The voice channel the bot joins when playback is started from the Spotify app.
    "voiceChannelId": "",
}


@contextmanager
def connect():
    # One short lived connection per operation. Keeps things safe across the asyncio
    # event loop and the executor threads spotipy runs in.
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

