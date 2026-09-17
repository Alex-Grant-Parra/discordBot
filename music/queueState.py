# Internal queue state and the diff against a freshly fetched Spotify playlist.
# Deliberately free of discord and spotipy imports so the reconciliation logic can be
# tested on its own. Changes are treated identically whether they came from Spotify
# directly or from a Discord command, because this only ever sees the resulting list.

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class QueueTrack:
    trackId: str
    name: str
    primaryArtist: str
    artists: list = field(default_factory=list)
    durationMs: int = 0
    uri: str = ""
    addedBy: str = ""

    def label(self):
        if self.primaryArtist:
            return self.name + " by " + self.primaryArtist
        return self.name


@dataclass
class QueueDiff:
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    reordered: bool = False

    @property
    def changed(self):
        return bool(self.added or self.removed or self.reordered)


def takeByCounts(tracks, counts):
    # Pulls out one track object per outstanding count, keeping playlist order.
    remaining = Counter(counts)
    picked = []
    for track in tracks:
        if remaining.get(track.trackId, 0) > 0:
            picked.append(track)
            remaining[track.trackId] -= 1
    return picked


def idsByCounts(trackIds, counts):
    remaining = Counter(counts)
    kept = []
    for trackId in trackIds:
        if remaining.get(trackId, 0) > 0:
            kept.append(trackId)
            remaining[trackId] -= 1
    return kept


def diffTracks(oldTracks, newTracks):
    # Multiset based so a playlist holding the same song twice behaves sensibly.
    oldIds = [track.trackId for track in oldTracks]
    newIds = [track.trackId for track in newTracks]
    oldCounts = Counter(oldIds)
    newCounts = Counter(newIds)

    added = takeByCounts(newTracks, newCounts - oldCounts)
    removed = takeByCounts(oldTracks, oldCounts - newCounts)

    # Reordering is judged only on the entries that survived, otherwise every add
    # or remove would also look like a reorder.
    retainedCounts = oldCounts & newCounts
    reordered = idsByCounts(oldIds, retainedCounts) != idsByCounts(newIds, retainedCounts)

    return QueueDiff(added=added, removed=removed, reordered=reordered)


def occurrenceKeyAt(trackIds, index):
    # Identifies an entry as a track id plus which occurrence of that id it is, so
    # duplicates stay distinguishable when the playlist is edited around them.
    if index < 0 or index >= len(trackIds):
        return None
    trackId = trackIds[index]
    return (trackId, trackIds[:index].count(trackId))


def indexOfOccurrence(trackIds, key):
    if key is None:
        return None
    trackId, ordinal = key
    seen = 0
    for index, candidate in enumerate(trackIds):
        if candidate == trackId:
            if seen == ordinal:
                return index
            seen += 1
    return None


class QueueState:
    # Holds the playlist as the bot last saw it, plus a cursor marking what plays next
    # when the queue mode is cursor. In consume mode the cursor is unused because
    # finished tracks are deleted from the playlist instead.

    def __init__(self):
        self.tracks = []
        self.snapshotId = None
        self.cursor = 0

    def trackIds(self):
        return [track.trackId for track in self.tracks]

    def applyFetched(self, newTracks, snapshotId):
        # Diff first, then move the cursor to match the new ordering, then commit.
        diff = diffTracks(self.tracks, newTracks)
        self.reanchorCursor(newTracks)
        self.tracks = list(newTracks)
        self.snapshotId = snapshotId
        return diff

    def reanchorCursor(self, newTracks):
        oldIds = self.trackIds()
        newIds = [track.trackId for track in newTracks]

        # Anchor on whatever was up next, so inserting or deleting above it does not
        # make the queue jump position.
        found = indexOfOccurrence(newIds, occurrenceKeyAt(oldIds, self.cursor))
        if found is not None:
            self.cursor = found
            return

        # The track that was up next is gone. Count how many already passed entries
        # survived and put the cursor after them.
        consumed = Counter(oldIds[: self.cursor])
        survivors = 0
        for trackId in newIds:
            if consumed.get(trackId, 0) > 0:
                consumed[trackId] -= 1
                survivors += 1
        self.cursor = min(survivors, len(newIds))

    def cursorKey(self):
        # Persisted across restarts so a restart does not replay the whole playlist.
        return occurrenceKeyAt(self.trackIds(), self.cursor)

    def restoreCursor(self, key):
        found = indexOfOccurrence(self.trackIds(), key)
        self.cursor = found if found is not None else 0

    def pendingTracks(self, queueMode):
        # What is still waiting to play. In consume mode the whole playlist is pending
        # because played entries are removed from Spotify as they finish.
        if queueMode == "consume":
            return list(self.tracks)
        return self.tracks[self.cursor :]

    def upNext(self, queueMode):
        pending = self.pendingTracks(queueMode)
        return pending[0] if pending else None
