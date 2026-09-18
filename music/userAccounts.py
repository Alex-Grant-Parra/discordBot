# Personal Spotify logins for Discord users, linked with /link. Nothing is ever played on
# these accounts and they do not need Premium. They are only read, to fill /play's
# suggestions with each person's own recently played, top and liked songs. Playback
# always happens on the shared speaker account.

import secrets
import time

import requests
import spotipy
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

from . import catalog, spotifyAuth, store

userScopes = "user-library-read user-read-recently-played user-top-read"

pendingLoginSeconds = 15 * 60
libraryCacheSeconds = 10 * 60
libraryFetchLimit = 50


class LinkError(RuntimeError):
    # Carries a message meant to be shown to the person as is.
    pass


class UserCacheHandler(CacheHandler):
    def __init__(self, discordUserId):
        self.discordUserId = discordUserId

    def get_cached_token(self):
        return store.loadUserToken(self.discordUserId)

    def save_token_to_cache(self, token_info):
        store.saveUserToken(self.discordUserId, token_info)


def buildOauth(discordUserId, state=None):
    clientId, clientSecret, redirectUri = spotifyAuth.readSpotifyEnv()
    return SpotifyOAuth(
        client_id=clientId,
        client_secret=clientSecret,
        redirect_uri=redirectUri,
        scope=userScopes,
        state=state,
        cache_handler=UserCacheHandler(discordUserId),
        open_browser=False,
        # Always shows the account picker, so a friend whose browser is logged in to
        # someone else's Spotify notices before linking the wrong account.
        show_dialog=True,
    )


def tokenCoversScopes(tokenInfo):
    granted = set(((tokenInfo or {}).get("scope") or "").split())
    return set(userScopes.split()).issubset(granted)


def trackFromEntry(entry):
    # Saved and recently played entries wrap the track, top tracks are bare. The February
    # 2026 Web API migration renamed some wrappers from track to item, so accept both.
    if not entry:
        return None
    if entry.get("type") == "track":
        return entry
    return entry.get("track") or entry.get("item")


class UserAccounts:
    def __init__(self):
        self.pending = {}
        self.clients = {}
        self.libraries = {}

    # Linking

    def startLink(self, discordUserId):
        now = time.monotonic()
        self.pending = {
            state: entry for state, entry in self.pending.items() if now - entry[1] < pendingLoginSeconds
        }
        state = secrets.token_urlsafe(16)
        self.pending[state] = (discordUserId, now)
        return buildOauth(discordUserId, state).get_authorize_url()

    def finishLink(self, discordUserId, pastedUrl):
        # Blocking, run it in an executor. Returns the linked Spotify display name.
        try:
            state, code = SpotifyOAuth.parse_auth_response_url(pastedUrl.strip())
        except SpotifyOauthError:
            raise LinkError("Spotify said the login was cancelled. Run /link to try again.")
        if not code or not state:
            raise LinkError(
                "That does not look like the right address. After approving, copy the whole "
                "address of the page that failed to load, it contains ?code="
            )

        entry = self.pending.get(state)
        if entry is None or entry[0] != discordUserId:
            raise LinkError("That login link has expired or belongs to someone else. Run /link again.")
        if time.monotonic() - entry[1] >= pendingLoginSeconds:
            self.pending.pop(state, None)
            raise LinkError("That login link has expired. Run /link again.")
        self.pending.pop(state, None)

        oauth = buildOauth(discordUserId, state)
        try:
            oauth.get_access_token(code, check_cache=False)
        except SpotifyOauthError:
            raise LinkError("Spotify rejected that code, it may already have been used. Run /link again.")

        sp = spotipy.Spotify(auth_manager=oauth, **spotifyAuth.clientOptions)
        try:
            me = sp.me()
        except spotipy.SpotifyException as err:
            store.deleteUserToken(discordUserId)
            if err.http_status == 403:
                raise LinkError(
                    "Spotify refused this account. The bot's Spotify app is in Development Mode, "
                    "so the bot's owner has to add your Spotify email under User Management on "
                    "developer.spotify.com first. It allows at most 5 people."
                )
            raise LinkError("Spotify did not accept the login, try /link again.")

        displayName = me.get("display_name") or me.get("id") or "your account"
        store.saveUserProfile(discordUserId, me.get("id"), displayName)
        self.forget(discordUserId)
        return displayName

    def unlink(self, discordUserId):
        profile = store.loadUserProfile(discordUserId)
        store.deleteUserToken(discordUserId)
        self.forget(discordUserId)
        return profile

    def forget(self, discordUserId):
        self.clients.pop(discordUserId, None)
        self.libraries.pop(discordUserId, None)

    # Using a linked account

    def client(self, discordUserId):
        tokenInfo = store.loadUserToken(discordUserId)
        if not tokenInfo or not tokenCoversScopes(tokenInfo):
            return None
        sp = self.clients.get(discordUserId)
        if sp is None:
            sp = spotipy.Spotify(auth_manager=buildOauth(discordUserId), **spotifyAuth.clientOptions)
            self.clients[discordUserId] = sp
        return sp

    def library(self, discordUserId):
        # Blocking. Recently played first, then top and liked songs, without repeats.
        # Cached for a while so typing in /play does not call Spotify on every keystroke.
        cached = self.libraries.get(discordUserId)
        if cached and time.monotonic() - cached[0] < libraryCacheSeconds:
            return cached[1]

        sp = self.client(discordUserId)
        if sp is None:
            return []

        sources = [
            ("recently played", lambda: sp.current_user_recently_played(limit=libraryFetchLimit)),
            ("your top", lambda: sp.current_user_top_tracks(limit=libraryFetchLimit, time_range="short_term")),
            ("liked", lambda: sp.current_user_saved_tracks(limit=libraryFetchLimit)),
        ]
        seen = set()
        tracks = []
        for source, fetch in sources:
            try:
                page = fetch() or {}
            except SpotifyOauthError:
                # Refresh token revoked from the Spotify side, /link fixes it.
                return []
            except (spotipy.SpotifyException, requests.RequestException):
                continue
            for entry in page.get("items") or []:
                track = trackFromEntry(entry)
                uri = (track or {}).get("uri")
                if not uri or uri in seen or not uri.startswith("spotify:track:"):
                    continue
                seen.add(uri)
                tracks.append({"uri": uri, "label": catalog.trackLabel(track), "source": source})

        self.libraries[discordUserId] = (time.monotonic(), tracks)
        return tracks
