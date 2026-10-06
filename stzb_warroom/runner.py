"""The capture-and-upload loop shared by the command line and the window."""

import os
import threading
from pathlib import Path

from .capture import Capture
from .upload import Queue, Uploader


def state_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "stzb-warroom"


def lock(path):
    """One capture process per queue; the OS releases the lock if it dies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError(f"another capture process is using {path.parent}")
    return handle


def run_capture(server, tokens, folder, stop, interface=None, all_hosts=False, dumpcap=None, started=None):
    """Capture and upload until `stop` is set; RuntimeError when the capture or the upload fails.

    `started(uploader)` runs once dumpcap is capturing.
    """
    folder = Path(folder)
    held = lock(folder / "capture.lock")
    try:
        uploader = Uploader(Queue(folder / "queue.sqlite"), server, tokens)
        uploader.stop = stop  # a rejected token stops the capture too
        source = Capture(uploader.on_packet, interface=interface, all_hosts=all_hosts,
                         **({"dumpcap": dumpcap} if dumpcap else {}))
        thread = threading.Thread(target=uploader.run, name="upload", daemon=True)
        try:
            source.start()
            thread.start()
            if started:
                started(uploader)
            while not stop.wait(1):
                source.check()
        finally:
            stop.set()
            source.close()
            if thread.is_alive():
                thread.join(timeout=35)
            if not thread.is_alive():
                uploader.queue.close()  # the window may start again on the same files
        if uploader.error is not None:
            raise uploader.error
    finally:
        held.close()
