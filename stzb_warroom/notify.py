"""Print the server's notifications in this terminal, and what the server asks it to show.

For each token it follows ``/v1/state/stream?notify=1`` (only notifications, already
filtered and tagged by the server) and remembers its cursor in a small file, so a restart
neither repeats nor loses one; the first start begins at the newest. It also prints the
server's prompts (``/v1/client/prompt``, e.g. why nothing is uploaded) and
sends back the line typed when a prompt asks for one.
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

GAME_TZ = timezone(timedelta(hours=8))  # the game's and the players' clock
STYLES = {"alarm": ("\033[1;97;41m", "\033[1;31m"), "warn": ("\033[1;30;43m", "\033[33m"),
          "info": ("\033[1;36m", "")}
RESET = "\033[0m"
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # no escape sequences from game text
LOCK = threading.Lock()


def plain(value):
    return CONTROL.sub("", str(value or ""))


def render(notice, color, label=False):
    """14:03:21 [敌袭] title, then the body lines indented; coloured by level."""
    level = notice.get("level") if notice.get("level") in STYLES else "info"
    tag_style, text_style = STYLES[level] if color else ("", "")
    reset = RESET if color else ""
    stamp = notice.get("time") if isinstance(notice.get("time"), (int, float)) else time.time()
    clock = datetime.fromtimestamp(stamp, GAME_TZ).strftime("%H:%M:%S")
    title = (f"[{notice.get('role')}] " if label and notice.get("role") else "") + plain(notice.get("title"))
    lines = [f"{clock} {tag_style}[{plain(notice.get('tag') or '通知')}]{reset} {text_style}{title}{reset}"]
    lines += [f"{' ' * 9}{text_style}{line.strip()}{reset}" for line in plain(notice.get("body")).splitlines()
              if line.strip()]
    return ("\a" if level == "alarm" else "") + "\n".join(lines)


def events(stream, stop):
    """(kind, id, data) of each server-sent event."""
    kind, event_id, data = None, None, []
    for raw in stream:
        if stop.is_set():
            return
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if kind and data:
                yield kind, event_id, json.loads("\n".join(data))
            kind, event_id, data = None, None, []
        elif line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("id: "):
            event_id = line[4:]
        elif line.startswith("data: "):
            data.append(line[6:])


def say(text):
    with LOCK:
        print(text, file=sys.stderr, flush=True)


def ask_terminal(text, ask):
    """A prompt in the terminal: its text, then the line typed when it asks for one."""
    if text is None:  # the prompt is over; what was printed stays
        return None
    say(text)
    return input(ask) if ask else None


class Follower:
    """One token's notifications; its cursor {"epoch", "after"} kept in `path`.

    `show(notice)` displays one (default: printed, coloured when `color`); `warn(text)` reports
    a problem with the stream (default: stderr).
    """

    def __init__(self, server, token, path, color=False, label=False, show=None, warn=say):
        self.server, self.token, self.path, self.color, self.label = server, token, Path(path), color, label
        self.show, self.warn = show or self.print, warn
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        self.epoch, self.after = value.get("epoch"), value.get("after")

    def print(self, notice):
        with LOCK:
            print(render(notice, self.color, self.label), flush=True)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"epoch": self.epoch, "after": self.after}))
        temporary.replace(self.path)

    def follow_once(self, stop):
        if self.after is None:
            latest = self.server.json("/v1/state/events?after=latest", self.token)
            self.epoch, self.after = latest.get("event_epoch"), int(latest.get("next_id") or 0)
            self.save()
        query = urlencode({"after": self.after, "notify": 1, **({"epoch": self.epoch} if self.epoch else {})})
        with self.server.open("/v1/state/stream?" + query, self.token, timeout=60) as stream:
            for kind, event_id, data in events(stream, stop):
                if kind == "ready":
                    self.epoch = data.get("event_epoch")
                elif kind == "notify":
                    self.show(data)
                    self.after = int(event_id)
                    self.save()
                elif kind == "cursor":  # read past other events: a reconnect need not rescan them
                    self.after = int(event_id)
                    self.save()
                elif kind == "reset":  # the event log was rebuilt: start from its newest event
                    self.after = self.epoch = None
                    return
                elif kind == "unavailable":
                    raise ConnectionError("no game login captured for this token yet")

    def follow(self, stop):
        last, delay = None, 2
        while not stop.is_set():
            try:
                self.follow_once(stop)
                last, delay = None, 2
            except HTTPError as exc:
                if exc.code == 401:
                    self.warn("a token was rejected (HTTP 401); its notifications stop")
                    return
                # 403: the token has no approved role yet.
                summary, delay = f"HTTP {exc.code}", 60 if exc.code == 403 else 10
                if summary != last:
                    self.warn(f"notifications unavailable: {summary}")
                    last = summary
            except (OSError, ValueError) as exc:
                summary, delay = f"{type(exc).__name__}: {exc}", min(delay * 2, 60)
                if summary != last:
                    self.warn(f"notifications interrupted: {summary}")
                    last = summary
            stop.wait(delay)


def follow_prompts(server, token, stop, show=ask_terminal):
    """Show the server's prompt when it changes; answer it when it asks.

    `show(text, ask)` displays a prompt and, when `ask` is set, returns the answer;
    `show(None, None)` when the server no longer has it (the problem is over).
    """
    shown, start = None, True
    while not stop.is_set():
        try:
            value = server.json("/v1/client/prompt" + ("?start=1" if start else ""), token)
            start = False
        except HTTPError as exc:
            if exc.code in (401, 404):
                return
            if exc.code != 403:  # 403: no prompt, and the token not approved yet
                stop.wait(60)
                continue
            value, start = {}, False
        except (OSError, ValueError):
            stop.wait(30)
            continue
        prompt_id = value.get("id") if isinstance(value.get("id"), str) else None
        if shown and not prompt_id:
            show(None, None)
        if prompt_id and prompt_id != shown:
            answer = show(plain(value.get("text")), plain(value.get("ask")))
            if value.get("ask") and answer is not None and not stop.is_set():
                try:
                    server.json("/v1/client/prompt", token, {"id": prompt_id, "answer": answer.strip()})
                except (OSError, ValueError):
                    pass
        shown = prompt_id
        stop.wait(2 if prompt_id else 60)


def start(server, tokens, state_dir, stop, prompt_for=lambda _token: ask_terminal, **options):
    """Follow every token's notifications and prompts until `stop`; the notification threads.

    `prompt_for(token)`: follow_prompts's `show` for that token. `options`: Follower's color,
    show and warn.
    """
    threads = []
    for token in tokens:
        key = hashlib.sha256(token.encode()).hexdigest()[:8]
        follower = Follower(server, token, Path(state_dir) / f"notify-{key}.json", label=len(tokens) > 1,
                            **options)
        threads.append(threading.Thread(target=follower.follow, args=(stop,), daemon=True))
        threading.Thread(target=follow_prompts, args=(server, token, stop, prompt_for(token)), daemon=True).start()
    for thread in threads:
        thread.start()
    return threads


def run(server, tokens, state_dir, color=None):
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR") if color is None else color
    stop = threading.Event()
    say(f"正在接收 {server.origin} 的通知（{len(tokens)} 个 Token），Ctrl+C 退出")
    threads = start(server, tokens, state_dir, stop, color=color)
    try:
        while any(thread.is_alive() for thread in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()
        return
    raise SystemExit(1)  # every token was rejected
