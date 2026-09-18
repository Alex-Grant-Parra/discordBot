# go-librespot is the open source Spotify Connect receiver that makes the bot show up
# as a speaker in the Spotify app. The bot runs it as a child process and talks to it
# over its local REST and WebSocket API. Audio comes out of a named pipe, see audioBridge.

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import aiohttp

logger = logging.getLogger("music.librespot")

librespotVersion = "v0.9.1"
releaseUrl = "https://github.com/devgianlu/go-librespot/releases/download/{version}/go-librespot_{asset}.tar.gz"

baseDir = Path(os.getenv("LIBRESPOT_DIR", "librespot")).resolve()
binaryPath = baseDir / "go-librespot"
configDir = baseDir / "config"
fifoPath = baseDir / "audio.fifo"

requestTimeout = aiohttp.ClientTimeout(total=15)
webApiQueueUrl = "https://api.spotify.com/v1/me/player/queue"


class LibrespotError(RuntimeError):
    pass


class NotLinkedError(LibrespotError):
    def __init__(self):
        super().__init__("No Spotify account is linked to the speaker yet")


def apiPort():
    try:
        return int(os.getenv("LIBRESPOT_PORT", "3678"))
    except ValueError:
        return 3678


def deviceName():
    return os.getenv("LIBRESPOT_DEVICE_NAME", "").strip() or "Discord Bot"


def initialVolume():
    try:
        value = int(os.getenv("MUSIC_DEFAULT_VOLUME", "50"))
    except ValueError:
        value = 50
    return max(0, min(100, value))


def releaseAsset():
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "linux_x86_64"
    if machine in ("aarch64", "arm64"):
        return "linux_arm64"
    if machine.startswith("armv"):
        return "linux_armv6"
    raise LibrespotError("No prebuilt go-librespot for this machine type: " + machine)


def downloadBinary():
    url = releaseUrl.format(version=librespotVersion, asset=releaseAsset())
    baseDir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "go-librespot.tar.gz"
        urllib.request.urlretrieve(url, archive)
        with tarfile.open(archive) as tar:
            member = tar.getmember("go-librespot")
            member.name = binaryPath.name
            tar.extract(member, baseDir)
    binaryPath.chmod(0o755)
    return url


def writeConfig():
    # Rewritten on every start so .env stays the single place settings live.
    # JSON strings are valid YAML double quoted scalars, which keeps quoting safe.
    configDir.mkdir(parents=True, exist_ok=True)
    lines = [
        "log_level: info",
        "log_disable_timestamp: true",
        "device_name: " + json.dumps(deviceName()),
        "device_type: speaker",
        "zeroconf_enabled: false",
        "credentials:",
        "  type: device_auth",
        "server:",
        "  enabled: true",
        "  address: 127.0.0.1",
        "  port: " + str(apiPort()),
        "audio_backend: pipe",
        "audio_output_pipe: " + json.dumps(str(fifoPath)),
        "audio_output_pipe_format: s16le",
        "bitrate: 320",
        "volume_steps: 100",
        "initial_volume: " + str(initialVolume()),
        "metadata:",
        "  enabled: true",
    ]
    (configDir / "config.yml").write_text("\n".join(lines) + "\n")


def forgetAccount():
    for name in ("state.json", "credentials.json"):
        (configDir / name).unlink(missing_ok=True)


def launchCommand():
    command = [str(binaryPath), "--config_dir", str(configDir)]
    # Makes the kernel stop go-librespot if the bot dies without cleaning up. An orphan
    # would hold the config lockfile and block the next start.
    setpriv = shutil.which("setpriv")
    if setpriv:
        command = [setpriv, "--pdeathsig", "TERM", "--"] + command
    return command


logLevelPattern = re.compile(r"level=(\w+)")
logLevels = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
    "panic": logging.CRITICAL,
}


class LibrespotProcess:
    def __init__(self):
        self.proc = None
        self.task = None
        self.stopping = False

    def start(self):
        writeConfig()
        self.task = asyncio.create_task(self.supervise())

    async def supervise(self):
        loop = asyncio.get_running_loop()
        backoff = 2
        while not self.stopping:
            startedAt = loop.time()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *launchCommand(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError:
                logger.exception("Could not start go-librespot")
            else:
                await self.forwardLogs(self.proc.stdout)
                code = await self.proc.wait()
                if self.stopping:
                    return
                logger.warning("go-librespot exited with code %s", code)

            if loop.time() - startedAt > 60:
                backoff = 2
            logger.info("Restarting go-librespot in %d seconds", backoff)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def forwardLogs(self, stream):
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            match = logLevelPattern.search(text)
            level = logLevels.get(match.group(1), logging.INFO) if match else logging.INFO
            logger.log(level, "%s", text)

    async def stop(self):
        self.stopping = True
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self.task is not None:
            self.task.cancel()


class LibrespotApi:
    def __init__(self, port):
        self.base = "http://127.0.0.1:" + str(port)
        self.session = None

    async def open(self):
        self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session is not None:
            await self.session.close()

    async def request(self, method, path, payload=None, timeout=requestTimeout):
        try:
            async with self.session.request(
                method, self.base + path, json=payload, timeout=timeout
            ) as resp:
                text = await resp.text()
                if resp.status == 204:
                    return None
                if resp.status >= 400:
                    raise LibrespotError(
                        "go-librespot answered " + str(resp.status) + " to " + path + ": " + text.strip()
                    )
                # Successful commands answer 200 with a body of null. Only a 204 means
                # there is no session, so an empty success must not come back as None.
                data = json.loads(text) if text.strip() else None
                return {} if data is None else data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise LibrespotError("The Spotify speaker is not running or not reachable") from err

    async def command(self, path, payload=None):
        # go-librespot answers 204 instead of an error when no account is linked.
        result = await self.request("POST", path, payload)
        if result is None:
            raise NotLinkedError()
        return result

    # Until an account is linked go-librespot holds every request except /auth/code,
    # so that is the one to ask first.
    async def status(self, timeout=requestTimeout):
        return await self.request("GET", "/status", timeout=timeout)

    async def authCode(self):
        # The pairing link and code while waiting for approval, otherwise None.
        return await self.request("GET", "/auth/code")

    async def play(self, uri, skipToUri=""):
        payload = {"uri": uri}
        if skipToUri:
            payload["skip_to_uri"] = skipToUri
        await self.command("/player/play", payload)

    async def pause(self):
        await self.command("/player/pause")

    async def resume(self):
        await self.command("/player/resume")

    async def next(self):
        await self.command("/player/next")

    async def prev(self):
        await self.command("/player/prev")

    async def seek(self, positionMs):
        await self.command("/player/seek", {"position": int(positionMs)})

    async def volume(self):
        result = await self.request("GET", "/player/volume")
        if result is None:
            raise NotLinkedError()
        return result

    async def setVolume(self, value):
        await self.command("/player/volume", {"volume": int(value)})

    async def setShuffle(self, enabled):
        await self.command("/player/shuffle_context", {"shuffle_context": bool(enabled)})

    async def setRepeatContext(self, enabled):
        await self.command("/player/repeat_context", {"repeat_context": bool(enabled)})

    async def setRepeatTrack(self, enabled):
        await self.command("/player/repeat_track", {"repeat_track": bool(enabled)})

    async def addToQueue(self, uri):
        await self.command("/player/add_to_queue", {"uri": uri})

    async def upcomingTracks(self):
        # go-librespot only knows the next track, so the full queue comes from the Web API
        # using the speaker's own session token, which is always the right account.
        tokenInfo = await self.request("POST", "/token")
        token = (tokenInfo or {}).get("token")
        if not token:
            return None
        try:
            async with self.session.get(
                webApiQueueUrl,
                headers={"Authorization": "Bearer " + token},
                timeout=requestTimeout,
            ) as resp:
                if resp.status != 200:
                    logger.info("Web API queue lookup answered %d", resp.status)
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logger.exception("Web API queue lookup failed")
            return None
        return data.get("queue") or []

    async def events(self):
        # Yields player events forever, reconnecting whenever go-librespot restarts.
        url = self.base.replace("http://", "ws://") + "/events"
        while True:
            try:
                async with self.session.ws_connect(url, heartbeat=30) as ws:
                    logger.info("Listening for Spotify speaker events")
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        try:
                            yield json.loads(message.data)
                        except ValueError:
                            logger.warning("Ignoring malformed speaker event: %s", message.data)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
            await asyncio.sleep(2)
