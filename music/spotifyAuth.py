# Spotify Authorization Code flow. Playlist writes require a user authorized token,
# so Client Credentials is not enough. The refresh token lives in SQLite so the
# browser login only ever has to happen once.

import os

import spotipy
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth

from . import store

# Read scopes cover private and collaborative playlists. Write scopes cover both
# visibilities so the playlist can be flipped later without a second login.
spotifyScopes = " ".join(
    [
        "playlist-read-private",
        "playlist-read-collaborative",
        "playlist-modify-private",
        "playlist-modify-public",
    ]
)

defaultRedirectUri = "http://127.0.0.1:8888/callback"
defaultPlaylistName = "Discord Queue"
defaultPlaylistDescription = "Shared queue for the Discord music bot. Add songs here and the bot will play them."


# spotipy normally sleeps through a 429 before retrying, and Development Mode quota errors
# carry a Retry-After of many hours, which would hang the calling thread for that long.
# urllib3 honours Retry-After whenever any retry is allowed, so retries are off entirely
# and a 429 raises at once for the caller to back off.
clientOptions = {
    "requests_timeout": 10,
    "retries": 0,
    "status_retries": 0,
    "status_forcelist": (500, 502, 503, 504),
}


def retryAfterSeconds(err, default=60):
    try:
        return max(default, int((err.headers or {}).get("Retry-After", default)))
    except (TypeError, ValueError):
        return default


class SpotifyConfigError(RuntimeError):
    # Raised when the .env values needed for Spotify are missing or malformed.
    pass


class SpotifyAuthNotReady(RuntimeError):
    # Raised when no refresh token is stored yet, meaning the one time login has not been done.
    pass


class SqliteCacheHandler(CacheHandler):
    # Plugs the SQLite store into spotipy so it persists and refreshes tokens for us.

    def get_cached_token(self):
        return store.loadToken()

    def save_token_to_cache(self, token_info):
        store.saveToken(token_info)


def readSpotifyEnv():
    clientId = os.getenv("SPOTIFY_CLIENT_ID")
    clientSecret = os.getenv("SPOTIFY_CLIENT_SECRET")
    redirectUri = os.getenv("SPOTIFY_REDIRECT_URI", defaultRedirectUri)

    missing = []
    if not clientId:
        missing.append("SPOTIFY_CLIENT_ID")
    if not clientSecret:
        missing.append("SPOTIFY_CLIENT_SECRET")
    if missing:
        raise SpotifyConfigError(
            "Missing required .env values: " + ", ".join(missing) + ". See .env.example."
        )

    return clientId, clientSecret, redirectUri


def buildOauth(openBrowser=False):
    clientId, clientSecret, redirectUri = readSpotifyEnv()
    return SpotifyOAuth(
        client_id=clientId,
        client_secret=clientSecret,
        redirect_uri=redirectUri,
        scope=spotifyScopes,
        cache_handler=SqliteCacheHandler(),
        open_browser=openBrowser,
        show_dialog=False,
    )


def hasStoredToken():
    tokenInfo = store.loadToken()
    return bool(tokenInfo and tokenInfo.get("refresh_token"))


def tokenCoversScopes():
    # A token minted before a scope was added will not carry it. Detect that so the
    # user is told to log in again instead of hitting a confusing 403 later.
    tokenInfo = store.loadToken() or {}
    granted = set((tokenInfo.get("scope") or "").split())
    needed = set(spotifyScopes.split())
    return needed.issubset(granted)


def getSpotifyClient():
    # Returns an authorized client, refreshing the access token automatically.
    # Never opens a browser, so it is safe to call from inside the running bot.
    store.initDb()
    if not hasStoredToken():
        raise SpotifyAuthNotReady(
            "No stored Spotify token. Run the one time login first: python spotifyLogin.py"
        )
    if not tokenCoversScopes():
        raise SpotifyAuthNotReady(
            "The stored Spotify token is missing required scopes. "
            "Run python spotifyLogin.py again to re authorize."
        )
    return spotipy.Spotify(auth_manager=buildOauth(openBrowser=False), **clientOptions)


def playlistIdFromEnvOrConfig():
    # .env wins so an explicit override is always respected. Otherwise fall back to
    # whatever playlist the bot created for itself on a previous run.
    fromEnv = (os.getenv("SPOTIFY_PLAYLIST_ID") or "").strip()
    if fromEnv:
        return normalizePlaylistId(fromEnv)
    fromConfig = (store.getConfig("playlistId") or "").strip()
    return normalizePlaylistId(fromConfig) if fromConfig else ""


def normalizePlaylistId(value):
    # Accepts a bare id, a spotify:playlist:ID uri, or an open.spotify.com link.
    value = value.strip()
    if value.startswith("spotify:playlist:"):
        return value.split(":")[-1]
    if "open.spotify.com" in value:
        tail = value.split("playlist/")[-1]
        return tail.split("?")[0].split("/")[0]
    return value


def fetchPlaylist(sp, playlistId):
    try:
        return sp.playlist(
            playlistId,
            fields="id,name,collaborative,public,snapshot_id,owner.id,external_urls.spotify",
        )
    except spotipy.SpotifyException:
        return None


def ensurePlaylist(sp, playlistName=None):
    # Makes sure there is a usable playlist and that it is collaborative, creating one
    # on the bot's own account if needed. Returns a small summary dict.
    store.initDb()
    me = sp.me()
    ownerId = me["id"]

    playlistId = playlistIdFromEnvOrConfig()
    playlist = fetchPlaylist(sp, playlistId) if playlistId else None

    created = False
    if playlist is None:
        # Spotify requires a collaborative playlist to be private, so public is forced off.
        # current_user_playlist_create posts to me/playlists. The older user_playlist_create
        # posts to users/{id}/playlists, which Spotify removed for Development Mode apps in
        # its February 2026 Web API migration and now returns a bare 403 with no detail.
        try:
            playlist = sp.current_user_playlist_create(
                name=playlistName or defaultPlaylistName,
                public=False,
                collaborative=True,
                description=defaultPlaylistDescription,
            )
        except spotipy.SpotifyException as err:
            if err.http_status == 403:
                raise SpotifyAuthNotReady(
                    "Spotify refused to create the playlist (403 forbidden) for account "
                    + str(ownerId)
                    + ". Check that this Spotify app has been added under Settings, User "
                    "Management on the app's page at developer.spotify.com/dashboard if this "
                    "account is not the one that created the app, then run this script again, "
                    "no need to log in again."
                ) from err
            raise
        created = True
        playlist = fetchPlaylist(sp, playlist["id"]) or playlist

    weOwnIt = playlist.get("owner", {}).get("id") == ownerId

    # Only the owner can change these flags, so leave someone else's playlist alone.
    if weOwnIt and not playlist.get("collaborative"):
        sp.playlist_change_details(playlist["id"], public=False, collaborative=True)
        playlist = fetchPlaylist(sp, playlist["id"]) or playlist

    store.setConfig("playlistId", playlist["id"])

    return {
        "id": playlist["id"],
        "name": playlist.get("name"),
        "url": (playlist.get("external_urls") or {}).get("spotify", ""),
        "collaborative": bool(playlist.get("collaborative")),
        "public": bool(playlist.get("public")),
        "snapshotId": playlist.get("snapshot_id"),
        "owned": weOwnIt,
        "created": created,
        "ownerDisplayName": me.get("display_name") or ownerId,
    }
