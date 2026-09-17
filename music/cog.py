# Music cog.
#
# Stage two scope: poll the Spotify playlist and log what changed. No audio, no
# voice connection, no slash commands yet. The poller is the single place that
# learns about queue changes, so edits made in Spotify and edits made later by
# Discord commands are handled by exactly the same code path.

import asyncio
import logging
import os

from discord.ext import commands, tasks

from . import playlist as playlistApi
from . import spotifyAuth, store
from .queueState import QueueState

logger = logging.getLogger("music")

defaultPollSeconds = 7
minPollSeconds = 5
maxPollSeconds = 60

# Backs off after repeated failures so a Spotify outage does not spam the log.
maxBackoffMultiplier = 8


def readPollSeconds():
    raw = os.getenv("MUSIC_POLL_SECONDS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return defaultPollSeconds
    return max(minPollSeconds, min(maxPollSeconds, value))


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sp = None
        self.playlistId = None
        self.state = QueueState()
        self.pollSeconds = readPollSeconds()

        # The first successful poll only records what is already there. Those tracks
        # are not new arrivals and must not be queued up as if someone just added them.
        self.primed = False
        self.consecutiveFailures = 0

    async def cog_load(self):
        store.initDb()
        try:
            self.sp = await self.runBlocking(spotifyAuth.getSpotifyClient)
        except (spotifyAuth.SpotifyAuthNotReady, spotifyAuth.SpotifyConfigError) as err:
            logger.error("Spotify is not ready, the playlist poller will not start: %s", err)
            return

        self.playlistId = spotifyAuth.playlistIdFromEnvOrConfig()
        if not self.playlistId:
            logger.error("No Spotify playlist is configured, run spotifyLogin.py first")
            return

        queueMode = store.getConfig("queueMode")
        logger.info(
            "Polling Spotify playlist %s every %d seconds in %s mode",
            self.playlistId,
            self.pollSeconds,
            queueMode,
        )
        self.pollPlaylist.change_interval(seconds=self.pollSeconds)
        self.pollPlaylist.start()

    async def cog_unload(self):
        self.pollPlaylist.cancel()

    async def runBlocking(self, func, *args):
        # spotipy uses blocking requests calls, including its token refresh, so every
        # call has to leave the event loop or the whole bot stalls behind it.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    @tasks.loop(seconds=defaultPollSeconds)
    async def pollPlaylist(self):
        try:
            await self.pollOnce()
            self.consecutiveFailures = 0
        except Exception:
            # Never let a bad poll kill the loop. tasks.loop would otherwise stop
            # permanently on an unhandled exception.
            self.consecutiveFailures += 1
            logger.exception(
                "Playlist poll failed (%d in a row), continuing", self.consecutiveFailures
            )
            await self.applyBackoff()

    async def applyBackoff(self):
        if self.consecutiveFailures < 3:
            return
        multiplier = min(maxBackoffMultiplier, 2 ** (self.consecutiveFailures - 2))
        backedOff = min(maxPollSeconds, self.pollSeconds * multiplier)
        if self.pollPlaylist.seconds != backedOff:
            logger.warning("Backing the poll interval off to %d seconds", backedOff)
            self.pollPlaylist.change_interval(seconds=backedOff)

    async def restoreInterval(self):
        if self.pollPlaylist.seconds != self.pollSeconds:
            logger.info("Restoring the poll interval to %d seconds", self.pollSeconds)
            self.pollPlaylist.change_interval(seconds=self.pollSeconds)

    async def pollOnce(self):
        snapshotId = await self.runBlocking(
            playlistApi.fetchSnapshotId, self.sp, self.playlistId
        )
        await self.restoreInterval()

        # The snapshot id changes on any edit from any source, so an unchanged one
        # means there is nothing to fetch.
        if self.primed and snapshotId == self.state.snapshotId:
            return

        tracks = await self.runBlocking(playlistApi.fetchTracks, self.sp, self.playlistId)
        diff = self.state.applyFetched(tracks, snapshotId)

        if not self.primed:
            self.primed = True
            self.restoreCursorFromConfig()
            self.logInitialState()
            return

        if diff.changed:
            await self.logDiff(diff)

    def restoreCursorFromConfig(self):
        # Picks the queue back up where it left off instead of replaying everything
        # after a restart. Falls back to the top if that entry is gone.
        storedId = store.getConfig("cursorTrackId") or ""
        storedOrdinal = store.getConfig("cursorOrdinal") or ""
        if not storedId:
            return
        try:
            key = (storedId, int(storedOrdinal))
        except (TypeError, ValueError):
            return
        self.state.restoreCursor(key)
        logger.info("Restored the queue cursor to position %d", self.state.cursor)

    def logInitialState(self):
        queueMode = store.getConfig("queueMode")
        pending = self.state.pendingTracks(queueMode)
        logger.info(
            "Initial playlist state: %d tracks, %d pending, snapshot %s",
            len(self.state.tracks),
            len(pending),
            self.state.snapshotId,
        )
        for position, track in enumerate(pending[:10], start=1):
            logger.info("  %2d. %s", position, track.label())
        if len(pending) > 10:
            logger.info("  ... and %d more", len(pending) - 10)

    async def logDiff(self, diff):
        queueMode = store.getConfig("queueMode")
        logger.info("Playlist changed, snapshot now %s", self.state.snapshotId)

        for track in diff.added:
            cached = store.getCachedMatch(track.trackId)
            if cached:
                logger.info(
                    "  added: %s (already mapped to youtube id %s, no search needed)",
                    track.label(),
                    cached["youtubeVideoId"],
                )
            else:
                # Stage three turns this into a real yt_dlp search. Resolution is
                # driven only by genuinely new tracks, never by the whole playlist.
                logger.info(
                    "  added: %s (would resolve on youtube, duration %s)",
                    track.label(),
                    formatDuration(track.durationMs),
                )

        for track in diff.removed:
            logger.info("  removed: %s", track.label())

        if diff.reordered:
            logger.info("  reordered: the surviving tracks are in a different order")

        pending = self.state.pendingTracks(queueMode)
        upNext = self.state.upNext(queueMode)
        logger.info(
            "  queue now %d tracks, %d pending, up next %s",
            len(self.state.tracks),
            len(pending),
            upNext.label() if upNext else "nothing",
        )


def formatDuration(durationMs):
    totalSeconds = int((durationMs or 0) / 1000)
    return "%d:%02d" % (totalSeconds // 60, totalSeconds % 60)


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
