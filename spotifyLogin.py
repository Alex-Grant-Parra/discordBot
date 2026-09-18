# One time Spotify login. Run this once:
#
#     python spotifyLogin.py           normal login
#     python spotifyLogin.py force     discard the stored token and log in again
#     python spotifyLogin.py paste     skip the local callback server, paste the URL by hand
#
# It sends you to the Spotify consent page, captures the redirect, and stores the
# refresh token in SQLite. Every later bot start refreshes silently, so this never
# needs running again unless the token is revoked or the scopes change.
#
# On a headless machine there is no browser to catch the redirect, so the paste flow
# is used automatically: you open the URL on your own computer, approve, and then copy
# the address bar back here. The redirect page failing to load is expected and fine,
# the only thing that matters is the URL it tried to reach.

import os
import sys
import webbrowser

from dotenv import load_dotenv

load_dotenv()

import spotipy

from music import spotifyAuth, store


def looksHeadless():
    # No display means no usable browser, so the local callback server would never be reached.
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def pasteFlow(oauth, authUrl):
    print("Open this URL in a browser, log in as the bot's Spotify account, then approve:")
    print()
    print(authUrl)
    print()
    print("Your browser will then be redirected to a page that fails to load. That is expected.")
    responseUrl = input("Paste the full URL from the address bar, then press enter: ").strip()
    if not responseUrl:
        raise SystemExit("Nothing pasted, aborting.")
    return oauth.parse_response_code(responseUrl)


def obtainCode(oauth, authUrl, forcePaste):
    if forcePaste or looksHeadless():
        return pasteFlow(oauth, authUrl)

    try:
        webbrowser.open(authUrl)
        print("A browser window should have opened. Approve the request there.")
        print("Waiting for the redirect on the local callback server...")
        return oauth.get_auth_response()
    except Exception as err:
        print("Local callback server did not work (" + str(err) + "), falling back to paste.")
        print()
        return pasteFlow(oauth, authUrl)


def readTokenInfo(oauth, code):
    # spotipy deprecated the as_dict argument, and older versions return a bare string.
    # Whatever comes back, the cache handler has already written the real token, so read that.
    result = oauth.get_access_token(code, check_cache=False)
    if isinstance(result, dict) and result.get("refresh_token"):
        return result
    return store.loadToken()


def runLogin(force=False, forcePaste=False):
    store.initDb()

    if not force and spotifyAuth.hasStoredToken() and spotifyAuth.tokenCoversScopes():
        print("A Spotify token is already stored and covers all required scopes.")
        print("To log in again anyway, run: python spotifyLogin.py force")
        print()
        return spotifyAuth.getSpotifyClient()

    if force:
        store.clearToken()

    oauth = spotifyAuth.buildOauth(openBrowser=not forcePaste)
    authUrl = oauth.get_authorize_url()

    print("Requesting these scopes:")
    for scope in spotifyAuth.spotifyScopes.split():
        print("  " + scope)
    print()

    code = obtainCode(oauth, authUrl, forcePaste)
    tokenInfo = readTokenInfo(oauth, code)

    if not tokenInfo or not tokenInfo.get("refresh_token"):
        raise SystemExit("Spotify did not return a refresh token. Check the credentials and try again.")

    print()
    print("Login succeeded. Refresh token stored in " + str(store.dbPath) + ".")
    return spotipy.Spotify(auth_manager=oauth)


def main():
    flags = {arg.lower() for arg in sys.argv[1:]}
    force = bool(flags & {"force", "relogin"})
    forcePaste = "paste" in flags

    try:
        sp = runLogin(force=force, forcePaste=forcePaste)
    except spotifyAuth.SpotifyConfigError as err:
        raise SystemExit(str(err))

    me = sp.me()
    print("Authorized as: " + str(me.get("display_name") or me.get("id")))
    print("Account tier:  " + str(me.get("product", "unknown")))

    playlist = spotifyAuth.ensurePlaylist(sp)
    print()
    print("Created a new collaborative playlist." if playlist["created"] else "Using the existing playlist.")
    print("  Name:          " + str(playlist["name"]))
    print("  Playlist ID:   " + playlist["id"])
    print("  Collaborative: " + str(playlist["collaborative"]))
    print("  Share link:    " + (playlist["url"] or "unavailable"))
    print()

    if not playlist["owned"]:
        print("Warning: this playlist belongs to another account, so the bot cannot change its settings.")
    if not playlist["collaborative"]:
        print("Warning: the playlist is not collaborative, so nobody else can add songs from Spotify.")

    print("Use /playlist in Discord to play it. To let people add songs from their own Spotify")
    print("accounts, open it in the Spotify app and send them an Invite collaborators link.")


if __name__ == "__main__":
    main()
