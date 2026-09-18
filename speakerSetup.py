# One time setup for the Spotify Connect speaker. Stop the bot first, then run:
#
#     python speakerSetup.py           download go-librespot if needed and link a Spotify account
#     python speakerSetup.py relink    forget the linked account and link a different one
#
# The linked account must be Spotify Premium. It is the account everyone logs in to
# on their own phone or computer to see the speaker and control it from the Spotify app.
# No browser is needed on this machine, the approval happens at spotify.com/pair.

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

from music import librespot

linkTimeoutSeconds = 15 * 60


def apiGet(path):
    # Returns (status, parsed body or None), or (None, None) while the API is not up.
    url = "http://127.0.0.1:" + str(librespot.apiPort()) + path
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
            return resp.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as err:
        return err.code, None
    except (urllib.error.URLError, OSError):
        return None, None


def ensureBinary():
    if librespot.binaryPath.exists():
        print("go-librespot is already installed at " + str(librespot.binaryPath))
        return
    print("Downloading go-librespot " + librespot.librespotVersion + "...")
    url = librespot.downloadBinary()
    print("Installed from " + url)


def waitForLink(proc):
    shownCode = None
    deadline = time.monotonic() + linkTimeoutSeconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(
                "go-librespot exited early with code " + str(proc.returncode) + ". "
                "If it says it is already running, stop the bot first."
            )

        # /status only answers once an account is linked, so the code is checked first.
        status, code = apiGet("/auth/code")
        if status == 204:
            status, body = apiGet("/status")
            if status == 200 and body:
                return body
        elif status == 200 and code and code.get("code") != shownCode:
            shownCode = code.get("code")
            print()
            print("Open this link on your phone or computer:")
            print()
            print("    " + str(code.get("url")))
            print()
            print("Log in as the shared Premium account and approve. If asked for a code, enter: " + str(shownCode))
            print("Waiting for approval...")
        time.sleep(2)
    raise SystemExit("Timed out waiting for approval. Run this again to get a fresh code.")


def main():
    relink = "relink" in {arg.lower() for arg in sys.argv[1:]}

    try:
        ensureBinary()
    except librespot.LibrespotError as err:
        raise SystemExit(str(err))

    librespot.writeConfig()
    if relink:
        librespot.forgetAccount()
        print("Forgot the previously linked account.")

    # go-librespot refuses to start playback without a reader on its audio pipe, and
    # this run never plays anything, so the pipe only has to exist.
    librespot.fifoPath.parent.mkdir(parents=True, exist_ok=True)
    if not librespot.fifoPath.exists():
        os.mkfifo(librespot.fifoPath)

    proc = subprocess.Popen(
        librespot.launchCommand(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        status = waitForLink(proc)
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled, no account was linked.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print("Linked to Spotify account: " + str(status.get("username")))
    print("Speaker name in the Spotify app: " + str(status.get("device_name")))
    print()
    print("Start the bot again. Anyone logged in to that account will see the speaker")
    print("under the devices icon in Spotify and can play, pause, skip and queue from there.")


if __name__ == "__main__":
    main()
