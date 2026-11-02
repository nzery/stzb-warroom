"""Run dumpcap on the game's TCP port and hand every packet to a callback.

Only reads packets; nothing is sent to the game or changed. By default it captures on the
adapter of the default route and keeps only connections whose local end is this computer:
a phone routed through this computer shows each packet twice on that adapter (phone to
server, then this computer's NATed leg), the game running here only once; keeping our own
address captures each packet once in both cases.
"""

import collections
import ntpath
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .pcap import read_timed_packets

GAME_PORT = 8001
ROUTE_EVERY = 15  # seconds between two looks at which adapter the default route uses
SETTLE = 2  # dumpcap says "Capturing on" even for an adapter it then fails on: seconds to see that
UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
APP_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Wireshark.exe"


def command_folder(command):
    """The folder of the program a registry command names: '"C:\\x\\a.exe" /S' -> 'C:\\x'."""
    command = (command or "").strip()
    if command.startswith('"'):
        command = command[1:].split('"', 1)[0]
    elif ".exe" in command.lower():
        command = command[:command.lower().index(".exe") + 4]
    return ntpath.dirname(re.sub(r",-?\d+$", "", command))  # DisplayIcon may end with ",0"


def registered_wireshark():
    """Folders where the Wireshark installer says it put Wireshark (both registry views)."""
    import winreg

    folders = []
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, APP_PATH, 0, winreg.KEY_READ | view) as key:
                    folders.append(command_folder(winreg.QueryValueEx(key, "")[0]))
            except OSError:
                pass
            try:
                uninstall = winreg.OpenKey(root, UNINSTALL, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with uninstall:
                for index in range(winreg.QueryInfoKey(uninstall)[0]):
                    try:
                        with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as key:
                            name = winreg.QueryValueEx(key, "DisplayName")[0]
                            if not str(name).startswith("Wireshark"):
                                continue
                            for field in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    value = winreg.QueryValueEx(key, field)[0]
                                except OSError:
                                    continue
                                folders.append(value.strip().strip('"') if field == "InstallLocation"
                                               else command_folder(value))
                    except OSError:
                        continue
    return [folder for folder in folders if folder]


def fixed_drives():
    """The local hard drives' roots ("C:\\", "D:\\", ...): no network, removable or CD drive, which may be slow."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    mask = kernel32.GetLogicalDrives()
    roots = [f"{chr(65 + bit)}:\\" for bit in range(26) if mask >> bit & 1]
    return [root for root in roots if kernel32.GetDriveTypeW(root) == 3]  # DRIVE_FIXED


def dumpcap_candidates(preferred=()):
    """Where dumpcap may be, in order: ST_DUMPCAP, `preferred`, then where Wireshark installs."""
    places = [os.environ.get("ST_DUMPCAP", ""), *preferred]
    if os.name == "nt":
        places += [os.path.join(folder, "dumpcap.exe") for folder in registered_wireshark()]
        places.append(shutil.which("dumpcap") or "")
        places += [os.path.join(os.environ[name], "Wireshark", "dumpcap.exe")
                   for name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)") if os.environ.get(name)]
        places.append(r"C:\Program Files\Wireshark\dumpcap.exe")
        # Installed where the registry does not say (unpacked, an old installer): the usual folders on each drive.
        places += [os.path.join(root, *folder, "dumpcap.exe") for root in fixed_drives()
                   for folder in (("Program Files", "Wireshark"), ("Program Files (x86)", "Wireshark"), ("Wireshark",))]
    else:
        places += [shutil.which("dumpcap") or "", "/usr/bin/dumpcap"]
    return list(dict.fromkeys(os.path.normpath(place) for place in places if place))


def find_dumpcap(preferred=()):
    """(the first dumpcap found or None, every place looked at)."""
    places = dumpcap_candidates(preferred)
    return next((place for place in places if os.path.isfile(place)), None), places


def default_interface():
    """(dumpcap interface, local address) of the adapter holding the default IPv4 route."""
    if os.name != "nt":
        fields = subprocess.run(["ip", "-o", "-4", "route", "get", "223.5.5.5"], capture_output=True,
                                text=True, check=True).stdout.split()
        return fields[fields.index("dev") + 1], fields[fields.index("src") + 1]
    import winreg

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("223.5.5.5", 53))  # no packet is sent; only picks the route
        local_ip = probe.getsockname()[0]
    finally:
        probe.close()
    root = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as interfaces:
        for index in range(winreg.QueryInfoKey(interfaces)[0]):
            guid = winreg.EnumKey(interfaces, index)
            with winreg.OpenKey(interfaces, guid) as key:
                addresses = []
                for name in ("DhcpIPAddress", "IPAddress"):
                    try:
                        value = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        continue
                    addresses += value if isinstance(value, list) else [value]
                if local_ip in addresses:
                    return rf"\Device\NPF_{guid.upper()}", local_ip
    raise RuntimeError(f"no adapter found for local address {local_ip}; pass --interface")


def interface_addresses(interface):
    """Global addresses of a Linux adapter (both families)."""
    lines = subprocess.run(["ip", "-o", "addr", "show", "dev", interface, "scope", "global"],
                           capture_output=True, text=True, check=True).stdout.splitlines()
    return [line.split()[3].split("/")[0] for line in lines if len(line.split()) > 3]


def capture_filter(port, hosts=()):
    return f"tcp port {port}" + (" and (" + " or ".join(f"host {h}" for h in hosts) + ")" if hosts else "")


class Capture:
    """dumpcap in the background; `on_packet(time, linktype, packet)` runs on its reader thread."""

    def __init__(self, on_packet, interface=None, all_hosts=False, dumpcap=None):
        self.on_packet, self.interface, self.all_hosts, self.dumpcap = on_packet, interface, all_hosts, dumpcap
        self.follows_route = interface is None  # an adapter asked for by name stays
        self.route, self.hosts = None, []
        self.route_checked = time.monotonic()
        self.process = None
        self.error = None
        self.stderr = collections.deque(maxlen=20)
        self.threads = []

    def start(self, timeout=15):
        if self.dumpcap is None:
            self.dumpcap, places = find_dumpcap()
            if self.dumpcap is None:
                raise RuntimeError("dumpcap not found (looked at " + ", ".join(places)
                                   + "); install Wireshark or pass --dumpcap")
        elif not Path(self.dumpcap).is_file():
            raise RuntimeError(f"dumpcap not found at {self.dumpcap}; install Wireshark or pass --dumpcap")
        print(f"dumpcap: {self.dumpcap}", flush=True)
        if self.follows_route:
            self.route = default_interface()
            self.interface, self.hosts = self.route[0], [self.route[1]]
        else:
            self.hosts = interface_addresses(self.interface) if os.name != "nt" else []
        self._run(timeout)

    def _run(self, timeout=15, settle=0):
        """Start dumpcap on self.interface and self.hosts; RuntimeError if it does not run for `settle` seconds."""
        bpf = capture_filter(GAME_PORT, () if self.all_hosts else self.hosts)
        print(f"capture interface: {self.interface} filter: {bpf}", flush=True)
        self.error = None
        self.stderr.clear()
        self.process = subprocess.Popen([self.dumpcap, "-q", "-i", self.interface, "-f", bpf, "-w", "-"],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ready = threading.Event()
        self.threads = [threading.Thread(target=self._read_stderr, args=(ready,), daemon=True),
                        threading.Thread(target=self._read_packets, daemon=True)]
        for thread in self.threads:
            thread.start()
        if not ready.wait(timeout) or self.process.poll() is not None:
            self.close()
            raise RuntimeError("dumpcap did not start capturing: " + " | ".join(self.stderr))
        try:
            self.process.wait(settle)
        except subprocess.TimeoutExpired:
            return
        self.close()
        raise RuntimeError("dumpcap stopped: " + " | ".join(self.stderr))

    def follow(self):
        """Capture on the adapter of the default route when the computer moved to another network.

        The game connections made from now on leave through it (2026-11-02: a computer moving
        between two networks kept relaying the phone's game with nothing captured for 20 minutes,
        the window still saying it was reporting). No route for now: stay until there is one.
        An adapter dumpcap cannot capture on (some VPNs): back to the one that worked.
        True when the route changed.
        """
        now = time.monotonic()
        if not self.follows_route or now - self.route_checked < ROUTE_EVERY:
            return False
        self.route_checked = now
        try:
            route = default_interface()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return False
        if route == self.route:
            return False
        self.route = route  # tried once, whatever comes of it
        working = self.interface, self.hosts
        self.close()
        self.interface, self.hosts = route[0], [route[1]]
        try:
            self._run(settle=SETTLE)
        except (OSError, RuntimeError) as exc:
            print(f"cannot capture on {route[0]}: {exc}", flush=True)
            self.interface, self.hosts = working
            self._run()
        return True

    def _read_stderr(self, ready):
        for raw in self.process.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.stderr.append(line)
            if line.startswith("Capturing on"):
                ready.set()
        ready.set()

    def _read_packets(self):
        try:
            for captured_at, linktype, packet in read_timed_packets(self.process.stdout):
                self.on_packet(captured_at, linktype, packet)
        except Exception as exc:  # reported by check()
            self.error = exc
        finally:
            if self.error is None:
                self.error = RuntimeError("capture stopped: " + " | ".join(self.stderr))

    def check(self):
        """Raise if dumpcap or the reader stopped."""
        if self.error is not None:
            raise RuntimeError(f"capture failed: {self.error}") from self.error

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        # Drain buffered packets before the runner closes their queue database.
        for thread in self.threads:
            thread.join()
        if self.process:
            self.process.stdout.close()
            self.process.stderr.close()
