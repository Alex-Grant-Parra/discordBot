import fcntl
import logging
import os
import random
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GIF_DIR = Path(os.getenv("GIF_DIR", "gifs"))
GIF_EXTENSIONS = {".gif"}

intents = discord.Intents.default()
# Every command is a slash command, so no message prefix is needed and the
# privileged message content intent stays off. when_mentioned is exempt from
# discord.py's missing intent warning.
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
commandsSynced = False


# Loaded as an extension so the music feature stays out of this file. A failure to
# load is logged rather than fatal, so the gif command keeps working regardless.
@bot.event
async def setup_hook():
    try:
        await bot.load_extension("music.cog")
    except Exception:
        logging.getLogger("music").exception("Failed to load the music cog")


@bot.event
async def on_ready():
    # Global commands can take up to an hour to appear. Setting DISCORD_GUILD_ID syncs
    # to that one server instead, which shows up immediately and is what you want while
    # developing. Leave it unset to publish globally.
    print(f"Logged in as {bot.user} ({bot.user.id})")

    # on_ready fires again after every reconnect, syncing once per start is enough.
    global commandsSynced
    if commandsSynced:
        return
    commandsSynced = True

    guildId = os.getenv("DISCORD_GUILD_ID", "").strip()
    if guildId:
        guild = discord.Object(id=int(guildId))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        # Global copies left over from before the guild id was set would show every
        # command twice, and keep commands that no longer exist, so they are cleared.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print(f"Synced commands to guild {guildId} and cleared global commands")
    else:
        await bot.tree.sync()


@bot.tree.command(name="gif", description="Send a random gif")
async def gif(interaction: discord.Interaction):
    gifs = [f for f in GIF_DIR.iterdir() if f.is_file() and f.suffix.lower() in GIF_EXTENSIONS]

    if not gifs:
        await interaction.response.send_message("No gifs found in the gifs folder.", ephemeral=True)
        return

    choice = random.choice(gifs)
    await interaction.response.send_message(file=discord.File(choice))


def holdSingleInstanceLock():
    # Two copies would share one Spotify speaker and audio pipe and both answer every
    # command. The lock is released by the kernel when the process exits, however it exits.
    lockFile = open(Path(__file__).with_name(".bot.lock"), "w")
    try:
        fcntl.flock(lockFile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            "Another copy of the bot is already running, usually the discordbot systemd "
            "service. Restart that with: sudo systemctl restart discordbot"
        )
    return lockFile


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    instanceLock = holdSingleInstanceLock()
    # root_logger makes the music feature's own log lines show up next to discord.py's.
    bot.run(TOKEN, root_logger=True)
