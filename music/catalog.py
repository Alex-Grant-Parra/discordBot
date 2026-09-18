# Turns what people type into Spotify URIs. Links and URIs are used as they are,
# anything else is a Web API search.

import re

playableKinds = ("track", "album", "playlist", "artist", "episode", "show")

uriPattern = re.compile(r"^spotify:(" + "|".join(playableKinds) + r"):([A-Za-z0-9]{22})$")
linkPattern = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?(" + "|".join(playableKinds) + r")/([A-Za-z0-9]{22})"
)

searchLimit = 10


def parseReference(text):
    # Returns (kind, uri) for a Spotify link or URI, or None for plain search text.
    text = (text or "").strip()
    match = uriPattern.match(text) or linkPattern.search(text)
    if not match:
        return None
    kind, itemId = match.group(1), match.group(2)
    return kind, "spotify:" + kind + ":" + itemId


def trackLabel(item):
    artists = ", ".join(a.get("name", "") for a in (item.get("artists") or []) if a.get("name"))
    name = item.get("name") or "Unknown track"
    return name + " by " + artists if artists else name


def searchTracks(sp, query, limit=searchLimit):
    results = sp.search(query, limit=limit, type="track") or {}
    items = (results.get("tracks") or {}).get("items") or []
    return [
        {"uri": item["uri"], "label": trackLabel(item)}
        for item in items
        if item and item.get("uri") and item.get("is_playable", True) is not False
    ]


def formatMs(ms):
    totalSeconds = max(0, int((ms or 0) / 1000))
    hours, rest = divmod(totalSeconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%d:%02d" % (minutes, seconds)


def parsePosition(text):
    # Accepts 90, 1:30 or 1:02:30 and returns milliseconds, or None if unreadable.
    parts = (text or "").strip().split(":")
    if not parts or len(parts) > 3:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if any(n < 0 for n in numbers):
        return None
    seconds = 0
    for n in numbers:
        seconds = seconds * 60 + n
    return seconds * 1000
