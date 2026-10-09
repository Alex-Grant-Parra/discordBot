# Apple Music and Amazon Music links. The speaker can only play Spotify, so these links
# are only read for the name of the song, album or artist, and the cog finds the same
# thing on Spotify by searching for it.

import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import aiohttp

requestTimeout = aiohttp.ClientTimeout(total=10)

appleLinkPattern = re.compile(r"https?://(?:geo\.)?(?:music|itunes)\.apple\.com/\S+")
appleKinds = {"song": "track", "album": "album", "artist": "artist"}
applePathPattern = re.compile(r"/(album|song|artist|playlist|station|music-video)/(?:[^/]+/)?([^/?#]+)")
appleCountryPattern = re.compile(r"^/([a-z]{2})/")
appleLookupUrl = "https://itunes.apple.com/lookup"

# Links copied from the Amazon website use www.amazon.<tld>/music/player/..., the app
# shares music.amazon.<tld>/...
amazonLinkPattern = re.compile(
    r"https?://(?:music\.amazon\.([a-z.]+)|(?:www\.)?amazon\.([a-z.]+)/music/player)(/\S*)"
)
amazonKinds = {"music.song": "track", "music.album": "album", "profile": "artist"}
# Amazon Music pages are drawn by JavaScript. Only link preview crawlers are sent the
# name in the page itself, the same tags Discord reads to show a preview.
amazonUserAgent = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
# Song and album titles read "Name – Artist", with an en dash.
amazonTitleSeparator = " – "


class ForeignLinkError(RuntimeError):
    # Carries a message meant to be shown to the person as is.
    pass


def parseLink(text):
    # Returns ("apple" or "amazon", url) for a link this module handles, otherwise None.
    text = (text or "").strip()
    match = appleLinkPattern.search(text)
    if match:
        return "apple", match.group(0)
    match = amazonLinkPattern.search(text)
    if match:
        tld = match.group(1) or match.group(2)
        return "amazon", "https://music.amazon." + tld + match.group(3)
    return None


async def lookUp(text):
    # Returns {"service", "kind", "name", "artist"} for an Apple Music or Amazon Music
    # link, where kind is a Spotify kind and artist is None for an artist.
    service, url = parseLink(text)
    try:
        async with aiohttp.ClientSession(timeout=requestTimeout) as session:
            if service == "apple":
                return await lookUpApple(session, url)
            return await lookUpAmazon(session, url)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
        name = "Apple Music" if service == "apple" else "Amazon Music"
        raise ForeignLinkError(
            "Could not read that " + name + " link right now (" + type(err).__name__ + "). "
            "Try again, or search by name."
        )


async def lookUpApple(session, url):
    parts = urlsplit(url)
    match = applePathPattern.search(parts.path)
    if not match:
        raise ForeignLinkError("That Apple Music link is not a song, album or artist.")
    appleKind, itemId = match.groups()
    # A song opened from its album is the album link with the song's id added.
    songId = (parse_qs(parts.query).get("i") or [None])[0]
    if appleKind == "album" and songId:
        appleKind, itemId = "song", songId
    if appleKind not in appleKinds:
        raise ForeignLinkError(
            "Apple Music " + appleKind.replace("-", " ") + "s cannot be played, only songs, "
            "albums and artists. Paste a Spotify link for playlists."
        )

    # Ids are per store, so the lookup uses the country the link came from.
    country = appleCountryPattern.match(parts.path)
    params = {"id": itemId, "country": country.group(1) if country else "us"}
    async with session.get(appleLookupUrl, params=params) as resp:
        resp.raise_for_status()
        # Sent as text/javascript, so the content type check is skipped.
        data = await resp.json(content_type=None)
    results = data.get("results") or []
    if not results:
        raise ForeignLinkError("Apple Music has nothing at that link.")
    item = results[0]

    kind = appleKinds[appleKind]
    if kind == "track":
        name, artist = item.get("trackName"), item.get("artistName")
    elif kind == "album":
        name, artist = item.get("collectionName"), item.get("artistName")
    else:
        name, artist = item.get("artistName"), None
    if not name:
        raise ForeignLinkError("Apple Music has nothing at that link.")
    return {"service": "Apple Music", "kind": kind, "name": name, "artist": artist}


class MetaReader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.properties = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        key = attrs.get("property") or attrs.get("name")
        if key and key not in self.properties and attrs.get("content") is not None:
            self.properties[key] = attrs["content"]


async def lookUpAmazon(session, url):
    async with session.get(url, headers={"User-Agent": amazonUserAgent}) as resp:
        if resp.status == 404:
            raise ForeignLinkError("Amazon Music has nothing at that link.")
        resp.raise_for_status()
        page = await resp.text()
    reader = MetaReader()
    reader.feed(page)
    pageType = reader.properties.get("og:type")
    title = reader.properties.get("og:title") or ""

    # A link that points at nothing gets the generic Amazon Music page.
    if pageType == "website" or not title:
        raise ForeignLinkError("Amazon Music has nothing at that link.")
    kind = amazonKinds.get(pageType)
    if kind is None:
        what = "playlist" if pageType == "music.playlist" else "link"
        raise ForeignLinkError(
            "That Amazon Music " + what + " cannot be played, only songs, albums and "
            "artists. Paste a Spotify link for playlists."
        )

    if kind == "artist":
        return {"service": "Amazon Music", "kind": kind, "name": title, "artist": None}
    name, separator, artist = title.rpartition(amazonTitleSeparator)
    if not separator:
        name, artist = title, None
    return {"service": "Amazon Music", "kind": kind, "name": name, "artist": artist}
