# Music cog. The bot is a Spotify Connect speaker: go-librespot plays the audio and the
# bot relays it into a voice channel. People control it from the Spotify app, where it
# shows up as a device for anyone logged into the linked account, or with the slash
# commands here. Both drive the same player, so they always agree.

import asyncio
import json
import logging
import os
import time
from collections import Counter, OrderedDict

import aiohttp
import discord
import requests
import spotipy
from discord import app_commands
from discord.ext import commands, tasks
from spotipy.oauth2 import SpotifyOauthError

from . import catalog, sharedPlaylist, spotifyAuth, store
from .audioBridge import AudioBridge, BridgeSource
from .userAccounts import LinkError, UserAccounts
from .librespot import (
    LibrespotApi,
    LibrespotError,
    LibrespotProcess,
    NotLinkedError,
    apiPort,
    binaryPath,
    deviceName,
    fifoPath,
)

logger = logging.getLogger("music")

spotifyGreen = discord.Colour.from_rgb(30, 215, 96)
defaultIdleSeconds = 300
maxRememberedLabels = 500
maxQueueLines = 10
maxHistory = 50
maxChoices = 25  # Discord's cap on autocomplete suggestions

# The shared playlist is checked often while the bot is in voice and rarely otherwise,
# because every check counts against the Spotify app's Development Mode quota.
defaultPlaylistPollSeconds = 30
idlePlaylistPollSeconds = 300
# Songs added while the bot was offline longer than this are not queued on its return.
playlistBacklogSeconds = 12 * 3600


class UserFacingError(Exception):
    # Raised by commands with a message meant to be shown to the person as is.
    pass


def readIdleSeconds():
    try:
        value = int(os.getenv("MUSIC_IDLE_TIMEOUT", str(defaultIdleSeconds)))
    except ValueError:
        return defaultIdleSeconds
    return max(30, value)


def readPlaylistPollSeconds():
    try:
        value = int(os.getenv("MUSIC_POLL_SECONDS", str(defaultPlaylistPollSeconds)))
    except ValueError:
        return defaultPlaylistPollSeconds
    return max(15, min(idlePlaylistPollSeconds, value))


def envChannelId(name):
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return 0


def openUrl(uri):
    parts = (uri or "").split(":")
    if len(parts) == 3 and parts[0] == "spotify":
        return "https://open.spotify.com/" + parts[1] + "/" + parts[2]
    return None


def describeTrack(track):
    artists = ", ".join(track.get("artist_names") or [])
    name = track.get("name") or "Unknown track"
    return name + " by " + artists if artists else name


def webApiTrackLabel(item):
    # Queue entries from the Web API are tracks or podcast episodes.
    if item.get("type") == "episode":
        return (item.get("name") or "Unknown episode") + " (podcast)"
    return catalog.trackLabel(item)


def userVoiceChannel(interaction):
    return getattr(getattr(interaction.user, "voice", None), "channel", None)


class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api = LibrespotApi(apiPort())
        self.process = LibrespotProcess()
        self.bridge = AudioBridge(fifoPath)
        self.sp = None
        self.speakerStarted = False
        self.linked = False
        self.playing = False
        self.lastAnnouncedUri = None
        self.currentTrack = None
        # Tell the voice listener whether a disconnect was the bot's own doing.
        self.leavingOnPurpose = False
        self.switchingFromGuilds = set()
        self.unloading = False
        # Songs played before the current one, newest last. go-librespot's own previous
        # only walks back through the current album or playlist and forgets queued
        # songs, which is most of what gets played here.
        self.history = []
        self.currentUri = None
        self.backTarget = None
        # Play/pause state last drawn on the now playing buttons, None when unknown.
        self.controlsPaused = None
        self.controls = None
        self.warnedNoChannel = False
        self.voiceLock = asyncio.Lock()
        self.idleSince = None
        self.idleSeconds = readIdleSeconds()
        self.backgroundTasks = []
        # Autocomplete hands back only a URI, this keeps the matching song name so the
        # reply can say what was played.
        self.labels = OrderedDict()
        # Who asked for each song, shown when it starts and in /queue.
        self.requestedBy = OrderedDict()
        self.accounts = UserAccounts()
        self.playlistPollSeconds = readPlaylistPollSeconds()
        self.playlistSnapshot = None
        self.playlistBlockedUntil = 0.0
        self.playlistWake = asyncio.Event()
        self.playlistLock = asyncio.Lock()
        # Songs from /play waiting to be written to the shared playlist, and the ones
        # written but not yet seen by the watcher, which must not queue them a second time.
        self.pendingPlaylistAdds = []
        self.selfAddedUris = Counter()

    async def cog_load(self):
        store.initDb()
        self.loadHistory()
        await self.api.open()
        self.sp = await self.loadWebApi()
        # Registered once so buttons on messages posted before a restart keep working.
        self.controls = PlayerControls(self)
        self.bot.add_view(self.controls)

        if not binaryPath.exists():
            logger.error(
                "go-librespot is not installed, so the Spotify speaker is off. "
                "Stop the bot and run: python speakerSetup.py"
            )
            return

        # Songs already in the shared playlist the first time this runs are not new.
        if not store.getConfig("playlistWatermark"):
            store.setConfig("playlistWatermark", sharedPlaylist.nowStamp())

        self.bridge.start()
        self.process.start()
        self.speakerStarted = True
        self.backgroundTasks = [
            asyncio.create_task(self.consumeEvents()),
            asyncio.create_task(self.watchLogin()),
            asyncio.create_task(self.watchPlaylist()),
        ]
        self.idleCheck.start()

    async def cog_unload(self):
        self.unloading = True
        self.idleCheck.cancel()
        if self.controls is not None:
            self.controls.stop()
        for task in self.backgroundTasks:
            task.cancel()
        for vc in list(self.bot.voice_clients):
            await vc.disconnect(force=True)
        await self.process.stop()
        self.bridge.stop()
        await self.api.close()

    async def loadWebApi(self):
        # Only used for searching by name and for the shared playlist. Links still work
        # without it, so a missing login is a warning rather than a failure.
        try:
            return await self.runBlocking(spotifyAuth.getSpotifyClient)
        except (spotifyAuth.SpotifyAuthNotReady, spotifyAuth.SpotifyConfigError) as err:
            logger.warning("Spotify search is unavailable: %s", err)
            return None

    async def runBlocking(self, func, *args):
        # spotipy makes blocking HTTP calls, including its token refresh.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    # Speaker events

    async def watchLogin(self):
        # Until an account is linked, repeats the pairing code in the log so it can be
        # found without digging through go-librespot's own output. The code is never
        # posted to Discord, whoever approves it decides which account the speaker uses.
        lastCode = None
        while True:
            await asyncio.sleep(3)
            try:
                code = await self.api.authCode()
                if code:
                    if code.get("code") != lastCode:
                        lastCode = code.get("code")
                        logger.warning(
                            "No Spotify account is linked to the speaker. Open %s and approve, "
                            "entering code %s if asked. Log in as the account everyone will share.",
                            code.get("url"),
                            lastCode,
                        )
                    continue
                status = await self.api.status(timeout=aiohttp.ClientTimeout(total=3))
            except LibrespotError:
                continue
            if status is not None:
                self.linked = True
                self.playing = self.statusIsPlaying(status)
                logger.info(
                    "Spotify speaker '%s' is online, linked to Spotify account %s",
                    status.get("device_name"),
                    status.get("username"),
                )
                return

    def statusIsPlaying(self, status):
        return bool(status.get("track")) and not status.get("paused") and not status.get("stopped")

    async def consumeEvents(self):
        async for event in self.api.events():
            try:
                await self.handleEvent(event.get("type"), event.get("data") or {})
            except Exception:
                logger.exception("Failed handling speaker event %s", event.get("type"))

    async def handleEvent(self, kind, data):
        if kind == "playing":
            self.playing = True
            await self.joinForPlayback()
            vc = self.voiceClient()
            if vc is not None and vc.is_paused():
                vc.resume()
            # Leaving voice deletes the now playing message, so resuming the same song
            # afterwards needs it posted again.
            if not store.getConfig("nowPlayingMessage") and self.currentTrack:
                await self.announceTrack(self.currentTrack)
            await self.refreshControls(paused=False)
        elif kind in ("paused", "stopped", "inactive"):
            # Pausing the voice client stops the bot transmitting silence. What little
            # audio is still in flight stays buffered and plays first on resume.
            self.playing = False
            vc = self.voiceClient()
            if vc is not None and vc.is_playing():
                vc.pause()
            await self.refreshControls(paused=True)
        elif kind == "metadata":
            self.currentTrack = data
            if data.get("uri"):
                self.rememberLabel(data["uri"], describeTrack(data))
            self.recordHistory(data.get("uri"))
            await self.announceTrack(data)

    def loadHistory(self):
        try:
            saved = json.loads(store.getConfig("playHistory") or "{}")
        except ValueError:
            saved = {}
        self.history = [uri for uri in saved.get("history") or [] if isinstance(uri, str)][-maxHistory:]
        self.currentUri = saved.get("current")

    def recordHistory(self, uri):
        if not uri or uri == self.currentUri:
            return
        if uri == self.backTarget:
            # Arrived by going back, so the song left behind is not history.
            self.backTarget = None
        elif self.currentUri:
            self.history.append(self.currentUri)
            del self.history[:-maxHistory]
        self.currentUri = uri
        # Saved so Previous still works straight after a restart.
        store.setConfig("playHistory", json.dumps({"history": self.history, "current": uri}))

    async def goBack(self):
        # Returns the label of the song gone back to, or None when there was nothing
        # earlier and the current song restarted instead.
        if not self.history:
            logger.info("Previous: nothing played before %s, restarting it", self.currentUri)
            await self.api.seek(0)
            return None
        target = self.history.pop()
        self.backTarget = target
        logger.info("Previous: going back from %s to %s", self.currentUri, target)
        try:
            await self.api.skipTo(target)
        except LibrespotError:
            self.history.append(target)
            self.backTarget = None
            raise
        asyncio.create_task(self.confirmWentBack(target))
        return self.labels.get(target) or "the previous song"

    async def restartSong(self):
        logger.info("Restart: back to the start of %s", self.currentUri)
        await self.api.seek(0)

    async def confirmWentBack(self, target):
        # go-librespot accepts a skip even when it then fails to carry it out, and only
        # says why in its own log. This makes that visible next to the request.
        for _ in range(10):
            await asyncio.sleep(0.5)
            if self.currentUri == target:
                return
        logger.warning("Previous: the speaker did not start %s within 5 seconds", target)

    async def announceTrack(self, track):
        uri = track.get("uri")
        if not uri or uri == self.lastAnnouncedUri:
            return
        self.lastAnnouncedUri = uri
        embed = self.trackEmbed(track, "Now playing")
        requester = self.requestedBy.pop(uri, None)
        if requester:
            embed.set_footer(text="Requested by " + requester)

        channel = self.textChannel()
        if channel is None:
            return
        await self.deleteNowPlayingMessage()
        paused = not self.playing
        try:
            message = await channel.send(embed=embed, view=PlayerControls(self, paused=paused))
        except discord.HTTPException:
            logger.exception("Failed to post the now playing message")
            return
        self.controlsPaused = paused
        # Kept in the database so the message is still cleaned up after a restart.
        store.setConfig("nowPlayingMessage", str(channel.id) + ":" + str(message.id))

    def nowPlayingMessage(self):
        channelId, _, messageId = (store.getConfig("nowPlayingMessage") or "").partition(":")
        channel = self.bot.get_channel(int(channelId)) if channelId.isdigit() else None
        if channel is None or not messageId.isdigit():
            return None
        return channel.get_partial_message(int(messageId))

    async def deleteNowPlayingMessage(self):
        message = self.nowPlayingMessage()
        store.setConfig("nowPlayingMessage", "")
        if message is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # Already deleted by someone, or too old to matter.

    async def refreshControls(self, paused):
        # Switches the play/pause button between Pause and Resume.
        if paused == self.controlsPaused:
            return
        message = self.nowPlayingMessage()
        if message is None:
            return
        self.controlsPaused = paused
        try:
            await message.edit(view=PlayerControls(self, paused=paused))
        except discord.HTTPException:
            pass

    async def pressControl(self, interaction, action):
        try:
            await self.requireSpeaker()
            await action()
        except (UserFacingError, LibrespotError) as err:
            await interaction.response.send_message(self.describeError(err), ephemeral=True)
            return
        # The message updates itself from the speaker's events, nothing to reply.
        await interaction.response.defer()

    def trackEmbed(self, track, heading, showPosition=False):
        embed = discord.Embed(
            title=track.get("name") or "Unknown track",
            url=openUrl(track.get("uri")),
            description=", ".join(track.get("artist_names") or []) or None,
            colour=spotifyGreen,
        )
        embed.set_author(name=heading)
        if track.get("album_name"):
            embed.add_field(name="Album", value=track["album_name"])
        if track.get("duration") and showPosition:
            embed.add_field(
                name="Position",
                value=catalog.formatMs(track.get("position")) + " / " + catalog.formatMs(track["duration"]),
            )
        elif track.get("duration"):
            embed.add_field(name="Length", value=catalog.formatMs(track["duration"]))
        if track.get("album_cover_url"):
            embed.set_thumbnail(url=track["album_cover_url"])
        return embed

    # Shared playlist

    async def watchPlaylist(self):
        await self.bot.wait_until_ready()
        while True:
            delay = self.playlistPollSeconds if self.voiceClient() else idlePlaylistPollSeconds
            try:
                async with self.playlistLock:
                    await self.checkPlaylist()
            except (spotipy.SpotifyException, SpotifyOauthError, requests.RequestException) as err:
                delay = max(delay, self.notePlaylistError(err))
            except LibrespotError as err:
                # The song stays after the watermark, so it is queued on the next check.
                logger.warning("Could not queue a song from the shared playlist: %s", err)
            except Exception:
                logger.exception("Checking the shared playlist failed")

            self.playlistWake.clear()
            try:
                await asyncio.wait_for(self.playlistWake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def notePlaylistError(self, err):
        # Returns how long to leave the playlist alone.
        if isinstance(err, spotipy.SpotifyException) and err.http_status == 429:
            wait = spotifyAuth.retryAfterSeconds(err)
            self.playlistBlockedUntil = time.monotonic() + wait
            logger.warning(
                "Spotify's quota for this app is used up, the shared playlist is left alone for "
                "%s. Search and playback keep working meanwhile.",
                catalog.formatMs(wait * 1000),
            )
            return wait
        logger.warning("Shared playlist request failed: %s", err)
        return 0

    def addToSharedPlaylist(self, uri):
        self.pendingPlaylistAdds.append(uri)
        asyncio.create_task(self.flushPlaylistAddsSafely())

    async def flushPlaylistAddsSafely(self):
        try:
            async with self.playlistLock:
                await self.flushPlaylistAdds()
        except (spotipy.SpotifyException, SpotifyOauthError, requests.RequestException) as err:
            # Left pending, the watcher retries once Spotify allows it again.
            self.notePlaylistError(err)
        except Exception:
            logger.exception("Adding to the shared playlist failed")

    async def flushPlaylistAdds(self):
        # Caller holds playlistLock.
        playlistId = spotifyAuth.playlistIdFromEnvOrConfig()
        if self.sp is None or not playlistId or not self.pendingPlaylistAdds:
            return
        if time.monotonic() < self.playlistBlockedUntil:
            return
        batch = self.pendingPlaylistAdds[:100]
        # Counted before the request so a check running right after cannot queue them.
        self.selfAddedUris.update(batch)
        try:
            await self.runBlocking(self.sp.playlist_add_items, playlistId, batch)
        except Exception:
            self.selfAddedUris.subtract(batch)
            raise
        del self.pendingPlaylistAdds[: len(batch)]
        logger.info("Added %d song(s) from /play to the shared playlist", len(batch))

    async def checkPlaylist(self):
        # Caller holds playlistLock.
        playlistId = spotifyAuth.playlistIdFromEnvOrConfig()
        if self.sp is None or not playlistId or not self.linked:
            return
        if time.monotonic() < self.playlistBlockedUntil:
            return
        await self.flushPlaylistAdds()

        snapshot = await self.runBlocking(sharedPlaylist.fetchSnapshotId, self.sp, playlistId)
        if snapshot == self.playlistSnapshot:
            return
        entries = await self.runBlocking(sharedPlaylist.fetchEntries, self.sp, playlistId)

        cutoff = sharedPlaylist.stampSecondsAgo(playlistBacklogSeconds)
        watermark = max(store.getConfig("playlistWatermark") or "", cutoff)
        for entry in sharedPlaylist.entriesAddedAfter(entries, watermark):
            if self.selfAddedUris[entry["uri"]] > 0:
                # Came from /play, which has already queued it.
                self.selfAddedUris[entry["uri"]] -= 1
            else:
                await self.queueFromPlaylist(entry)
            store.setConfig("playlistWatermark", entry["addedAt"])
        self.playlistSnapshot = snapshot

    async def queueFromPlaylist(self, entry):
        uri, label = entry["uri"], entry["label"]
        name = await self.discordNameForSpotifyUser(entry["addedBy"])
        self.rememberLabel(uri, label)
        if name:
            self.rememberRequester(uri, name)

        # Paused music stays paused, the song just waits in the queue.
        status = await self.api.status()
        if status and status.get("track") and not status.get("stopped"):
            await self.api.addToQueue(uri)
            outcome = "it is in the queue."
        else:
            await self.api.play(uri)
            outcome = "playing it now."
        logger.info("Shared playlist: %s added %s", name or "a collaborator", label)
        await self.notify((name or "Someone") + " added " + label + " from Spotify, " + outcome)

    async def discordNameForSpotifyUser(self, spotifyUserId):
        # Only people who used /link can be named, Spotify no longer lets apps look up
        # other users' profiles.
        discordUserId = store.findDiscordUserBySpotifyId(spotifyUserId) if spotifyUserId else None
        if not discordUserId:
            return None
        user = self.bot.get_user(int(discordUserId))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(discordUserId))
            except discord.HTTPException:
                return None
        return user.display_name

    # Voice

    def voiceClient(self):
        # The speaker has one audio stream, so the bot is in at most one channel at a time.
        for vc in self.bot.voice_clients:
            return vc
        return None

    def homeVoiceChannel(self):
        channelId = 0
        try:
            channelId = int(store.getConfig("voiceChannelId") or 0)
        except ValueError:
            pass
        channelId = channelId or envChannelId("MUSIC_VOICE_CHANNEL_ID")
        channel = self.bot.get_channel(channelId) if channelId else None
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        return None

    async def joinForPlayback(self):
        # Playback started from the Spotify app. Join the channel used last so casting
        # works without anyone having to run a command first.
        if self.voiceClient() is not None:
            return
        channel = self.homeVoiceChannel()
        if channel is None:
            if not self.warnedNoChannel:
                self.warnedNoChannel = True
                await self.notify(
                    "Spotify started playing, but I do not know which voice channel to use. "
                    "Join one and run /join."
                )
            return
        try:
            await self.connectTo(channel)
        except Exception:
            logger.exception("Could not join %s for playback", channel.name)

    async def connectTo(self, channel):
        async with self.voiceLock:
            vc = self.voiceClient()
            if vc is not None and vc.channel.id == channel.id:
                pass
            elif vc is not None and vc.guild.id == channel.guild.id:
                await vc.move_to(channel)
            else:
                if vc is not None:
                    # Moving to another server, not leaving, so no cleanup for this one.
                    self.switchingFromGuilds.add(vc.guild.id)
                    await vc.disconnect(force=True)
                vc = await channel.connect(timeout=20.0, reconnect=True, self_deaf=True)

            store.setConfig("voiceChannelId", channel.id)
            self.warnedNoChannel = False
            self.playlistWake.set()
            self.idleSince = None

            if not vc.is_playing() and not vc.is_paused():
                vc.play(BridgeSource(self.bridge))
            if not self.playing and vc.is_playing():
                vc.pause()
            return vc

    async def connectForCommand(self, interaction):
        # Joins the caller's channel unless the bot is already somewhere, so one person's
        # command never pulls the music away from everyone else.
        if self.voiceClient() is not None:
            return
        channel = userVoiceChannel(interaction)
        if channel is None:
            channel = self.homeVoiceChannel()
        if channel is None:
            raise UserFacingError("Join a voice channel first so I know where to play.")
        try:
            await self.connectTo(channel)
        except asyncio.TimeoutError:
            raise UserFacingError(
                "Could not connect to voice within 20 seconds. The usual cause is outbound "
                "UDP to Discord voice being blocked on the bot's host."
            )

    # Messages

    def textChannel(self):
        channelId = envChannelId("MUSIC_TEXT_CHANNEL_ID")
        if not channelId:
            return None
        channel = self.bot.get_channel(channelId)
        if channel is None:
            logger.warning("Text channel %d is not visible to the bot", channelId)
        return channel

    async def notify(self, message=None, embed=None):
        channel = self.textChannel()
        if channel is None:
            if message:
                logger.info("No text channel available, message not posted: %s", message)
            return
        try:
            await channel.send(content=message, embed=embed)
        except discord.HTTPException:
            logger.exception("Failed to post a message to channel %d", channel.id)

    async def reply(self, interaction, message=None, embed=None, ephemeral=False, view=None):
        kwargs = {"ephemeral": ephemeral}
        if message is not None:
            kwargs["content"] = message
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    def describeError(self, original):
        if isinstance(original, UserFacingError):
            return str(original)
        if isinstance(original, NotLinkedError):
            return (
                "The speaker is not paired with a Spotify account yet, so nothing can play. "
                "The pairing link is in the bot's log for the Premium account's owner to approve."
            )
        if isinstance(original, LibrespotError):
            logger.warning("Speaker command failed: %s", original)
            return "The Spotify speaker could not do that: " + str(original)
        logger.exception("Music command failed", exc_info=original)
        return "Something went wrong: " + str(original)

    async def cog_app_command_error(self, interaction, error):
        message = self.describeError(getattr(error, "original", error))
        try:
            await self.reply(interaction, message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not report a command error")

    async def requireSpeaker(self, needLinked=True):
        if not self.speakerStarted:
            raise UserFacingError(
                "The Spotify speaker is not installed. The bot's owner needs to run "
                "python speakerSetup.py and restart the bot."
            )
        if self.linked or not needLinked:
            return
        # Every other request would hang until an account is linked.
        if await self.api.authCode():
            raise NotLinkedError()
        raise UserFacingError("The Spotify speaker is still starting up, try again in a few seconds.")

    def who(self, interaction):
        return interaction.user.display_name

    # Resolving what to play

    def rememberLabel(self, uri, label):
        self.labels[uri] = label
        self.labels.move_to_end(uri)
        while len(self.labels) > maxRememberedLabels:
            self.labels.popitem(last=False)

    def rememberRequester(self, uri, name):
        self.requestedBy[uri] = name
        self.requestedBy.move_to_end(uri)
        while len(self.requestedBy) > maxRememberedLabels:
            self.requestedBy.popitem(last=False)

    async def searchTracks(self, query, limit=catalog.searchLimit, userId=None):
        # Searches as the person's own linked account when there is one, so results
        # follow their country and taste, falling back to the bot's login.
        personal = self.accounts.client(userId) if userId else None
        clients = [sp for sp in (personal, self.sp) if sp is not None]
        if not clients:
            raise UserFacingError(
                "Searching by name needs a Spotify login. Use /link to connect your own "
                "Spotify account, free is fine, or paste a Spotify link."
            )
        for sp in clients:
            try:
                return await self.runBlocking(catalog.searchTracks, sp, query, limit)
            except (spotipy.SpotifyException, SpotifyOauthError, requests.RequestException) as err:
                logger.warning("Spotify search failed: %s", err)
        raise UserFacingError("Spotify search failed, try again or paste a Spotify link.")

    async def personalMatches(self, userId, query):
        # Songs from the person's own recently played, top and liked lists whose name
        # contains what they typed, or all of them when nothing is typed yet.
        try:
            tracks = await self.runBlocking(self.accounts.library, userId)
        except (spotipy.SpotifyException, SpotifyOauthError, requests.RequestException):
            logger.exception("Could not read the linked Spotify library of %s", userId)
            return []
        lowered = query.lower()
        return [track for track in tracks if lowered in track["label"].lower()]

    async def resolve(self, query, userId):
        reference = catalog.parseReference(query)
        if reference is not None:
            kind, uri = reference
            return kind, uri, self.labels.get(uri) or "that " + kind

        # Typed without picking a suggestion. Prefer the person's own music, the same
        # order the suggestions list shows.
        personal = await self.personalMatches(userId, query.strip())
        if personal:
            return "track", personal[0]["uri"], personal[0]["label"]
        results = await self.searchTracks(query, 1, userId)
        if not results:
            raise UserFacingError("Nothing on Spotify matched " + query + ".")
        return "track", results[0]["uri"], results[0]["label"]

    # Commands

    @app_commands.command(name="join", description="Bring the Spotify speaker into your voice channel")
    async def join(self, interaction: discord.Interaction):
        # Joining voice works before the speaker is paired, which also makes this a quick
        # check that the bot's host can reach Discord voice at all.
        await self.requireSpeaker(needLinked=False)
        channel = userVoiceChannel(interaction)
        if channel is None:
            raise UserFacingError("Join a voice channel first, then run this again.")
        await interaction.response.defer()
        try:
            await self.connectTo(channel)
        except asyncio.TimeoutError:
            raise UserFacingError(
                "Could not connect to voice within 20 seconds. The usual cause is outbound "
                "UDP to Discord voice being blocked on the bot's host."
            )
        message = "Joined " + channel.name + ". Use /play, or add songs to the shared playlist (see /playlist)."
        if not self.linked:
            message += (
                "\nThe speaker is not paired with a Spotify account yet, so nothing can play "
                "until the Premium account's owner approves the pairing link in the bot's log."
            )
        await self.reply(interaction, message)

    @app_commands.command(name="leave", description="Stop the music and leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        vc = self.voiceClient()
        if vc is None:
            raise UserFacingError("I am not in a voice channel.")
        self.leavingOnPurpose = True
        await vc.disconnect(force=False)
        await self.reply(interaction, self.who(interaction) + " stopped the music and sent me out of voice.")

    @app_commands.command(
        name="play",
        description="Play a song, album or playlist. Songs are queued if something is already playing",
    )
    @app_commands.describe(
        query="A song name, or a Spotify link to a song, album, playlist or artist",
        now="Play it straight away instead of adding it to the queue",
    )
    async def play(self, interaction: discord.Interaction, query: str, now: bool = False):
        await self.requireSpeaker()
        await interaction.response.defer()
        kind, uri, label = await self.resolve(query, interaction.user.id)
        await self.connectForCommand(interaction)
        if kind in ("track", "episode"):
            self.rememberRequester(uri, self.who(interaction))

        if kind in ("track", "episode") and self.playing and not now:
            await self.api.addToQueue(uri)
            self.addToSharedPlaylist(uri)
            await self.reply(interaction, self.who(interaction) + " queued " + label + ".")
            return

        await self.api.play(uri)
        # Albums and playlists are not copied in, they could be hundreds of songs.
        if kind in ("track", "episode"):
            self.addToSharedPlaylist(uri)
        await self.reply(interaction, self.who(interaction) + " started " + label + ".")

    @play.autocomplete("query")
    async def queryAutocomplete(self, interaction: discord.Interaction, current: str):
        current = current.strip()
        if catalog.parseReference(current):
            return []
        try:
            # Discord drops autocomplete answers that take longer than 3 seconds. A slow
            # first library fetch still finishes in the background and fills the cache.
            return await asyncio.wait_for(self.suggest(interaction.user.id, current), timeout=2.5)
        except (asyncio.TimeoutError, UserFacingError):
            return []
        except Exception:
            logger.exception("Building /play suggestions failed")
            return []

    async def suggest(self, userId, current):
        suggestions = await self.personalMatches(userId, current)
        if len(current) >= 2:
            seen = {track["uri"] for track in suggestions}
            results = await self.searchTracks(current, userId=userId)
            suggestions += [result for result in results if result["uri"] not in seen]

        choices = []
        for track in suggestions[:maxChoices]:
            self.rememberLabel(track["uri"], track["label"])
            name = track["label"]
            if track.get("source"):
                name += " · " + track["source"]
            choices.append(app_commands.Choice(name=name[:100], value=track["uri"]))
        return choices

    @app_commands.command(name="pause", description="Pause the music")
    async def pause(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        await self.api.pause()
        await self.reply(interaction, self.who(interaction) + " paused the music.")

    @app_commands.command(name="resume", description="Resume the music")
    async def resume(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        await interaction.response.defer()
        await self.connectForCommand(interaction)
        await self.api.resume()
        await self.reply(interaction, self.who(interaction) + " resumed the music.")

    @app_commands.command(name="skip", description="Skip to the next song")
    async def skip(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        await self.api.next()
        await self.reply(interaction, self.who(interaction) + " skipped the song.")

    @app_commands.command(name="previous", description="Go back to the song that played before this one")
    async def previous(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        label = await self.goBack()
        if label is None:
            await self.reply(interaction, self.who(interaction) + " restarted the song, nothing played before it.")
            return
        await self.reply(interaction, self.who(interaction) + " went back to " + label + ".")

    @app_commands.command(name="seek", description="Jump to a point in the current song")
    @app_commands.describe(position="Where to jump to, like 1:30 or 90")
    async def seek(self, interaction: discord.Interaction, position: str):
        await self.requireSpeaker()
        positionMs = catalog.parsePosition(position)
        if positionMs is None:
            raise UserFacingError("Give the position as seconds or minutes:seconds, like 90 or 1:30.")
        await self.api.seek(positionMs)
        await self.reply(interaction, self.who(interaction) + " jumped to " + catalog.formatMs(positionMs) + ".")

    @app_commands.command(name="volume", description="Show or set the volume")
    @app_commands.describe(level="New volume from 0 to 100, leave out to see the current one")
    async def volume(
        self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100] = None
    ):
        await self.requireSpeaker()
        if level is None:
            current = await self.api.volume()
            percent = round(100 * current.get("value", 0) / max(1, current.get("max", 100)))
            await self.reply(interaction, "Volume is " + str(percent) + "%.", ephemeral=True)
            return
        await self.api.setVolume(level)
        await self.reply(interaction, self.who(interaction) + " set the volume to " + str(level) + "%.")

    @app_commands.command(name="shuffle", description="Turn shuffle on or off")
    async def shuffle(self, interaction: discord.Interaction, enabled: bool):
        await self.requireSpeaker()
        await self.api.setShuffle(enabled)
        await self.reply(
            interaction, self.who(interaction) + " turned shuffle " + ("on." if enabled else "off.")
        )

    @app_commands.command(name="repeat", description="Set repeat mode")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="off", value="off"),
            app_commands.Choice(name="the album or playlist", value="context"),
            app_commands.Choice(name="this song", value="track"),
        ]
    )
    async def repeat(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await self.requireSpeaker()
        await self.api.setRepeatContext(mode.value == "context")
        await self.api.setRepeatTrack(mode.value == "track")
        await self.reply(interaction, self.who(interaction) + " set repeat to " + mode.name + ".")

    @app_commands.command(name="nowplaying", description="Show what is playing")
    async def nowPlaying(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        status = await self.api.status()
        if status is None:
            raise NotLinkedError()
        track = status.get("track")
        if not track:
            raise UserFacingError("Nothing is playing. Use /play, or pick **" + deviceName() + "** in the Spotify app.")

        heading = "Paused" if status.get("paused") else "Now playing"
        embed = self.trackEmbed(track, heading, showPosition=True)
        modes = []
        if status.get("shuffle_context"):
            modes.append("shuffle")
        if status.get("repeat_track"):
            modes.append("repeat song")
        elif status.get("repeat_context"):
            modes.append("repeat")
        footer = "Volume " + str(round(100 * status.get("volume", 0) / max(1, status.get("volume_steps", 100)))) + "%"
        if modes:
            footer += " · " + ", ".join(modes)
        if status.get("context_name"):
            footer += " · from " + status["context_name"]
        embed.set_footer(text=footer)
        await self.reply(interaction, embed=embed)

    @app_commands.command(name="queue", description="Show what is coming up")
    async def queue(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        await interaction.response.defer()
        status = await self.api.status()
        if status is None:
            raise NotLinkedError()
        track = status.get("track")
        lines = []
        if track:
            lines.append(("Paused: " if status.get("paused") else "Now playing: ") + describeTrack(track))
        else:
            lines.append("Nothing is playing.")

        upcoming = await self.api.upcomingTracks()
        if upcoming:
            lines.append("")
            lines.append("Up next:")
            for position, item in enumerate(upcoming[:maxQueueLines], start=1):
                line = str(position) + ". " + webApiTrackLabel(item)
                if item.get("uri") in self.requestedBy:
                    line += " (added by " + self.requestedBy[item["uri"]] + ")"
                lines.append(line)
            if len(upcoming) > maxQueueLines:
                lines.append("...and " + str(len(upcoming) - maxQueueLines) + " more")
        elif status.get("next_track"):
            lines.append("Up next: " + describeTrack(status["next_track"]))
        elif track:
            lines.append("Nothing queued after this.")
        await self.reply(interaction, "\n".join(lines))

    @app_commands.command(name="playlist", description="Add songs from your own Spotify app through the shared playlist")
    @app_commands.describe(play_all="Also play everything in the playlist from the start")
    async def playlist(self, interaction: discord.Interaction, play_all: bool = False):
        playlistId = spotifyAuth.playlistIdFromEnvOrConfig()
        if not playlistId:
            raise UserFacingError("No shared playlist is set up. The bot's owner can create one with python spotifyLogin.py.")
        link = "https://open.spotify.com/playlist/" + playlistId
        howTo = (
            "Add songs to the shared playlist from your own Spotify app, free accounts too, and "
            "I queue them here: " + link + "\n"
            "You need to be a collaborator first. Ask the playlist's owner to send you an "
            "**Invite collaborators** link from the playlist in Spotify."
        )
        if not play_all:
            await self.reply(interaction, howTo)
            return

        await self.requireSpeaker()
        await interaction.response.defer()
        await self.connectForCommand(interaction)
        await self.api.play("spotify:playlist:" + playlistId)
        await self.reply(interaction, self.who(interaction) + " started the whole shared playlist.\n\n" + howTo)

    @app_commands.command(name="speaker", description="How to use the music bot")
    async def speaker(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        status = await self.api.status()
        if status is None:
            raise NotLinkedError()

        vc = self.voiceClient()
        if status.get("track"):
            state = "paused" if status.get("paused") else "playing"
        else:
            state = "idle"
        # Deliberately never shows which account the speaker uses, it belongs to one person.
        embed = discord.Embed(title=status.get("device_name") or deviceName(), colour=spotifyGreen)
        embed.add_field(name="State", value=state)
        embed.add_field(name="Voice channel", value=vc.channel.mention if vc else "not connected")
        embed.add_field(
            name="From your Spotify app",
            value="Add songs to the shared playlist and they are queued here. Run /playlist for the link.",
            inline=False,
        )
        embed.add_field(
            name="From Discord",
            value="/play, /pause, /resume, /skip, /previous, /seek, /volume, /shuffle, /repeat, /queue, /nowplaying",
            inline=False,
        )
        embed.add_field(
            name="Your own account",
            value=(
                "Run /link to connect your own Spotify account, free is fine. /play then suggests "
                "your recently played, top and liked songs, and your name shows when you add "
                "songs to the shared playlist."
            ),
            inline=False,
        )
        await self.reply(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="link", description="Connect your own Spotify account so /play suggests your music")
    async def link(self, interaction: discord.Interaction):
        try:
            url = self.accounts.startLink(interaction.user.id)
            redirectUri = spotifyAuth.readSpotifyEnv()[2]
        except spotifyAuth.SpotifyConfigError:
            raise UserFacingError("Spotify is not configured on the bot, ask its owner to check the .env file.")

        profile = store.loadUserProfile(interaction.user.id)
        intro = ""
        if profile:
            intro = "You are linked as **" + (profile.get("displayName") or "a Spotify account") + "**. Linking again replaces it.\n\n"
        await self.reply(
            interaction,
            intro
            + "1. Press **Log in to Spotify** and approve. Any Spotify account works, Premium is not needed.\n"
            "2. Your browser then opens a page that fails to load. That is expected.\n"
            "3. Copy that page's whole address, press **Paste the address** and paste it in.\n\n"
            "Your account is only read, to suggest your music in /play and to show your name when you "
            "add songs to the shared playlist. Songs still play on the shared speaker.",
            ephemeral=True,
            view=LinkView(self, url, redirectUri),
        )

    @app_commands.command(name="unlink", description="Disconnect your Spotify account from the bot")
    async def unlink(self, interaction: discord.Interaction):
        profile = self.accounts.unlink(interaction.user.id)
        if profile is None:
            raise UserFacingError("You do not have a Spotify account linked.")
        await self.reply(
            interaction,
            "Unlinked **" + (profile.get("displayName") or "your Spotify account") + "**. To also remove "
            "the bot from your Spotify account, visit spotify.com/account/apps.",
            ephemeral=True,
        )

    async def completeLink(self, interaction, pastedUrl):
        await interaction.response.defer(ephemeral=True, thinking=True)
        userId = interaction.user.id
        try:
            displayName = await self.runBlocking(self.accounts.finishLink, userId, pastedUrl)
        except LinkError as err:
            await interaction.followup.send(str(err), ephemeral=True)
            return
        # Warms the library cache so the first /play already has suggestions.
        asyncio.create_task(self.personalMatches(userId, ""))
        await interaction.followup.send(
            "Linked as **" + displayName + "**. /play now suggests your recently played, top and liked songs.",
            ephemeral=True,
        )

    # Leaves when nobody is listening, or when nothing has played for a while

    @tasks.loop(seconds=15)
    async def idleCheck(self):
        vc = self.voiceClient()
        if vc is None or not vc.is_connected():
            self.idleSince = None
            return
        listeners = [member for member in vc.channel.members if not member.bot]
        if listeners and self.playing:
            self.idleSince = None
            return

        nowTime = time.monotonic()
        if self.idleSince is None:
            self.idleSince = nowTime
            return
        if nowTime - self.idleSince < self.idleSeconds:
            return

        self.idleSince = None
        channelName = vc.channel.name
        self.leavingOnPurpose = True
        await vc.disconnect(force=False)
        reason = "nobody was listening" if not listeners else "nothing was playing"
        await self.notify("Left " + channelName + " because " + reason + ". Casting from Spotify will bring me back.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # The one place leaving voice is cleaned up after, whether it was /leave, the idle
        # timer, or someone disconnecting the bot. discord.py already closes its side of
        # an outside disconnect, but the music would keep playing to nobody.
        if self.unloading or self.bot.user is None or member.id != self.bot.user.id:
            return
        if before.channel is None or after.channel is not None:
            return
        if member.guild.id in self.switchingFromGuilds:
            self.switchingFromGuilds.discard(member.guild.id)
            return
        onPurpose = self.leavingOnPurpose
        self.leavingOnPurpose = False
        await self.cleanUpAfterLeaving(before.channel, onPurpose)

    async def cleanUpAfterLeaving(self, channel, onPurpose):
        self.idleSince = None
        wasPlaying = self.playing
        if wasPlaying:
            try:
                await self.api.pause()
            except LibrespotError:
                pass
        await self.deleteNowPlayingMessage()
        self.lastAnnouncedUri = None
        self.controlsPaused = None
        logger.info("Left voice channel %s (%s)", channel.name, "on purpose" if onPurpose else "disconnected by someone")
        if not onPurpose:
            await self.notify(
                "Someone disconnected me from " + channel.name
                + (", so I paused the music." if wasPlaying else ".")
                + " Use /join or play from Spotify to bring me back."
            )

    @idleCheck.before_loop
    async def beforeIdleCheck(self):
        await self.bot.wait_until_ready()


class PlayerControls(discord.ui.View):
    # Buttons under the now playing message. Fixed custom ids make them persistent, so
    # they keep working on messages posted before the bot restarted.
    def __init__(self, cog, paused=False):
        super().__init__(timeout=None)
        self.cog = cog
        self.toggle.label = "Resume" if paused else "Pause"
        self.toggle.emoji = "\u25b6\ufe0f" if paused else "\u23f8\ufe0f"

    @discord.ui.button(label="Previous", emoji="\u23ee\ufe0f", style=discord.ButtonStyle.secondary, custom_id="music:previous")
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.pressControl(interaction, self.cog.goBack)

    @discord.ui.button(label="Restart", emoji="\U0001f504", style=discord.ButtonStyle.secondary, custom_id="music:restart")
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.pressControl(interaction, self.cog.restartSong)

    @discord.ui.button(label="Pause", emoji="\u23f8\ufe0f", style=discord.ButtonStyle.primary, custom_id="music:playpause")
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.pressControl(interaction, self.cog.api.playPause)

    @discord.ui.button(label="Next", emoji="\u23ed\ufe0f", style=discord.ButtonStyle.secondary, custom_id="music:next")
    async def skipNext(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.pressControl(interaction, self.cog.api.next)


class LinkView(discord.ui.View):
    def __init__(self, cog, url, redirectUri):
        super().__init__(timeout=15 * 60)
        self.cog = cog
        self.redirectUri = redirectUri
        # Rebuilt so the login button comes before the decorated paste button.
        paste = self.paste
        self.clear_items()
        self.add_item(discord.ui.Button(label="Log in to Spotify", style=discord.ButtonStyle.link, url=url))
        self.add_item(paste)

    @discord.ui.button(label="Paste the address", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LinkModal(self.cog, self.redirectUri))


class LinkModal(discord.ui.Modal, title="Finish linking Spotify"):
    def __init__(self, cog, redirectUri):
        super().__init__()
        self.cog = cog
        self.address = discord.ui.TextInput(
            label="Address of the page that failed to load",
            placeholder=(redirectUri + "?code=...")[:100],
            max_length=2000,
        )
        self.add_item(self.address)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.completeLink(interaction, self.address.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception("Linking a Spotify account failed", exc_info=error)
        message = "Linking failed: " + str(error)
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
