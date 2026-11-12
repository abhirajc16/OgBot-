# Python Discord Music Bot

A small Python music bot with slash commands and an interactive Discord player panel. The panel shows the current song, requester, duration, and upcoming queue, with pause/resume, skip, stop, and refresh buttons.

## Features

- `/play <query>` shows YouTube video titles as you type. Choose one, or enter a search phrase or supported audio URL, to download the track and then join your voice channel.
- `/queue` shows the player and upcoming tracks.
- `/pause`, `/resume`, `/skip`, `/stop`, `/volume`, `/testaudio`, and `/help` provide quick controls. `/volume` sets the level for the next track; use Discord's user volume slider to change a track already playing.
- Each server has its own in-memory queue.

## Setup

1. Install Python 3.14 and Deno 2.3 or newer. Deno is a standalone JavaScript runtime used by yt-dlp for YouTube extraction. FFmpeg is installed as a Python dependency below, so a separate system FFmpeg install is not needed.
2. Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Enable the bot and copy its token.
3. Invite it to your server with the `bot` and `applications.commands` scopes and permissions to view/send messages, embed links, connect, and speak.
4. In this folder, create `.env` from `.env.example` and put the token in `DISCORD_TOKEN`.
5. Install dependencies and run:

   ```powershell
   py -3.14 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   Copy-Item .env.example .env
   # Edit .env, then:
   python bot.py
   ```

To update the bot and all voice dependencies in its virtual environment, run:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt
```

The dependency pin keeps `davey` below 0.2 for compatibility with discord.py 2.7.1. With `davey` 0.2.0, the bot can show as speaking while Discord listeners hear silence. Python 3.14 has a prebuilt Windows wheel for the pinned `davey` version.

After updating dependencies, stop any running bot and launch it with `\.venv\Scripts\python.exe bot.py`. The startup log prints the active `davey` version; this project environment uses `0.1.7`. Then run `/testaudio` once in a voice channel.

The bot explicitly loads discord.py's bundled Opus DLL at startup, then encodes FFmpeg PCM for Discord voice output.

Slash commands sync when the bot starts. Discord can take a little time to show globally synced commands. For instant development updates, sync to a test guild.

Commands sync globally at startup. Global command updates can take a little while to appear in Discord.

If YouTube prints `No supported JavaScript runtime could be found`, install Deno from [deno.com](https://deno.com/) and restart the bot. The `yt-dlp[default]` dependency includes yt-dlp's YouTube challenge scripts; Deno supplies the runtime that executes them.

If YouTube returns `HTTP Error 403`, update yt-dlp in the same Python environment used to run the bot:

```powershell
python -m pip install --upgrade "yt-dlp[default]"
```

Restart the bot after upgrading. If the current yt-dlp release still gets 403, stop retrying for a while and try later; YouTube can temporarily restrict downloads from a network IP. This bot waits one second between yt-dlp requests to reduce request bursts.

## Notes

- Tracks are downloaded to a temporary folder before playback, then removed afterward. If YouTube returns HTTP 403, the bot tries to find the same title on SoundCloud. Queue state is in memory and resets when the process restarts.
- Audio extraction depends on the source website and its current policies; yt-dlp may need updating if a site changes.
- Keep the bot token private. Never commit `.env`.
