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
    await bot.tree.sync()
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.tree.command(name="gif", description="Send a random gif")
async def gif(interaction: discord.Interaction):
    gifs = [f for f in GIF_DIR.iterdir() if f.is_file() and f.suffix.lower() in GIF_EXTENSIONS]

    if not gifs:
        await interaction.response.send_message("No gifs found in the gifs folder.", ephemeral=True)
        return

    choice = random.choice(gifs)
    await interaction.response.send_message(file=discord.File(choice))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Add it to your .env file.")
    bot.run(TOKEN)
