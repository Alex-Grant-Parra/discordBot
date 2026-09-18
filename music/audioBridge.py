# Carries go-librespot's audio into a Discord voice channel.
#
# go-librespot writes 44.1 kHz s16le stereo into a named pipe as fast as it is read and
# keeps no clock of its own, so whoever reads the pipe sets the playback speed. This
# reader runs at exactly real time whether or not the bot is in a voice channel, so the
# position shown in the Spotify app stays correct, resamples to the 48 kHz Discord needs
# through ffmpeg, and hands out 20 ms frames.

import fcntl
import logging
import os
import stat
import subprocess
import threading
import time
from collections import deque

import discord

logger = logging.getLogger("music.audio")

frameBytes = 3840  # 20 ms of 48 kHz s16le stereo, the unit discord.py reads
frameSeconds = 0.02
maxBufferedFrames = 25
prebufferFrames = 3
silenceFrame = bytes(frameBytes)

# Every pipe between go-librespot and Discord holds audio that has already left the
# player, so large pipes mean pause and skip take longer to be heard.
pipeBytes = 16384

# After a gap this long the clock restarts instead of rushing to catch up.
stallSeconds = 0.1

ffmpegCommand = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-fflags", "nobuffer",
    "-probesize", "32",
    "-analyzeduration", "0",
    "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
    "-f", "s16le", "-ar", "48000", "-ac", "2",
    "-flush_packets", "1",
    "pipe:1",
]


def shrinkPipe(fd):
    try:
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, pipeBytes)
    except OSError:
        pass


def writeAll(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def readExactly(fd, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class AudioBridge:
    def __init__(self, fifoPath):
        self.fifoPath = fifoPath
        self.frames = deque(maxlen=maxBufferedFrames)
        self.framesLock = threading.Lock()
        self.starved = True
        self.stopped = threading.Event()
        self.ffmpeg = None
        self.fifoFd = None

    def start(self):
        self.fifoPath.parent.mkdir(parents=True, exist_ok=True)
        if self.fifoPath.exists() and not stat.S_ISFIFO(self.fifoPath.stat().st_mode):
            self.fifoPath.unlink()
        if not self.fifoPath.exists():
            os.mkfifo(self.fifoPath)

        # Opened read write so the pipe always has a reader, go-librespot refuses to start
        # playback otherwise, and so its reopening the pipe between tracks never looks
        # like end of file here.
        self.fifoFd = os.open(self.fifoPath, os.O_RDWR)
        shrinkPipe(self.fifoFd)

        self.startFfmpeg()
        threading.Thread(target=self.feedLoop, name="spotify-feed", daemon=True).start()

    def startFfmpeg(self):
        proc = subprocess.Popen(
            ffmpegCommand,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        shrinkPipe(proc.stdin.fileno())
        shrinkPipe(proc.stdout.fileno())
        self.ffmpeg = proc
        threading.Thread(
            target=self.paceLoop, args=(proc,), name="spotify-pace", daemon=True
        ).start()

    def feedLoop(self):
        while not self.stopped.is_set():
            try:
                data = os.read(self.fifoFd, pipeBytes)
            except OSError:
                if self.stopped.is_set():
                    return
                logger.exception("Reading the Spotify audio pipe failed")
                time.sleep(1)
                continue

            proc = self.ffmpeg
            try:
                writeAll(proc.stdin.fileno(), data)
            except (OSError, ValueError):
                if self.stopped.is_set():
                    return
                logger.warning("The ffmpeg resampler exited, restarting it")
                proc.kill()
                self.startFfmpeg()

    def paceLoop(self, proc):
        fd = proc.stdout.fileno()
        nextTime = time.perf_counter()
        while not self.stopped.is_set():
            try:
                frame = readExactly(fd, frameBytes)
            except OSError:
                return
            if frame is None:
                return

            now = time.perf_counter()
            if now - nextTime > stallSeconds:
                # Paused, between tracks or just started. Nothing to catch up on.
                nextTime = now
            elif nextTime > now:
                time.sleep(nextTime - now)

            with self.framesLock:
                self.frames.append(frame)
            nextTime += frameSeconds

    def readFrame(self):
        # Keeps a few frames in hand after running dry so small timing wobbles between
        # this clock and discord.py's do not turn into audible gaps.
        with self.framesLock:
            if self.starved:
                if len(self.frames) < prebufferFrames:
                    return silenceFrame
                self.starved = False
            if not self.frames:
                self.starved = True
                return silenceFrame
            return self.frames.popleft()

    def stop(self):
        self.stopped.set()
        if self.ffmpeg is not None:
            self.ffmpeg.kill()
        if self.fifoFd is not None:
            # Wakes the feed thread out of its blocking read so it can see the stop.
            try:
                os.write(self.fifoFd, bytes(4))
            except OSError:
                pass


class BridgeSource(discord.AudioSource):
    # Never ends, silence is returned while nothing is playing. The cog pauses the
    # voice client instead so the bot does not sit there transmitting silence.
    def __init__(self, bridge):
        self.bridge = bridge

    def read(self):
        return self.bridge.readFrame()

    def is_opus(self):
        return False
