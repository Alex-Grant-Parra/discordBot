# The shared collaborative playlist. Anyone invited as a collaborator can add songs to it
# from their own Spotify app, free accounts included, and the bot queues each new song on
# the speaker. It is how people who cannot log in to the speaker's account still control
# the music from Spotify.
#
# Reads go through the playlist items endpoint. Spotify removed playlists/{id}/tracks for
# Development Mode apps in the February 2026 migration.

from datetime import datetime, timedelta, timezone

from . import catalog

pageLimit = 100


def nowStamp():
    # Same format as Spotify's added_at, so the two compare as plain strings.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stampSecondsAgo(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetchSnapshotId(sp, playlistId):
    # Changes on any edit, whoever made it, so it is a cheap check for new songs.
    data = sp.playlist(playlistId, fields="snapshot_id") or {}
    return data.get("snapshot_id")


def parseEntry(entry):
    # The February 2026 migration renamed the per entry payload from track to item.
    payload = entry.get("item") or entry.get("track")
    if not payload or payload.get("type") not in (None, "track", "episode"):
        return None
    uri = payload.get("uri") or ""
    addedAt = entry.get("added_at") or ""
    # Local files cannot be streamed, and entries without a date predate tracking it.
    if not uri.startswith(("spotify:track:", "spotify:episode:")) or not addedAt:
        return None
    return {
        "uri": uri,
        "label": catalog.trackLabel(payload),
        "addedAt": addedAt,
        "addedBy": (entry.get("added_by") or {}).get("id") or "",
    }


def fetchEntries(sp, playlistId):
    entries = []
    offset = 0
    while True:
        page = sp.playlist_items(playlistId, limit=pageLimit, offset=offset, additional_types=("track", "episode")) or {}
        items = page.get("items") or []
        for item in items:
            parsed = parseEntry(item)
            if parsed is not None:
                entries.append(parsed)
        if len(items) < pageLimit or not page.get("next"):
            return entries
        offset += pageLimit


def entriesAddedAfter(entries, watermark):
    return sorted((e for e in entries if e["addedAt"] > watermark), key=lambda e: e["addedAt"])
