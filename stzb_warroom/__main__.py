"""stzb-warroom (率土战局): report the games played on this computer; show the server's notifications.

    python -m stzb_warroom capture     report the games played on this computer (a service)
    python -m stzb_warroom notify      print the server's notifications in this terminal
    python -m stzb_warroom gui         both in a window (what the Windows package runs)

The commands read the tokens from ST_CLIENT_TOKEN (several separated by commas, one per game
account played on this computer); the window also asks for them and can keep them encrypted.
Nothing is ever sent to the game.
"""

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from . import notify
from .api import Server, client_tokens, default_server
from .runner import run_capture, state_dir


def capture(args):
    tokens = client_tokens()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def started(_uploader):
        print(json.dumps({"event": "started", "server": args.server, "tokens": len(tokens)}), flush=True)

    try:
        run_capture(Server(args.server), tokens, args.state_dir, stop, interface=args.interface,
                    all_hosts=args.all_hosts, dumpcap=args.dumpcap, started=started)
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"capture stopped: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m stzb_warroom", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default=default_server(),
                        help="the server's HTTPS origin (default: ST_SERVER, or the one the package was built for)")
    parser.add_argument("--state-dir", type=Path, default=state_dir(), help="queue and cursors (default %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("capture", help="capture and upload until stopped")
    run.add_argument("--interface", help="default: the adapter of the default route")
    run.add_argument("--all-hosts", action="store_true",
                     help="every host on the adapter, not only this computer's connections")
    run.add_argument("--dumpcap",
                     help="path of dumpcap (default: ST_DUMPCAP, else found on PATH or where Wireshark is installed)")
    show = commands.add_parser("notify", help="print the server's notifications until Ctrl+C")
    show.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    window = commands.add_parser("gui", help="capture, upload, notifications and Bark in a window")
    window.add_argument("--background", action="store_true", help="no window until the program is started again")
    args = parser.parse_args(argv)
    if not args.server:
        parser.error("no server: pass --server or set ST_SERVER")
    if args.command == "capture":
        return capture(args)
    if args.command == "gui":
        from . import webui
        return webui.main(Server(args.server), args.state_dir, background=args.background)
    notify.run(Server(args.server), client_tokens(), args.state_dir,
               color=None if args.color == "auto" else args.color == "always")


if __name__ == "__main__":
    main()
