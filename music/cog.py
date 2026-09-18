# Music cog. The bot is a Spotify Connect speaker: go-librespot plays the audio and the
# bot relays it into a voice channel. People control it from the Spotify app, where it
# shows up as a device for anyone logged into the linked account, or with the slash
# commands here. Both drive the same player, so they always agree.

import asyncio
import logging
import os
import time
from collections import OrderedDict

import aiohttp
import discord
import spotipy
from discord import app_commands
from discord.ext import commands, tasks

from . import catalog, spotifyAuth, store
from .audioBridge import AudioBridge, BridgeSource
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


class UserFacingError(Exception):
    # Raised by commands with a message meant to be shown to the person as is.
    pass


def readIdleSeconds():
    try:
        value = int(os.getenv("MUSIC_IDLE_TIMEOUT", str(defaultIdleSeconds)))
    except ValueError:
        return defaultIdleSeconds
    return max(30, value)


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
        self.warnedNoChannel = False
        self.voiceLock = asyncio.Lock()
        self.idleSince = None
        self.idleSeconds = readIdleSeconds()
        self.backgroundTasks = []
        # Autocomplete hands back only a URI, this keeps the matching song name so the
        # reply can say what was played.
        self.labels = OrderedDict()

    async def cog_load(self):
        store.initDb()
        await self.api.open()
        self.sp = await self.loadWebApi()

        if not binaryPath.exists():
            logger.error(
                "go-librespot is not installed, so the Spotify speaker is off. "
                "Stop the bot and run: python speakerSetup.py"
            )
            return

        self.bridge.start()
        self.process.start()
        self.speakerStarted = True
        self.backgroundTasks = [
            asyncio.create_task(self.consumeEvents()),
            asyncio.create_task(self.watchLogin()),
        ]
        self.idleCheck.start()

    async def cog_unload(self):
        self.idleCheck.cancel()
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
        elif kind in ("paused", "stopped", "inactive"):
            # Pausing the voice client stops the bot transmitting silence. What little
            # audio is still in flight stays buffered and plays first on resume.
            self.playing = False
            vc = self.voiceClient()
            if vc is not None and vc.is_playing():
                vc.pause()
        elif kind == "metadata":
            await self.announceTrack(data)

    async def announceTrack(self, track):
        uri = track.get("uri")
        if not uri or uri == self.lastAnnouncedUri:
            return
        self.lastAnnouncedUri = uri
        await self.notify(embed=self.trackEmbed(track, "Now playing"))

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
                    await vc.disconnect(force=True)
                vc = await channel.connect(timeout=20.0, reconnect=True, self_deaf=True)

            store.setConfig("voiceChannelId", channel.id)
            self.warnedNoChannel = False
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

    async def notify(self, message=None, embed=None):
        channelId = envChannelId("MUSIC_TEXT_CHANNEL_ID")
        if not channelId:
            if message:
                logger.info("No text channel configured, message not posted: %s", message)
            return
        channel = self.bot.get_channel(channelId)
        if channel is None:
            logger.warning("Text channel %d is not visible to the bot", channelId)
            return
        try:
            await channel.send(content=message, embed=embed)
        except discord.HTTPException:
            logger.exception("Failed to post a message to channel %d", channelId)

    async def reply(self, interaction, message=None, embed=None, ephemeral=False):
        kwargs = {"ephemeral": ephemeral}
        if message is not None:
            kwargs["content"] = message
        if embed is not None:
            kwargs["embed"] = embed
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def cog_app_command_error(self, interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, UserFacingError):
            message = str(original)
        elif isinstance(original, NotLinkedError):
            message = (
                "No Spotify account is linked to the speaker yet. The bot's owner needs to "
                "run python speakerSetup.py."
            )
        elif isinstance(original, LibrespotError):
            logger.warning("Speaker command failed: %s", original)
            message = "The Spotify speaker could not do that: " + str(original)
        else:
            logger.exception("Music command failed", exc_info=original)
            message = "Something went wrong: " + str(original)
        try:
            await self.reply(interaction, message, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Could not report a command error")

    async def requireSpeaker(self):
        if not self.speakerStarted:
            raise UserFacingError(
                "The Spotify speaker is not installed. The bot's owner needs to run "
                "python speakerSetup.py and restart the bot."
            )
        if self.linked:
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

    async def searchTracks(self, query, limit=catalog.searchLimit):
        try:
            return await self.runBlocking(catalog.searchTracks, self.sp, query, limit)
        except spotipy.SpotifyException as err:
            logger.warning("Spotify search failed: %s", err)
            raise UserFacingError("Spotify search failed, try again or paste a Spotify link.")

    async def resolve(self, query):
        reference = catalog.parseReference(query)
        if reference is not None:
            kind, uri = reference
            return kind, uri, self.labels.get(uri) or "that " + kind

        if self.sp is None:
            raise UserFacingError(
                "Searching by name needs the Spotify Web API login (python spotifyLogin.py). "
                "Paste a Spotify link instead."
            )
        results = await self.searchTracks(query, 1)
        if not results:
            raise UserFacingError("Nothing on Spotify matched " + query + ".")
        return "track", results[0]["uri"], results[0]["label"]

    # Commands

    @app_commands.command(name="join", description="Bring the Spotify speaker into your voice channel")
    async def join(self, interaction: discord.Interaction):
        await self.requireSpeaker()
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
        await self.reply(
            interaction,
            "Joined " + channel.name + ". Pick **" + deviceName() + "** as the device in "
            "the Spotify app, or use /play.",
        )

    @app_commands.command(name="leave", description="Stop the music and leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        vc = self.voiceClient()
        if vc is None:
            raise UserFacingError("I am not in a voice channel.")
        if self.playing:
            try:
                await self.api.pause()
            except LibrespotError:
                pass
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
        kind, uri, label = await self.resolve(query)
        await self.connectForCommand(interaction)

        if kind in ("track", "episode") and self.playing and not now:
            await self.api.addToQueue(uri)
            await self.reply(interaction, self.who(interaction) + " queued " + label + ".")
            return

        await self.api.play(uri)
        await self.reply(interaction, self.who(interaction) + " started " + label + ".")

    @play.autocomplete("query")
    async def queryAutocomplete(self, interaction: discord.Interaction, current: str):
        current = current.strip()
        if len(current) < 2 or self.sp is None or catalog.parseReference(current):
            return []
        try:
            # Discord drops autocomplete answers that take longer than 3 seconds.
            results = await asyncio.wait_for(self.searchTracks(current), timeout=2.5)
        except (asyncio.TimeoutError, UserFacingError):
            return []
        choices = []
        for result in results:
            self.rememberLabel(result["uri"], result["label"])
            choices.append(app_commands.Choice(name=result["label"][:100], value=result["uri"]))
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

    @app_commands.command(name="previous", description="Go back to the previous song, or the start of this one")
    async def previous(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        await self.api.prev()
        await self.reply(interaction, self.who(interaction) + " went back a song.")

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
                lines.append(str(position) + ". " + webApiTrackLabel(item))
            if len(upcoming) > maxQueueLines:
                lines.append("...and " + str(len(upcoming) - maxQueueLines) + " more")
        elif status.get("next_track"):
            lines.append("Up next: " + describeTrack(status["next_track"]))
        elif track:
            lines.append("Nothing queued after this.")
        await self.reply(interaction, "\n".join(lines))

    @app_commands.command(name="playlist", description="Play the shared Spotify playlist")
    async def playlist(self, interaction: discord.Interaction):
        await self.requireSpeaker()
        playlistId = spotifyAuth.playlistIdFromEnvOrConfig()
        if not playlistId:
            raise UserFacingError("No shared playlist is set up. The bot's owner can create one with python spotifyLogin.py.")
        await interaction.response.defer()
        await self.connectForCommand(interaction)
        await self.api.play("spotify:playlist:" + playlistId)
        await self.reply(
            interaction,
            self.who(interaction) + " started the shared playlist: https://open.spotify.com/playlist/" + playlistId,
        )

    @app_commands.command(name="speaker", description="How to control the music from the Spotify app")
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
        embed = discord.Embed(title=status.get("device_name") or deviceName(), colour=spotifyGreen)
        embed.add_field(name="Spotify account", value=status.get("username") or "unknown")
        embed.add_field(name="State", value=state)
        embed.add_field(name="Voice channel", value=vc.channel.mention if vc else "not connected")
        embed.add_field(
            name="Controlling it from Spotify",
            value=(
                "1. Log in to the Spotify app with the account above.\n"
                "2. Tap the devices icon and pick **" + (status.get("device_name") or deviceName()) + "**.\n"
                "3. Play, pause, skip, change volume and add to queue as normal. "
                "Everyone logged in to that account can control it at the same time."
            ),
            inline=False,
        )
        await self.reply(interaction, embed=embed, ephemeral=True)

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
        if self.playing:
            try:
                await self.api.pause()
            except LibrespotError:
                pass
        channelName = vc.channel.name
        await vc.disconnect(force=False)
        reason = "nobody was listening" if not listeners else "nothing was playing"
        await self.notify("Left " + channelName + " because " + reason + ". Casting from Spotify will bring me back.")

    @idleCheck.before_loop
    async def beforeIdleCheck(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(MusicCog(bot))
