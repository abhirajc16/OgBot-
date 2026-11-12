"""A small Discord music bot with an interactive player panel."""

import asyncio
import math
import os
import struct
import shutil
import tempfile
import time
from importlib.metadata import PackageNotFoundError, version as package_version
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import imageio_ffmpeg
import yt_dlp

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in your .env file.")
SYNC_GUILD_ID = int(os.getenv("DISCORD_SYNC_GUILD_ID", "1517408751341142148"))


def check_voice_dependency_compatibility() -> str:
    try:
        davey_version = package_version("davey")
    except PackageNotFoundError as exc:
        raise RuntimeError("Install the voice dependencies with: pip install -r requirements.txt") from exc
    davey_major_minor = tuple(int(part) for part in davey_version.split(".")[:2])
    if discord.__version__ == "2.7.1" and davey_major_minor >= (0, 2):
        raise RuntimeError(
            "Incompatible voice dependencies: discord.py 2.7.1 with davey 0.2.0 or newer "
            "can show as speaking while sending inaudible audio. Reinstall requirements.txt "
            "in the project's .venv."
        )
    return davey_version


DAVEY_VERSION = check_voice_dependency_compatibility()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "sleep_interval_requests": 0.2,
    "concurrent_fragment_downloads": 4,
}
FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()
guild_volumes: dict[int, float] = defaultdict(lambda: 1.0)


def load_opus_encoder() -> None:
    """Explicitly load discord.py's bundled Opus DLL before voice playback."""
    if discord.opus.is_loaded():
        return
    if os.name == "nt":
        architecture = "x64" if struct.calcsize("P") * 8 == 64 else "x86"
        opus_path = Path(discord.__file__).parent / "bin" / f"libopus-0.{architecture}.dll"
        if opus_path.is_file():
            discord.opus.load_opus(str(opus_path))
    if not discord.opus.is_loaded():
        raise RuntimeError("discord.py could not load its Opus encoder DLL.")


load_opus_encoder()


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: Optional[int]
    requester: str
    thumbnail: Optional[str] = None
    temp_dir: Optional[str] = None

    @property
    def duration_text(self) -> str:
        if self.duration is None:
            return "Live"
        minutes, seconds = divmod(self.duration, 60)
        return f"{minutes}:{seconds:02d}"


queues: dict[int, Deque[Track]] = defaultdict(deque)
current: dict[int, Optional[Track]] = defaultdict(lambda: None)
playback_started_at: dict[int, float] = {}
panel_messages: dict[int, discord.Message] = {}
panel_refresh_tasks: dict[int, asyncio.Task] = {}
guild_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
playback_errors: dict[int, str] = {}


async def resolve_track(query: str, requester: str) -> Track:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _extract_track, query)
    return Track(
        title=data.get("title", "Unknown title"),
        webpage_url=data.get("webpage_url") or data.get("original_url") or query,
        stream_url=data["local_path"],
        duration=data.get("duration"),
        requester=requester,
        thumbnail=data.get("thumbnail"),
        temp_dir=data.get("temp_dir"),
    )


def _extract_track(query: str) -> dict:
    temp_dir = tempfile.mkdtemp(prefix="discord-music-")
    options = {
        **YTDL_OPTIONS,
        "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = {}
            try:
                # Extract and download in one pass; selected autocomplete results are URLs,
                # so they do not need a separate search or metadata request first.
                downloaded = ydl.extract_info(query, download=True)
            except yt_dlp.utils.DownloadError as exc:
                if "403" not in str(exc):
                    raise
                info = ydl.extract_info(query, download=False)
                if info and "entries" in info:
                    info = next((entry for entry in info["entries"] if entry), None)
                if not info:
                    raise ValueError("No playable result was found.")
                if "youtube" not in (info.get("extractor_key") or "").lower():
                    raise exc
                # YouTube can temporarily block video downloads from a network IP.
                # Try the same title on SoundCloud so the player has another source.
                shutil.rmtree(temp_dir, ignore_errors=True)
                temp_dir = tempfile.mkdtemp(prefix="discord-music-")
                options["outtmpl"] = os.path.join(temp_dir, "%(id)s.%(ext)s")
                with yt_dlp.YoutubeDL(options) as fallback_ydl:
                    downloaded = fallback_ydl.extract_info(
                        f"scsearch1:{info.get('title') or query}", download=True
                    )
            if "entries" in downloaded:
                downloaded = next((entry for entry in downloaded["entries"] if entry), None)
            if not downloaded:
                raise ValueError("Could not download the selected track.")

            source_url = downloaded.get("webpage_url") or downloaded.get("original_url") or query
            local_path = downloaded.get("_filename") or ydl.prepare_filename(downloaded)
            if not os.path.isfile(local_path):
                candidates = [
                    os.path.join(temp_dir, name)
                    for name in os.listdir(temp_dir)
                    if os.path.isfile(os.path.join(temp_dir, name))
                ]
                if not candidates:
                    raise ValueError("yt-dlp finished without creating an audio file.")
                local_path = candidates[0]

            return {
                **downloaded,
                "webpage_url": downloaded.get("webpage_url") or info.get("webpage_url") or source_url,
                "local_path": local_path,
                "temp_dir": temp_dir,
            }
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _search_tracks(query: str) -> list[dict]:
    """Return YouTube video titles quickly for Discord autocomplete."""
    options = {
        "quiet": True,
        "noplaylist": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 1.5,
        "retries": 0,
        "extractor_retries": 0,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        return [entry for entry in (info or {}).get("entries", []) if entry]


def player_embed(guild_id: int) -> discord.Embed:
    track = current[guild_id]
    embed = discord.Embed(
        title="🎶 Now Playing" if track else "🎶 Music Player",
        color=discord.Color.from_rgb(119, 92, 230),
        description=(f"[{discord.utils.escape_markdown(track.title)}]({track.webpage_url})" if track
                     else "Nothing is playing. Use **/play** to add a song."),
    )
    if track:
        if track.duration and guild_id in playback_started_at:
            elapsed = min(int(time.monotonic() - playback_started_at[guild_id]), track.duration)
            filled = round(20 * elapsed / track.duration)
            bar = "▬" * filled + "🔘" + "▬" * (20 - filled)
            elapsed_text = f"{elapsed // 60}:{elapsed % 60:02d}"
            embed.add_field(name="Progress", value=f"{bar}\n`{elapsed_text} / {track.duration_text}`", inline=False)
        else:
            embed.add_field(name="Progress", value="`LIVE`", inline=False)
        embed.add_field(name="Requested by", value=track.requester, inline=True)
        if track.thumbnail:
            embed.set_image(url=track.thumbnail)
    if guild_id in playback_errors:
        embed.add_field(
            name="Playback issue",
            value=f"{playback_errors[guild_id]} Check the bot console for FFmpeg details.",
            inline=False,
        )
    upcoming = list(queues[guild_id])[:5]
    if upcoming:
        lines = [f"`{i}.` [{discord.utils.escape_markdown(t.title)}]({t.webpage_url}) · `{t.duration_text}`"
                 for i, t in enumerate(upcoming, 1)]
        more = len(queues[guild_id]) - len(upcoming)
        if more:
            lines.append(f"…and {more} more")
        embed.add_field(name=f"Up Next · {len(queues[guild_id])} track(s)", value="\n".join(lines), inline=False)
    embed.set_footer(text="Music controls · Use /help for commands")
    return embed


async def refresh_panel(guild_id: int) -> None:
    message = panel_messages.get(guild_id)
    if message:
        try:
            await message.edit(embed=player_embed(guild_id), view=PlayerControls(guild_id))
        except (discord.NotFound, discord.Forbidden):
            panel_messages.pop(guild_id, None)


async def refresh_panel_progress(guild_id: int) -> None:
    """Refresh the progress bar once per second without rebuilding its controls."""
    try:
        while current[guild_id] is not None:
            await asyncio.sleep(1)
            guild = bot.get_guild(guild_id)
            voice = guild.voice_client if guild else None
            message = panel_messages.get(guild_id)
            if not voice or not voice.is_playing() or not message:
                continue
            try:
                await message.edit(embed=player_embed(guild_id))
            except discord.NotFound:
                panel_messages.pop(guild_id, None)
                return
            except discord.Forbidden:
                panel_messages.pop(guild_id, None)
                return
    finally:
        panel_refresh_tasks.pop(guild_id, None)


async def start_next(guild: discord.Guild) -> None:
    async with guild_locks[guild.id]:
        voice = guild.voice_client
        if not voice or voice.is_playing() or voice.is_paused():
            return
        if not queues[guild.id]:
            current[guild.id] = None
            await refresh_panel(guild.id)
            return
        track = queues[guild.id].popleft()
        current[guild.id] = track
        playback_started_at[guild.id] = time.monotonic()
        playback_errors.pop(guild.id, None)
        ffmpeg_log = tempfile.TemporaryFile(mode="w+b")
        ffmpeg_audio = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=FFMPEG_EXECUTABLE,
            options="-vn",
            stderr=ffmpeg_log,
        )
        source = MeteredVolumeTransformer(ffmpeg_audio, volume=guild_volumes[guild.id])

        def after_play(error: Optional[Exception]) -> None:
            process = getattr(ffmpeg_audio, "_process", None)
            return_code = None
            if process is not None:
                try:
                    return_code = process.wait(timeout=3)
                except Exception:
                    return_code = process.poll()
            ffmpeg_log.seek(0)
            ffmpeg_details = ffmpeg_log.read().decode("utf-8", errors="replace").strip()
            ffmpeg_log.close()
            if error or return_code not in (None, 0) or ffmpeg_details:
                print(
                    f"FFmpeg playback diagnostics (guild {guild.id}, exit {return_code}, "
                    f"PCM frames {source.frames_read}, non-silent {source.non_silent_frames}):\n"
                    f"{ffmpeg_details or 'FFmpeg did not write diagnostic output.'}"
                )
            else:
                print(
                    f"Playback ended in guild {guild.id}: FFmpeg exit {return_code}, "
                    f"PCM frames {source.frames_read}, non-silent {source.non_silent_frames}."
                )
            asyncio.run_coroutine_threadsafe(
                handle_track_end(
                    guild,
                    track,
                    error,
                    ffmpeg_details,
                    return_code,
                    source.frames_read,
                    source.non_silent_frames,
                ),
                bot.loop,
            )

        voice.play(source, after=after_play)
        task = panel_refresh_tasks.get(guild.id)
        if task is None or task.done():
            panel_refresh_tasks[guild.id] = asyncio.create_task(refresh_panel_progress(guild.id))
    await refresh_panel(guild.id)


class MeteredVolumeTransformer(discord.PCMVolumeTransformer):
    """Measure decoded PCM before discord.py encodes it with the loaded Opus DLL."""

    def __init__(self, original: discord.AudioSource, volume: float = 1.0):
        super().__init__(original, volume=volume)
        self.frames_read = 0
        self.non_silent_frames = 0

    def read(self) -> bytes:
        data = super().read()
        if data:
            self.frames_read += 1
            if any(data):
                self.non_silent_frames += 1
        return data


class ToneSource(discord.AudioSource):
    """Generate a short 440 Hz stereo PCM tone for voice diagnostics."""

    def __init__(self, seconds: int = 3):
        self.sample_index = 0
        self.total_samples = 48_000 * seconds

    def read(self) -> bytes:
        if self.sample_index >= self.total_samples:
            return b""
        count = min(960, self.total_samples - self.sample_index)
        frame = bytearray(count * 4)
        for index in range(count):
            sample = int(10_000 * math.sin(2 * math.pi * 440 * self.sample_index / 48_000))
            struct.pack_into("<hh", frame, index * 4, sample, sample)
            self.sample_index += 1
        return bytes(frame)

    def is_opus(self) -> bool:
        return False


async def handle_track_end(
    guild: discord.Guild,
    finished_track: Track,
    error: Optional[Exception],
    ffmpeg_details: str = "",
    return_code: Optional[int] = None,
    frames_read: int = 0,
    non_silent_frames: int = 0,
) -> None:
    if finished_track.temp_dir:
        shutil.rmtree(finished_track.temp_dir, ignore_errors=True)
    failed = error is not None or (return_code not in (None, 0))
    if failed:
        details = "\n".join(
            part for part in (
                str(error).strip() if error else "",
                f"FFmpeg exit code: {return_code}" if return_code not in (None, 0) else "",
                ffmpeg_details,
            ) if part
        ) or repr(error)
        print(f"Playback error in guild {guild.id}: {details}")
        status = f"FFmpeg exited with code {return_code}." if return_code not in (None, 0) else "Audio stream failed."
        if ffmpeg_details:
            status += f"\n{ffmpeg_details[-700:]}"
        playback_errors[guild.id] = status
        await refresh_panel(guild.id)
    elif frames_read == 0:
        playback_errors[guild.id] = "FFmpeg produced no audio frames."
        await refresh_panel(guild.id)
    elif non_silent_frames == 0:
        playback_errors[guild.id] = "FFmpeg produced only silent audio frames."
        await refresh_panel(guild.id)
    else:
        playback_errors.pop(guild.id, None)
    await start_next(guild)


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        raise app_commands.AppCommandError("Use this command in a server.")
    channel = interaction.user.voice.channel if interaction.user.voice else None
    if channel is None:
        raise app_commands.AppCommandError("Join a voice channel first.")
    voice = interaction.guild.voice_client
    if voice is None:
        voice = await channel.connect()
    elif voice.channel != channel:
        await voice.move_to(channel)
    return voice


class PlayerControls(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This player belongs to another server.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Pause / Resume", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice = interaction.guild.voice_client
        if not voice:
            await interaction.response.send_message("I’m not in a voice channel.", ephemeral=True)
        elif voice.is_paused():
            voice.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        elif voice.is_playing():
            voice.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        voice = interaction.guild.voice_client
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
            await interaction.response.send_message("Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        queues[self.guild_id].clear()
        voice = interaction.guild.voice_client
        if voice:
            voice.stop()
            await voice.disconnect()
        current[self.guild_id] = None
        await interaction.response.edit_message(embed=player_embed(self.guild_id), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, custom_id="music:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=player_embed(self.guild_id), view=self)


@bot.event
async def on_ready() -> None:
    test_guild = discord.Object(id=SYNC_GUILD_ID)
    bot.tree.clear_commands(guild=test_guild)
    await bot.tree.sync(guild=test_guild)
    global_commands = await bot.tree.sync()
    print(
        f"Ready as {bot.user}; synced {len(global_commands)} global commands and "
        f"cleared server-specific command copies for guild {SYNC_GUILD_ID}."
    )
    print(
        f"Voice output: PCM encoded with loaded libopus; discord.py {discord.__version__}; "
        f"davey {DAVEY_VERSION}; local Opus encoder loaded: {discord.opus.is_loaded()}."
    )


@bot.tree.command(name="play", description="Play a song or add it to the queue")
@app_commands.describe(query="A song title or supported audio URL")
async def play(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer(thinking=True)
    try:
        voice = await ensure_voice(interaction)
        track = await resolve_track(query, interaction.user.display_name)
    except Exception as exc:
        error_text = str(exc)
        if "HTTP Error 403" in error_text:
            message = (
                "YouTube rejected the download with HTTP 403. Update yt-dlp in this bot's "
                "Python environment with `python -m pip install --upgrade \"yt-dlp[default]\"`, "
                "then restart the bot. If the latest yt-dlp still gets 403, pause requests and "
                "try again later; YouTube may be temporarily restricting downloads from your network."
            )
        else:
            message = f"Could not add that track: {error_text}"
        await interaction.followup.send(message, ephemeral=True)
        return
    gid = interaction.guild_id
    if current[gid] is None and not voice.is_playing() and not voice.is_paused():
        queues[gid].appendleft(track)
        await start_next(interaction.guild)
    else:
        queues[gid].append(track)
    message = await interaction.followup.send(embed=player_embed(gid), view=PlayerControls(gid), wait=True)
    # Keep the most recent player panel available for queue updates.
    panel_messages[gid] = message


@play.autocomplete("query")
async def play_autocomplete(
    interaction: discord.Interaction, current_text: str
) -> list[app_commands.Choice[str]]:
    """Offer YouTube video titles without exceeding Discord's autocomplete deadline."""
    text = current_text.strip()
    if len(text) < 2:
        return []
    try:
        loop = asyncio.get_running_loop()
        results = await asyncio.wait_for(
            loop.run_in_executor(None, _search_tracks, text), timeout=1.3
        )
    except Exception:
        return []

    choices = []
    for result in results:
        title = result.get("title")
        video_id = result.get("id")
        url = result.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={video_id}" if video_id else None
        )
        if title and url:
            choices.append(app_commands.Choice(name=title[:100], value=url[:100]))
    return choices[:25]


@bot.tree.command(name="queue", description="Show the current song and upcoming tracks")
async def queue(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=player_embed(interaction.guild_id), view=PlayerControls(interaction.guild_id))
    panel_messages[interaction.guild_id] = await interaction.original_response()


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction) -> None:
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and (voice.is_playing() or voice.is_paused()):
        voice.stop()
        await interaction.response.send_message("Skipped the current track.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction: discord.Interaction) -> None:
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_playing():
        voice.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction: discord.Interaction) -> None:
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and voice.is_paused():
        voice.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Playback is not paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Clear the queue and leave voice")
async def stop(interaction: discord.Interaction) -> None:
    gid = interaction.guild_id
    queues[gid].clear()
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice:
        voice.stop()
        await voice.disconnect()
    current[gid] = None
    await interaction.response.send_message("Queue cleared; leaving voice.")
    await refresh_panel(gid)


@bot.tree.command(name="volume", description="Set playback volume from 0 to 100")
@app_commands.describe(level="Volume percentage (0–100)")
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("Use this command in a server.", ephemeral=True)
        return
    guild_volumes[interaction.guild_id] = level / 100
    voice = interaction.guild.voice_client if interaction.guild else None
    if voice and isinstance(voice.source, MeteredVolumeTransformer):
        voice.source.volume = level / 100
        await interaction.response.send_message(f"Volume set to {level}%.")
    else:
        await interaction.response.send_message(f"Volume set to {level}% for the next track.")


@bot.tree.command(name="help", description="Show music bot commands")
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="🎵 Music Bot", color=discord.Color.from_rgb(119, 92, 230))
    embed.description = "`/play` add a song · `/queue` view player · `/pause` · `/resume` · `/skip` · `/stop` · `/volume` · `/testaudio`"
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="testaudio", description="Play a short tone to check voice audio")
async def test_audio(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        voice = await ensure_voice(interaction)
    except Exception as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    if voice.is_playing() or voice.is_paused():
        await interaction.followup.send(
            "Stop the current playback first, then run `/testaudio` again.", ephemeral=True
        )
        return

    tone_source = MeteredVolumeTransformer(ToneSource(), volume=1.0)

    def after_tone(error: Optional[Exception]) -> None:
        if error:
            print(
                f"Test tone error in guild {interaction.guild_id}: {error!r}; "
                f"PCM frames: {tone_source.frames_read}; non-silent: {tone_source.non_silent_frames}."
            )
        else:
            print(
                f"Test tone finished in guild {interaction.guild_id}; "
                f"PCM frames: {tone_source.frames_read}; non-silent: {tone_source.non_silent_frames}."
            )

    voice.play(tone_source, after=after_tone)
    bot_member = interaction.guild.me
    can_speak = voice.channel.permissions_for(bot_member).speak if bot_member else False
    server_muted = bot_member.voice.mute if bot_member and bot_member.voice else "unknown"
    await interaction.followup.send(
        f"Playing a 3-second test tone. Connected: {voice.is_connected()}; "
        f"Speak permission: {can_speak}; server-muted: {server_muted}; "
        f"local Opus encoder loaded: {discord.opus.is_loaded()}.",
        ephemeral=True,
    )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    message = str(error)
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


bot.run(TOKEN)
