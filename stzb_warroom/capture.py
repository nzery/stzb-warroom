"""Run dumpcap on the game's TCP port and hand every packet to a callback.

Only reads packets; nothing is sent to the game or changed. By default it captures on the
adapter of the default route and keeps only connections whose local end is this computer:
a phone routed through this computer shows each packet twice on that adapter (phone to
server, then this computer's NATed leg), the game running here only once; keeping our own
address captures each packet once in both cases.

A computer dialing its own broadband (PPPoE) has a default route on a connection Npcap cannot
open: its packets are read on the network card below it instead, still wrapped in PPPoE.
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
NETWORK = r"SYSTEM\CurrentControlSet\Control\Network\{4D36E972-E325-11CE-BFC1-08002BE10318}"
NOT_HARDWARE = ("ROOT\\", "SWD\\", "BTH\\", "{")  # virtual adapters, dial-up and VPN connections, Bluetooth
PROBE_SECONDS = 3
PROBE_ADAPTERS = 3


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


def adapter_names():
    """Windows: {adapter GUID in capitals: (the name the player sees, its device id)}."""
    import winreg

    found = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NETWORK) as network:
        for index in range(winreg.QueryInfoKey(network)[0]):
            guid = winreg.EnumKey(network, index)
            try:
                with winreg.OpenKey(network, guid + r"\Connection") as key:
                    found[guid.upper()] = (winreg.QueryValueEx(key, "Name")[0],
                                           winreg.QueryValueEx(key, "PnPInstanceId")[0])
            except OSError:  # not an adapter, or one without those values
                continue
    return found


def is_hardware(device):
    """Whether a device id belongs to a real network card (PCI, USB, ...)."""
    return bool(device) and not device.upper().startswith(NOT_HARDWARE)


def adapter_guid(interface):
    match = re.search(r"\{[0-9A-Fa-f-]+\}", interface)
    return match.group().upper() if match else None


def listed_interfaces(dumpcap):
    """The interfaces dumpcap can see right now; [] if it cannot say."""
    try:
        listing = subprocess.run([dumpcap, "-D"], capture_output=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    lines = listing.stdout.decode("utf-8", "replace").splitlines()
    return [match.group(1) for match in (re.match(r"\d+\.\s+(\S+)", line) for line in lines) if match]


def network_cards(dumpcap):
    """dumpcap's interfaces that are real network cards, the only place PPPoE can be seen (Windows)."""
    if os.name != "nt":
        return []
    names = adapter_names()
    return [interface for interface in listed_interfaces(dumpcap)
            if is_hardware(names.get(adapter_guid(interface), ("", ""))[1])]


def capture_filter(port, hosts=(), pppoe=False):
    """libpcap only looks inside a PPPoE session when told to."""
    return (("pppoes and " if pppoe else "") + f"tcp port {port}"
            + (" and (" + " or ".join(f"host {h}" for h in hosts) + ")" if hosts else ""))


def count_pppoe(dumpcap, interface, seconds=PROBE_SECONDS):
    """PPPoE packets seen on an adapter in `seconds`; None if it cannot be captured on."""
    try:
        probe = subprocess.run([dumpcap, "-i", interface, "-f", "pppoes", "-a", f"duration:{seconds}", "-w", "-"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=seconds + 10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    counts = re.findall(r"Packets(?: captured)?: (\d+)", probe.stderr.decode("utf-8", "replace"))
    return int(counts[-1]) if counts else None


def adapter_report(dumpcap):
    """Lines for the diagnostics: the adapter in use and, when it is no network card, what the cards see.

    The group chat keeps 500 characters and is public: short, and no address.
    """
    try:
        interface = default_interface()[0]
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return ["上网网卡：没有找到"]
    if os.name != "nt":
        return [f"上网网卡：{interface}"]
    try:
        names = adapter_names()
    except OSError:
        names = {}
    name, device = names.get(adapter_guid(interface), ("未知", ""))
    lines = [f"上网网卡：{name}（{chr(92).join(device.split(chr(92))[:2]) or '无设备号'}）"]
    if dumpcap and not is_hardware(device):
        seen = []
        for card in network_cards(dumpcap)[:PROBE_ADAPTERS]:
            count = count_pppoe(dumpcap, card)
            seen.append(f"{names.get(adapter_guid(card), ('未知',))[0]} "
                        + ("打不开" if count is None else f"{count} 个"))
        lines.append(f"物理网卡 {PROBE_SECONDS} 秒内的拨号包：" + ("；".join(seen) or "没有物理网卡"))
    return lines


class Capture:
    """dumpcap in the background; `on_packet(time, linktype, packet)` runs on its reader thread."""

    def __init__(self, on_packet, interface=None, all_hosts=False, dumpcap=None):
        self.on_packet, self.interface, self.all_hosts, self.dumpcap = on_packet, interface, all_hosts, dumpcap
        self.follows_route = interface is None  # an adapter asked for by name stays
        self.route, self.hosts = None, []
        self.pppoe = False  # reading a dial-up connection on the network cards below it
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
            try:
                self.route = default_interface()
            except OSError as exc:  # no network at all
                raise RuntimeError(f"no adapter found for the default route: {exc}") from exc
            self._open(self.route)
        else:
            self.hosts = interface_addresses(self.interface) if os.name != "nt" else []
            self._run(timeout)

    def _open(self, route):
        """Capture on the adapter of `route`, or on the network cards below it (dial-up); RuntimeError if neither."""
        self.interface, self.hosts, self.pppoe = route[0], [route[1]], False
        try:
            self._run(settle=SETTLE)
            return
        except (OSError, RuntimeError) as exc:
            failed = exc
        print(f"cannot capture on {route[0]}: {failed}", flush=True)
        cards = network_cards(self.dumpcap)
        if not cards:
            raise failed
        self.pppoe = True
        for choice in ([cards] if len(cards) > 1 else []) + [[card] for card in cards]:
            self.interface = tuple(choice)
            try:
                self._run(settle=SETTLE)
                print("dial-up connection: reading its packets on the network card", flush=True)
                return
            except (OSError, RuntimeError) as exc:
                print(f"cannot capture on {', '.join(choice)}: {exc}", flush=True)
        raise RuntimeError(f"cannot capture on the network adapter: {failed}") from failed

    def _run(self, timeout=15, settle=0):
        """Start dumpcap on self.interface and self.hosts; RuntimeError if it does not run for `settle` seconds."""
        bpf = capture_filter(GAME_PORT, () if self.all_hosts else self.hosts, self.pppoe)
        interfaces = [self.interface] if isinstance(self.interface, str) else self.interface
        print(f"capture interface: {', '.join(interfaces)} filter: {bpf}", flush=True)
        self.error = None
        self.stderr.clear()
        selected = [option for interface in interfaces for option in ("-i", interface, "-f", bpf)]
        self.process = subprocess.Popen([self.dumpcap, "-q", *selected, "-w", "-"],
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
        An adapter dumpcap cannot capture on (some VPNs) and no network card to read instead:
        back to the one that worked.
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
        working = self.interface, self.hosts, self.pppoe
        self.close()
        try:
            self._open(route)
        except (OSError, RuntimeError):
            self.interface, self.hosts, self.pppoe = working
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
