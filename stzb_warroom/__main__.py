"""stzb-warroom (率土战局): report the games played on this computer; show the server's notifications.

    python -m stzb_warroom capture     report the games played on this computer (a service)
    python -m stzb_warroom notify      print the server's notifications in this terminal
    python -m stzb_warroom gui         both in a window (what the Windows package runs)
    python -m stzb_warroom join CODE   join an alliance with its invitation code (adds a token)

The commands read the tokens from ST_CLIENT_TOKEN (several separated by commas, one per game
account played on this computer); the window also asks for them and can keep them encrypted.
``join`` adds the token it is given to ST_CLIENT_TOKEN in ~/.config/environment.d (Linux),
without showing it. Nothing is ever sent to the game.
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
from pathlib import Path

from . import notify
from .api import Server, client_tokens, default_server, invite_code
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


ENVIRONMENT_D = Path.home() / ".config" / "environment.d"


def token_file(folder=None):
    """The environment.d file that sets ST_CLIENT_TOKEN (the last one wins), else a new one."""
    folder = Path(folder or ENVIRONMENT_D)
    found = [path for path in sorted(folder.glob("*.conf"))
             if re.search(r"^\s*ST_CLIENT_TOKEN=", path.read_text(encoding="utf-8"), re.M)]
    return found[-1] if found else folder / "90-stzb.conf"


def add_to_file(path, token):
    """Add `token` to ST_CLIENT_TOKEN in an environment.d file (0600), keeping the rest."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for index, line in enumerate(lines):
        name, _sep, value = line.strip().partition("=")
        if name == "ST_CLIENT_TOKEN":
            tokens = [t for t in re.split(r"[\s,]+", value.strip().strip('"')) if t]
            lines[index] = "ST_CLIENT_TOKEN=" + ",".join(dict.fromkeys(tokens + [token]))
            break
    else:
        lines.append(f"ST_CLIENT_TOKEN={token}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    temporary.replace(path)


def join(args):
    code = invite_code(args.code)
    if not code:
        raise SystemExit("邀请码应是 stj_ 开头的 26 个字符")
    try:
        reply = Server(args.server).join(code)
    except ValueError as exc:
        raise SystemExit(str(exc))
    path = Path(args.env_file) if args.env_file else token_file()
    add_to_file(path, reply["token"])
    print(reply.get("text") or "已加入。")
    print(f"新 Token 已加到 {path} 的 ST_CLIENT_TOKEN（不在这里显示）。让上报服务用上它：\n"
          "  systemctl --user daemon-reload && systemctl --user restart stzb-warroom\n"
          "终端里运行 notify 前请重新登录，或重新打开终端。")


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
    joining = commands.add_parser("join", help="join an alliance with its invitation code (adds a token)")
    joining.add_argument("code", help="the invitation code (stj_...)")
    joining.add_argument("--env-file", help="the environment.d file to add the token to "
                         "(default: the one that sets ST_CLIENT_TOKEN, else ~/.config/environment.d/90-stzb.conf)")
    args = parser.parse_args(argv)
    if not args.server:
        parser.error("no server: pass --server or set ST_SERVER")
    if args.command == "capture":
        return capture(args)
    if args.command == "join":
        return join(args)
    if args.command == "gui":
        from . import webui
        return webui.main(Server(args.server), args.state_dir, background=args.background)
    notify.run(Server(args.server), client_tokens(), args.state_dir,
               color=None if args.color == "auto" else args.color == "always")


if __name__ == "__main__":
    main()
