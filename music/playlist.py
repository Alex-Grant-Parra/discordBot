# Reading the Spotify playlist that acts as the shared queue.
#
# Every call here goes through the items endpoints. Spotify removed the older
# playlists/{id}/tracks endpoints for Development Mode apps in the February 2026
# migration, so spotipy's playlist_tracks must never be used.

import logging

from .queueState import QueueTrack

logger = logging.getLogger("music.playlist")

# Spotify caps a page of playlist entries at 100.
pageLimit = 100


def fetchSnapshotId(sp, playlistId):
    # Cheap call used by the poller. The snapshot id changes on any edit to the
    # playlist, whoever made it, so it is the trigger for a full fetch.
    data = sp.playlist(playlistId, fields="snapshot_id") or {}
    return data.get("snapshot_id")


def fetchEntries(sp, playlistId):
    entries = []
    offset = 0
    while True:
        page = sp.playlist_items(
            playlistId,
            limit=pageLimit,
            offset=offset,
            additional_types=("track",),
        ) or {}
        pageItems = page.get("items") or []
        entries.extend(pageItems)
        if len(pageItems) < pageLimit or not page.get("next"):
            break
        offset += pageLimit
    return entries


def parseEntry(entry):
    # The February 2026 migration renamed the per entry payload from track to item,
    # so accept either shape rather than depending on which one the API returns.
    payload = entry.get("item") or entry.get("track")
    if not payload:
        return None

    # Podcast episodes and anything else that is not a track cannot be matched.
    if payload.get("type") not in (None, "track"):
        return None

    trackId = payload.get("id")
    if not trackId:
        # Local files carry no id and cannot be looked up on YouTube.
        return None

    artists = [artist.get("name", "") for artist in (payload.get("artists") or [])]
    artists = [name for name in artists if name]

    return QueueTrack(
        trackId=trackId,
        name=payload.get("name") or "unknown",
        primaryArtist=artists[0] if artists else "",
        artists=artists,
        durationMs=int(payload.get("duration_ms") or 0),
        uri=payload.get("uri") or "",
        addedBy=(entry.get("added_by") or {}).get("id") or "",
    )


def fetchTracks(sp, playlistId):
    tracks = []
    skipped = 0
    for entry in fetchEntries(sp, playlistId):
        track = parseEntry(entry)
        if track is None:
            skipped += 1
            continue
        tracks.append(track)
    if skipped:
        logger.info("Ignored %d playlist entries that are not playable tracks", skipped)
    return tracks
