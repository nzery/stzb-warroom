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
    """One token's notifications; its cursor {"epoch", "after"} kept in `path`. `role` (a role key the
    server listed in /v1/client/status ``roles``): that role's notifications, though another may be the
    token's current one (one NetEase account on two servers at once); None: the token's current role.
    `seed`: a cursor file read when `path` has none yet (the one kept before roles were followed apart).

    `show(notice)` displays one (default: printed, coloured when `color`); `warn(text)` reports
    a problem with the stream, and that it is over once the stream opens again (default: stderr).
    `ready()`: whether the server said the token has an approved role (the window's account
    check); until then the stream is not asked, it would only be refused (403). None: always ask.
    """

    def __init__(self, server, token, path, color=False, label=False, show=None, warn=say, ready=None, role=None,
                 seed=None):
        self.server, self.token, self.path, self.color, self.label = server, token, Path(path), color, label
        self.show, self.warn, self.ready, self.role = show or self.print, warn, ready, role
        self.problem = None  # the last problem reported, until the stream opens again
        self.rejected = False  # the token was refused (401)
        value = {}
        for source in (self.path, seed):
            if source is None:
                continue
            try:
                value = json.loads(Path(source).read_text())
                break
            except (OSError, ValueError):
                value = {}
        if not isinstance(value, dict):
            value = {}
        self.epoch, self.after = value.get("epoch"), value.get("after")

    def print(self, notice):
        if notice.get("hidden"):  # a recall: only the window uses what it names
            return
        with LOCK:
            print(render(notice, self.color, self.label), flush=True)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"epoch": self.epoch, "after": self.after}))
        temporary.replace(self.path)

    def follow_once(self, stop):
        role = {"role": self.role} if self.role is not None else {}
        if self.after is None:
            latest = self.server.json("/v1/state/events?" + urlencode({"after": "latest", **role}), self.token)
            self.epoch, self.after = latest.get("event_epoch"), int(latest.get("next_id") or 0)
            self.save()
        query = urlencode({"after": self.after, "notify": 1, **({"epoch": self.epoch} if self.epoch else {}), **role})
        with self.server.open("/v1/state/stream?" + query, self.token, timeout=60) as stream:
            for kind, event_id, data in events(stream, stop):
                if kind == "ready":
                    self.epoch = data.get("event_epoch")
                    if self.problem:  # the problem's line would otherwise stay the last word
                        self.warn("notifications resumed")
                        self.problem = None
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
        delay = 2
        while not stop.is_set():
            if self.ready and not self.ready():
                stop.wait(5)
                continue
            try:
                self.follow_once(stop)
                delay = 2
            except HTTPError as exc:
                if exc.code == 401:
                    self.warn("a token was rejected (HTTP 401); its notifications stop")
                    self.rejected = True
                    return
                if exc.code == 403 and self.role is not None:  # no longer played: start() follows it again if it is
                    return
                # 403: the token has no approved role yet.
                summary, delay = f"HTTP {exc.code}", 60 if exc.code == 403 else 10
                if summary != self.problem:
                    self.warn(f"notifications unavailable: {summary}")
                    self.problem = summary
            except (OSError, ValueError) as exc:
                summary, delay = f"{type(exc).__name__}: {exc}", min(delay * 2, 60)
                if summary != self.problem:
                    self.warn(f"notifications interrupted: {summary}")
                    self.problem = summary
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
        # Every 2 s only while the server asks for an answer. A prompt that only tells (an alliance short of
        # reports, a capture hint) stays up for hours: every 2 s it was about 1600 requests an hour per player
        # (2026-11-11); it is taken down within a minute of the server dropping it.
        stop.wait(2 if prompt_id and value.get("ask") else 60)


UNKNOWN = object()  # roles_for: the token's roles not known yet
ROLES_EVERY = 5  # seconds between two looks at the roles to follow (the window checks the server every minute)


def follow_roles(server, token, state_dir, stop, roles_for, label, ready, options, every=ROLES_EVERY):
    """Follow each role `roles_for(token)` lists (the server's ``roles``: the token's current role and
    any other it plays now), one Follower each, started and stopped as the list changes; when it is
    None (a server without ``roles``), the token's current role only, as before. UNKNOWN: waits."""
    key = hashlib.sha256(token.encode()).hexdigest()[:8]
    legacy = Path(state_dir) / f"notify-{key}.json"
    running = {}  # role key (None: the current one) -> (its stop, its thread)
    try:
        while not stop.is_set():
            roles = roles_for(token)
            if roles is not UNKNOWN:
                wanted = [None] if roles is None else [r for r in roles if type(r) is int and r > 0][:8]
                if any(follower.rejected for _own, follower in running.values()):
                    return  # the token was refused (401): its notifications stop, as with one follower
                # A follower stopped (403: the role no longer played) stays until the list drops the role:
                # it starts again only when listed anew, not every few seconds meanwhile.
                for role in [r for r in running if r not in wanted]:
                    running.pop(role)[0].set()
                for index, role in enumerate(wanted):
                    if role in running:
                        continue
                    path = legacy if role is None else Path(state_dir) / f"notify-{key}-{role}.json"
                    follower = Follower(server, token, path, label=label, ready=ready, role=role,
                                        seed=legacy if role is not None and index == 0 else None, **options)
                    own = threading.Event()
                    running[role] = (own, follower)
                    threading.Thread(target=follower.follow, args=(own,), daemon=True).start()
            stop.wait(every)
    finally:
        for own, _follower in running.values():
            own.set()


def start(server, tokens, state_dir, stop, prompt_for=lambda _token: ask_terminal, ready_for=lambda _token: None,
          roles_for=lambda _token: None, **options):
    """Follow every token's notifications and prompts until `stop`; the notification threads.

    `prompt_for(token)`: follow_prompts's `show` for that token; `ready_for(token)`: its
    Follower's `ready`; `roles_for(token)`: the roles to follow (follow_roles). The prompts are asked
    from the start: they say why a token's capture is not uploaded before it is approved.
    `options`: Follower's color, show and warn.
    """
    threads = []
    for token in tokens:
        threads.append(threading.Thread(target=follow_roles, daemon=True, args=(
            server, token, state_dir, stop, roles_for, len(tokens) > 1, ready_for(token), options)))
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
