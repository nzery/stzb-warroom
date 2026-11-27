"""Windows built-in packet capture via Packet Monitor (PktMon) and ETW.

Uses Windows 10 (2004+) / Windows 11's inbox PktMon driver and Event Tracing for Windows (ETW) so
players do not need to install Wireshark or Npcap. Read-only: packets are only copied from the
kernel's trace stream; nothing is modified, delayed or injected.

When the main program runs as a normal user (the usual case for the Electron window), only a tiny
helper subprocess is elevated via UAC (`ShellExecuteExW("runas")`) and streams captured frames back
over an authenticated loopback socket. When already elevated, the session runs in-process.
"""

import collections
import hmac
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

GAME_PORT = 8001

# ETW provider: Microsoft-Windows-PktMon {4D4F80D9-C8BD-4D73-BB5B-19C90402C5AC}
PKTMON_PROVIDER_UUID = (0x4D4F80D9, 0xC8BD, 0x4D73, (0xBB, 0x5B, 0x19, 0xC9, 0x04, 0x02, 0xC5, 0xAC))
SESSION_UUID = (0x9A7C5E31, 0x8F24, 0x4B6D, (0xA1, 0xE3, 0x7C, 0x4B, 0x8D, 0x92, 0xE5, 0xF1))
SESSION_NAME = "stzb-warroom-pktmon"
FILTER_NAME = "stzb-warroom"

PKTMON_EVENT_FRAME_PAYLOAD = 160
PKTMON_KEYWORD_PAYLOAD = 0x10
PKTMON_TYPE_ETHERNET = 1
PKTMON_TYPE_IP = 3

# PktMon PacketType -> pcap linktype understood by pcap.tcp_segment
LINKTYPES = {
    PKTMON_TYPE_ETHERNET: 1,
    PKTMON_TYPE_IP: 101,
}

# Loopback IPC framing between the elevated helper and the main process: <dHI + payload
RECORD_HEADER = struct.Struct("<dHI")
RECORD_READY = 0xFFFE
RECORD_ERROR = 0xFFFF
MAX_RECORD = 256 * 1024

FILETIME_EPOCH_DIFF = 116444736000000000  # 100ns ticks between 1601-01-01 and 1970-01-01
FLUSH_EVERY = 0.15  # seconds between ETW buffer flushes so tile clicks arrive promptly
SETTLE = 1.5  # seconds to verify pktmon.exe stays running after launch


def find_pktmon():
    """Path of Windows' built-in pktmon.exe, or None on non-Windows / older Windows."""
    if os.name != "nt":
        return None
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidates = [
        os.path.join(system_root, "System32", "pktmon.exe"),
        shutil.which("pktmon") or "",
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def is_admin():
    """Whether this process already holds Windows Administrator privileges."""
    if os.name != "nt":
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def filetime_to_unix(filetime):
    """Convert a Windows FILETIME (100ns since 1601-01-01 UTC) to Unix seconds."""
    if not filetime or filetime < FILETIME_EPOCH_DIFF:
        return time.time()
    return (filetime - FILETIME_EPOCH_DIFF) / 1e7


def encode_record(captured_at, kind, payload=b""):
    return RECORD_HEADER.pack(float(captured_at), int(kind), len(payload)) + payload


def _read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return bytes(data)


def read_record(sock):
    """Read one (captured_at, kind, payload) record from `sock`, or None on EOF."""
    head = _read_exact(sock, RECORD_HEADER.size)
    if head is None:
        return None
    captured_at, kind, length = RECORD_HEADER.unpack(head)
    if length > MAX_RECORD:
        raise ValueError(f"record too large: {length}")
    payload = _read_exact(sock, length) if length else b""
    if payload is None:
        return None
    return captured_at, kind, payload


def parse_frame_payload(data):
    """Fallback binary parser for PktMon Event 160 (PktMon_FramePayload) UserData.

    Layout from Microsoft's etl_Microsoft-Windows-PktMon-Events.npl:
      PktGroupId(Q), PktNumber(H), AppearanceCount(H), DirTag(H), PacketType(H),
      ComponentId(H), EdgeId(H), FilterId(H), DropReason(I), DropLocation(I),
      OriginalPayloadSize(H), LoggedPayloadSize(H), Payload(bytes).
    Returns (pkt_group_id, pkt_number, packet_type, payload) or None.
    """
    if len(data) < 34:
        return None
    group_id, pkt_num, _app, _dir, pkt_type, _comp, _edge, _flt, _dr, _dl, _orig, logged = (
        struct.unpack_from("<QHHHHHHHIIHH", data, 0)
    )
    if logged == 0 or 34 + logged > len(data):
        return None
    return group_id, pkt_num, pkt_type, bytes(data[34:34 + logged])


class PacketDeduper:
    """Drop repeated appearances of the same PktMon packet across NIC/stack edges."""

    def __init__(self, maxlen=512):
        self.maxlen = maxlen
        self.order = collections.deque()
        self.seen = set()

    def accept(self, group_id, pkt_number):
        if not group_id:
            return True
        key = (group_id, pkt_number)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.order.append(key)
        if len(self.order) > self.maxlen:
            self.seen.discard(self.order.popleft())
        return True


def pktmon_start_args(pktmon_path, help_text, comp_nics=True):
    """Build compatible `pktmon start` arguments across Windows 10 (2004+) and Windows 11."""
    lower = (help_text or "").lower()
    args = [pktmon_path, "start"]
    if "--capture" in lower:
        args.append("--capture")
    elif "--etw" in lower:
        args.append("--etw")
    else:
        args.append("--capture")
    if comp_nics and "--comp" in lower:
        args += ["--comp", "nics"]
    if "--pkt-size" in lower:
        args += ["--pkt-size", "0"]
    else:
        args += ["-p", "0"]
    if "--log-mode" in lower:
        args += ["--log-mode", "real-time"]
    else:
        args += ["-m", "real-time"]
    return args


def _decode_console(raw):
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("mbcs" if os.name == "nt" else "utf-8", "replace")


def _run_pktmon(pktmon_path, *args, timeout=15):
    return subprocess.run(
        [pktmon_path, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ---------------------------------------------------------------- Windows ETW session

def _etw_api():
    """Win32 ETW / TDH structures and bindings (64-bit Windows safe)."""
    import ctypes
    import types
    from ctypes import wintypes

    TRACEHANDLE = ctypes.c_uint64
    INVALID_PROCESSTRACE_HANDLE = 0xFFFFFFFFFFFFFFFF
    EVENT_TRACE_REAL_TIME_MODE = 0x00000100
    EVENT_TRACE_INDEPENDENT_SESSION_MODE = 0x08000000
    WNODE_FLAG_TRACED_GUID = 0x00020000
    EVENT_CONTROL_CODE_DISABLE_PROVIDER = 0
    EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
    EVENT_TRACE_CONTROL_STOP = 1
    EVENT_TRACE_CONTROL_FLUSH = 3
    PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
    PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
    TRACE_LEVEL_INFORMATION = 4
    ULONG_MAX = 0xFFFFFFFF

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_tuple(cls, parts):
            return cls(parts[0], parts[1], parts[2], (ctypes.c_ubyte * 8)(*parts[3]))

        def as_tuple(self):
            return (self.Data1, self.Data2, self.Data3, tuple(self.Data4))

    class WNODE_HEADER(ctypes.Structure):
        _fields_ = [
            ("BufferSize", ctypes.c_uint32),
            ("ProviderId", ctypes.c_uint32),
            ("HistoricalContext", ctypes.c_uint64),
            ("TimeStamp", ctypes.c_int64),
            ("Guid", GUID),
            ("ClientContext", ctypes.c_uint32),
            ("Flags", ctypes.c_uint32),
        ]

    class EVENT_TRACE_PROPERTIES(ctypes.Structure):
        _fields_ = [
            ("Wnode", WNODE_HEADER),
            ("BufferSize", ctypes.c_uint32),
            ("MinimumBuffers", ctypes.c_uint32),
            ("MaximumBuffers", ctypes.c_uint32),
            ("MaximumFileSize", ctypes.c_uint32),
            ("LogFileMode", ctypes.c_uint32),
            ("FlushTimer", ctypes.c_uint32),
            ("EnableFlags", ctypes.c_uint32),
            ("AgeLimit", ctypes.c_int32),
            ("NumberOfBuffers", ctypes.c_uint32),
            ("FreeBuffers", ctypes.c_uint32),
            ("EventsLost", ctypes.c_uint32),
            ("BuffersWritten", ctypes.c_uint32),
            ("LogBuffersLost", ctypes.c_uint32),
            ("RealTimeBuffersLost", ctypes.c_uint32),
            ("LoggerThreadId", ctypes.c_void_p),
            ("LogFileNameOffset", ctypes.c_uint32),
            ("LoggerNameOffset", ctypes.c_uint32),
        ]

    class SessionProperties(ctypes.Structure):
        _fields_ = [
            ("properties", EVENT_TRACE_PROPERTIES),
            ("logger_name", ctypes.c_wchar * 256),
        ]

    class EVENT_TRACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("Size", ctypes.c_uint16),
            ("FieldTypeFlags", ctypes.c_uint16),
            ("Class", ctypes.c_uint32),
            ("ThreadId", ctypes.c_uint32),
            ("ProcessId", ctypes.c_uint32),
            ("TimeStamp", ctypes.c_int64),
            ("Guid", GUID),
            ("KernelAndUserTime", ctypes.c_uint64),
        ]

    class EVENT_TRACE(ctypes.Structure):
        _fields_ = [
            ("Header", EVENT_TRACE_HEADER),
            ("InstanceId", ctypes.c_uint32),
            ("ParentInstanceId", ctypes.c_uint32),
            ("ParentGuid", GUID),
            ("MofData", ctypes.c_void_p),
            ("MofLength", ctypes.c_uint32),
            ("BufferContext", ctypes.c_uint32),
        ]

    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [
            ("wYear", ctypes.c_uint16),
            ("wMonth", ctypes.c_uint16),
            ("wDayOfWeek", ctypes.c_uint16),
            ("wDay", ctypes.c_uint16),
            ("wHour", ctypes.c_uint16),
            ("wMinute", ctypes.c_uint16),
            ("wSecond", ctypes.c_uint16),
            ("wMilliseconds", ctypes.c_uint16),
        ]

    class TIME_ZONE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Bias", ctypes.c_int32),
            ("StandardName", ctypes.c_wchar * 32),
            ("StandardDate", SYSTEMTIME),
            ("StandardBias", ctypes.c_int32),
            ("DaylightName", ctypes.c_wchar * 32),
            ("DaylightDate", SYSTEMTIME),
            ("DaylightBias", ctypes.c_int32),
        ]

    class TRACE_LOGFILE_HEADER(ctypes.Structure):
        _fields_ = [
            ("BufferSize", ctypes.c_uint32),
            ("Version", ctypes.c_uint32),
            ("ProviderVersion", ctypes.c_uint32),
            ("NumberOfProcessors", ctypes.c_uint32),
            ("EndTime", ctypes.c_int64),
            ("TimerResolution", ctypes.c_uint32),
            ("MaximumFileSize", ctypes.c_uint32),
            ("LogFileMode", ctypes.c_uint32),
            ("BuffersWritten", ctypes.c_uint32),
            ("LogInstanceGuid", GUID),
            ("LoggerName", wintypes.LPWSTR),
            ("LogFileName", wintypes.LPWSTR),
            ("TimeZone", TIME_ZONE_INFORMATION),
            ("BootTime", ctypes.c_int64),
            ("PerfFreq", ctypes.c_int64),
            ("StartTime", ctypes.c_int64),
            ("ReservedFlags", ctypes.c_uint32),
            ("BuffersLost", ctypes.c_uint32),
        ]

    class EVENT_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("Id", ctypes.c_uint16),
            ("Version", ctypes.c_uint8),
            ("Channel", ctypes.c_uint8),
            ("Level", ctypes.c_uint8),
            ("Opcode", ctypes.c_uint8),
            ("Task", ctypes.c_uint16),
            ("Keyword", ctypes.c_uint64),
        ]

    class EVENT_HEADER(ctypes.Structure):
        _fields_ = [
            ("Size", ctypes.c_uint16),
            ("HeaderType", ctypes.c_uint16),
            ("Flags", ctypes.c_uint16),
            ("EventProperty", ctypes.c_uint16),
            ("ThreadId", ctypes.c_uint32),
            ("ProcessId", ctypes.c_uint32),
            ("TimeStamp", ctypes.c_int64),
            ("ProviderId", GUID),
            ("EventDescriptor", EVENT_DESCRIPTOR),
            ("ProcessorTime", ctypes.c_uint64),
            ("ActivityId", GUID),
        ]

    class ETW_BUFFER_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("ProcessorIndex", ctypes.c_uint16),
            ("LoggerId", ctypes.c_uint16),
        ]

    class EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("EventHeader", EVENT_HEADER),
            ("BufferContext", ETW_BUFFER_CONTEXT),
            ("ExtendedDataCount", ctypes.c_uint16),
            ("UserDataLength", ctypes.c_uint16),
            ("ExtendedData", ctypes.c_void_p),
            ("UserData", ctypes.c_void_p),
            ("UserContext", ctypes.c_void_p),
        ]

    EVENT_RECORD_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.POINTER(EVENT_RECORD))
    BUFFER_CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p)

    class EVENT_TRACE_LOGFILEW(ctypes.Structure):
        _fields_ = [
            ("LogFileName", wintypes.LPWSTR),
            ("LoggerName", wintypes.LPWSTR),
            ("CurrentTime", ctypes.c_int64),
            ("BuffersRead", ctypes.c_uint32),
            ("ProcessTraceMode", ctypes.c_uint32),
            ("CurrentEvent", EVENT_TRACE),
            ("LogfileHeader", TRACE_LOGFILE_HEADER),
            ("BufferCallback", BUFFER_CALLBACK),
            ("BufferSize", ctypes.c_uint32),
            ("Filled", ctypes.c_uint32),
            ("EventsLost", ctypes.c_uint32),
            ("EventRecordCallback", EVENT_RECORD_CALLBACK),
            ("IsKernelTrace", ctypes.c_uint32),
            ("Context", ctypes.c_void_p),
        ]

    class PROPERTY_DATA_DESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("PropertyName", ctypes.c_uint64),
            ("ArrayIndex", ctypes.c_uint32),
            ("Reserved", ctypes.c_uint32),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    tdh = ctypes.WinDLL("tdh", use_last_error=True)

    advapi32.StartTraceW.argtypes = [ctypes.POINTER(TRACEHANDLE), wintypes.LPCWSTR, ctypes.POINTER(EVENT_TRACE_PROPERTIES)]
    advapi32.StartTraceW.restype = ctypes.c_uint32
    advapi32.ControlTraceW.argtypes = [TRACEHANDLE, wintypes.LPCWSTR, ctypes.POINTER(EVENT_TRACE_PROPERTIES), ctypes.c_uint32]
    advapi32.ControlTraceW.restype = ctypes.c_uint32
    advapi32.EnableTraceEx2.argtypes = [
        TRACEHANDLE, ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_uint8,
        ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32, ctypes.c_void_p,
    ]
    advapi32.EnableTraceEx2.restype = ctypes.c_uint32
    advapi32.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
    advapi32.OpenTraceW.restype = TRACEHANDLE
    advapi32.ProcessTrace.argtypes = [ctypes.POINTER(TRACEHANDLE), ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.ProcessTrace.restype = ctypes.c_uint32
    advapi32.CloseTrace.argtypes = [TRACEHANDLE]
    advapi32.CloseTrace.restype = ctypes.c_uint32

    tdh.TdhGetProperty.argtypes = [
        ctypes.POINTER(EVENT_RECORD), ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(PROPERTY_DATA_DESCRIPTOR),
        ctypes.c_uint32, ctypes.c_void_p,
    ]
    tdh.TdhGetProperty.restype = ctypes.c_uint32

    return types.SimpleNamespace(
        ctypes=ctypes, advapi32=advapi32, tdh=tdh,
        TRACEHANDLE=TRACEHANDLE, INVALID_PROCESSTRACE_HANDLE=INVALID_PROCESSTRACE_HANDLE,
        EVENT_TRACE_REAL_TIME_MODE=EVENT_TRACE_REAL_TIME_MODE,
        EVENT_TRACE_INDEPENDENT_SESSION_MODE=EVENT_TRACE_INDEPENDENT_SESSION_MODE,
        WNODE_FLAG_TRACED_GUID=WNODE_FLAG_TRACED_GUID,
        EVENT_CONTROL_CODE_DISABLE_PROVIDER=EVENT_CONTROL_CODE_DISABLE_PROVIDER,
        EVENT_CONTROL_CODE_ENABLE_PROVIDER=EVENT_CONTROL_CODE_ENABLE_PROVIDER,
        EVENT_TRACE_CONTROL_STOP=EVENT_TRACE_CONTROL_STOP,
        EVENT_TRACE_CONTROL_FLUSH=EVENT_TRACE_CONTROL_FLUSH,
        PROCESS_TRACE_MODE_REAL_TIME=PROCESS_TRACE_MODE_REAL_TIME,
        PROCESS_TRACE_MODE_EVENT_RECORD=PROCESS_TRACE_MODE_EVENT_RECORD,
        TRACE_LEVEL_INFORMATION=TRACE_LEVEL_INFORMATION,
        ULONG_MAX=ULONG_MAX,
        GUID=GUID, EVENT_TRACE_PROPERTIES=EVENT_TRACE_PROPERTIES,
        SessionProperties=SessionProperties, EVENT_TRACE_LOGFILEW=EVENT_TRACE_LOGFILEW,
        EVENT_RECORD=EVENT_RECORD, PROPERTY_DATA_DESCRIPTOR=PROPERTY_DATA_DESCRIPTOR,
        EVENT_RECORD_CALLBACK=EVENT_RECORD_CALLBACK, BUFFER_CALLBACK=BUFFER_CALLBACK,
    )


class PktMonSession:
    """In-process privileged PktMon + ETW real-time capture session (Windows Administrator)."""

    def __init__(self, on_packet, port=GAME_PORT, pktmon=None):
        self.on_packet = on_packet
        self.port = int(port)
        self.pktmon = pktmon or find_pktmon()
        self.error = None
        self._api = None
        self._session_handle = 0
        self._trace_handle = 0
        self._proc = None
        self._output = collections.deque(maxlen=20)
        self._stop = threading.Event()
        self._threads = []
        self._dedup = PacketDeduper()
        self._active = False

    def _new_props(self, independent=True):
        api = self._api
        buf = api.SessionProperties()
        props = buf.properties
        props.Wnode.BufferSize = api.ctypes.sizeof(buf)
        props.Wnode.Flags = api.WNODE_FLAG_TRACED_GUID
        props.Wnode.ClientContext = 2  # FILETIME (SystemTime)
        props.Wnode.Guid = api.GUID.from_tuple(SESSION_UUID)
        props.BufferSize = 64  # KB
        props.MinimumBuffers = 8
        props.MaximumBuffers = 64
        props.LogFileMode = api.EVENT_TRACE_REAL_TIME_MODE | (
            api.EVENT_TRACE_INDEPENDENT_SESSION_MODE if independent else 0
        )
        props.FlushTimer = 1
        props.LoggerNameOffset = api.ctypes.sizeof(api.EVENT_TRACE_PROPERTIES)
        props.LogFileNameOffset = 0
        buf.logger_name = SESSION_NAME
        return buf

    def _stop_etw_session(self):
        if self._api is None:
            return
        api = self._api
        if self._session_handle:
            provider = api.GUID.from_tuple(PKTMON_PROVIDER_UUID)
            api.advapi32.EnableTraceEx2(
                self._session_handle, api.ctypes.byref(provider),
                api.EVENT_CONTROL_CODE_DISABLE_PROVIDER, api.TRACE_LEVEL_INFORMATION,
                PKTMON_KEYWORD_PAYLOAD, 0, 0, None,
            )
        buf = self._new_props()
        api.advapi32.ControlTraceW(
            api.TRACEHANDLE(self._session_handle), SESSION_NAME,
            api.ctypes.byref(buf.properties), api.EVENT_TRACE_CONTROL_STOP,
        )
        self._session_handle = 0

    def _start_etw(self):
        api = self._api = _etw_api()
        ctypes = api.ctypes
        # Clean up any orphaned session from a previous crash.
        self._stop_etw_session()
        handle = api.TRACEHANDLE(0)
        buf = self._new_props(independent=True)
        err = api.advapi32.StartTraceW(ctypes.byref(handle), SESSION_NAME, ctypes.byref(buf.properties))
        if err == 87:  # ERROR_INVALID_PARAMETER: retry without INDEPENDENT_SESSION_MODE
            buf = self._new_props(independent=False)
            err = api.advapi32.StartTraceW(ctypes.byref(handle), SESSION_NAME, ctypes.byref(buf.properties))
        if err != 0:
            raise RuntimeError(f"pktmon StartTraceW failed: WinError {err}")
        self._session_handle = handle.value
        provider = api.GUID.from_tuple(PKTMON_PROVIDER_UUID)
        err = api.advapi32.EnableTraceEx2(
            handle, ctypes.byref(provider), api.EVENT_CONTROL_CODE_ENABLE_PROVIDER,
            api.TRACE_LEVEL_INFORMATION, PKTMON_KEYWORD_PAYLOAD, 0, 0, None,
        )
        if err != 0:
            self._stop_etw_session()
            raise RuntimeError(f"pktmon EnableTraceEx2 failed: WinError {err}")

        # Pre-allocate TDH property descriptors to avoid per-event allocations.
        self._prop_bufs = {}
        self._prop_descs = {}
        for name in ("PktGroupId", "PktNumber", "PacketType", "LoggedPayloadSize", "Payload"):
            wbuf = ctypes.create_unicode_buffer(name)
            self._prop_bufs[name] = wbuf
            self._prop_descs[name] = api.PROPERTY_DATA_DESCRIPTOR(ctypes.addressof(wbuf), api.ULONG_MAX, 0)

        self._record_cb = api.EVENT_RECORD_CALLBACK(self._on_event)
        self._buffer_cb = api.BUFFER_CALLBACK(self._on_buffer)
        self._logger_name_buf = ctypes.create_unicode_buffer(SESSION_NAME)
        self._logfile = api.EVENT_TRACE_LOGFILEW()
        self._logfile.LoggerName = ctypes.cast(self._logger_name_buf, ctypes.c_wchar_p)
        self._logfile.ProcessTraceMode = api.PROCESS_TRACE_MODE_REAL_TIME | api.PROCESS_TRACE_MODE_EVENT_RECORD
        self._logfile.EventRecordCallback = self._record_cb
        self._logfile.BufferCallback = self._buffer_cb

        trace_handle = api.advapi32.OpenTraceW(ctypes.byref(self._logfile))
        if trace_handle == api.INVALID_PROCESSTRACE_HANDLE:
            err = ctypes.get_last_error()
            self._stop_etw_session()
            raise RuntimeError(f"pktmon OpenTraceW failed: WinError {err}")
        self._trace_handle = trace_handle

        consumer = threading.Thread(target=self._run_trace, name="pktmon-etw", daemon=True)
        flusher = threading.Thread(target=self._run_flusher, name="pktmon-flush", daemon=True)
        self._threads = [consumer, flusher]
        consumer.start()
        flusher.start()

    def _tdh_uint(self, record_ptr, name, size):
        api = self._api
        buf = (api.ctypes.c_ubyte * size)()
        err = api.tdh.TdhGetProperty(
            record_ptr, 0, None, 1, api.ctypes.byref(self._prop_descs[name]), size, buf,
        )
        if err != 0:
            return None
        return int.from_bytes(bytes(buf), "little")

    def _extract_event(self, record_ptr):
        api = self._api
        ctypes = api.ctypes
        pkt_type = self._tdh_uint(record_ptr, "PacketType", 4)
        logged = self._tdh_uint(record_ptr, "LoggedPayloadSize", 4)
        if pkt_type is not None and logged is not None and 0 < logged <= MAX_RECORD:
            linktype = LINKTYPES.get(pkt_type)
            if linktype is None:
                return None
            payload_buf = (ctypes.c_ubyte * logged)()
            err = api.tdh.TdhGetProperty(
                record_ptr, 0, None, 1, ctypes.byref(self._prop_descs["Payload"]), logged, payload_buf,
            )
            if err == 0:
                group_id = self._tdh_uint(record_ptr, "PktGroupId", 8) or 0
                pkt_num = self._tdh_uint(record_ptr, "PktNumber", 4) or 0
                return group_id, pkt_num, linktype, bytes(payload_buf)
        # Fallback: parse UserData directly if TDH failed on this build.
        rec = record_ptr.contents
        if rec.UserData and rec.UserDataLength >= 34:
            raw = ctypes.string_at(rec.UserData, rec.UserDataLength)
            parsed = parse_frame_payload(raw)
            if parsed is not None:
                group_id, pkt_num, pkt_type, payload = parsed
                linktype = LINKTYPES.get(pkt_type)
                if linktype is not None:
                    return group_id, pkt_num, linktype, payload
        return None

    def _on_event(self, record_ptr):
        try:
            if not record_ptr or self._stop.is_set():
                return
            header = record_ptr.contents.EventHeader
            if header.EventDescriptor.Id != PKTMON_EVENT_FRAME_PAYLOAD:
                return
            if header.ProviderId.as_tuple() != PKTMON_PROVIDER_UUID:
                return
            extracted = self._extract_event(record_ptr)
            if extracted is None:
                return
            group_id, pkt_num, linktype, payload = extracted
            if not self._dedup.accept(group_id, pkt_num):
                return
            self.on_packet(filetime_to_unix(header.TimeStamp), linktype, payload)
        except Exception as exc:
            if self.error is None:
                self.error = exc

    def _on_buffer(self, _logfile_ptr):
        return 0 if self._stop.is_set() else 1

    def _run_trace(self):
        api = self._api
        handle = api.TRACEHANDLE(self._trace_handle)
        err = api.advapi32.ProcessTrace(api.ctypes.byref(handle), 1, None, None)
        if err not in (0, 1223) and not self._stop.is_set() and self.error is None:
            self.error = RuntimeError(f"pktmon ProcessTrace stopped: WinError {err}")

    def _run_flusher(self):
        api = self._api
        while not self._stop.wait(FLUSH_EVERY):
            if not self._session_handle:
                break
            buf = self._new_props()
            api.advapi32.ControlTraceW(
                api.TRACEHANDLE(self._session_handle), SESSION_NAME,
                api.ctypes.byref(buf.properties), api.EVENT_TRACE_CONTROL_FLUSH,
            )

    def _drain_output(self, proc):
        try:
            for raw in proc.stdout:
                line = _decode_console(raw).strip()
                if line:
                    self._output.append(line)
        except OSError:
            pass
        if not self._stop.is_set() and self.error is None:
            self.error = RuntimeError("pktmon stopped: " + " | ".join(self._output))

    def start(self, timeout=15):
        if not self.pktmon or not os.path.isfile(self.pktmon):
            raise RuntimeError("pktmon not found on this Windows version")
        # Reset any leftover pktmon session/filters from an unclean exit.
        try:
            _run_pktmon(self.pktmon, "stop", timeout=10)
            _run_pktmon(self.pktmon, "filter", "remove", timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        added = _run_pktmon(self.pktmon, "filter", "add", FILTER_NAME, "-t", "TCP", "-p", str(self.port), timeout=10)
        if added.returncode != 0:
            detail = (_decode_console(added.stderr) or _decode_console(added.stdout)).strip()
            raise RuntimeError(f"pktmon filter add failed: {detail or added.returncode}")

        help_res = _run_pktmon(self.pktmon, "start", "--help", timeout=10)
        help_text = _decode_console(help_res.stdout) + "\n" + _decode_console(help_res.stderr)

        self._start_etw()
        last_err = None
        for comp_nics in (True, False):
            cmd = pktmon_start_args(self.pktmon, help_text, comp_nics=comp_nics)
            self._output.clear()
            self.error = None
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._proc = proc
            reader = threading.Thread(target=self._drain_output, args=(proc,), name="pktmon-out", daemon=True)
            reader.start()
            self._threads.append(reader)
            try:
                proc.wait(SETTLE)
            except subprocess.TimeoutExpired:
                self._active = True
                print(f"capture backend: pktmon ({'nics' if comp_nics else 'all'}) port {self.port}", flush=True)
                return
            last_err = " | ".join(self._output) or f"exit {proc.returncode}"
            try:
                _run_pktmon(self.pktmon, "stop", timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        self.close()
        raise RuntimeError(f"pktmon did not start capturing: {last_err}")

    def check(self):
        if self.error is not None:
            raise RuntimeError(f"capture failed: {self.error}") from self.error
        if self._proc is not None and self._proc.poll() is not None:
            raise RuntimeError("capture failed: pktmon exited: " + " | ".join(self._output))

    def follow(self):
        # PktMon monitors all active NICs in the kernel; network switches need no restart.
        return False

    def close(self):
        self._stop.set()
        if self.pktmon and os.path.isfile(self.pktmon):
            try:
                _run_pktmon(self.pktmon, "stop", timeout=8)
            except (OSError, subprocess.SubprocessError):
                pass
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(2)
            try:
                self._proc.stdout.close()
            except OSError:
                pass
            self._proc = None
        if self.pktmon and os.path.isfile(self.pktmon):
            try:
                _run_pktmon(self.pktmon, "filter", "remove", timeout=8)
            except (OSError, subprocess.SubprocessError):
                pass
        if self._api is not None and self._trace_handle:
            self._api.advapi32.CloseTrace(self._api.TRACEHANDLE(self._trace_handle))
            self._trace_handle = 0
        self._stop_etw_session()
        for thread in self._threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=3)
        self._threads = []
        self._active = False


# ---------------------------------------------------------------- Elevated helper IPC

def _elevate(executable, args):
    """Launch `executable` with `args` elevated via Windows UAC (`runas`), hidden -> process HANDLE."""
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    SW_HIDE = 0

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(executable)
    info.lpParameters = subprocess.list2cmdline([str(a) for a in args])
    info.nShow = SW_HIDE
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:  # ERROR_CANCELLED
            raise RuntimeError("pktmon uac cancelled: administrator permission was declined")
        raise RuntimeError(f"pktmon elevate failed: WinError {err}")
    return info.hProcess


def _process_exited(handle):
    if not handle or os.name != "nt":
        return False
    import ctypes
    return ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0


def _close_handle(handle):
    if handle and os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def _write_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle)


class PktMonCapture:
    """Drop-in alternative to capture.Capture using Windows PktMon (in-process if admin, else UAC helper)."""

    def __init__(self, on_packet, port=GAME_PORT, state_dir=None, pktmon=None):
        self.on_packet = on_packet
        self.port = int(port)
        self.state_dir = Path(state_dir) if state_dir else Path.home()
        self.pktmon = pktmon or find_pktmon()
        self._direct = None
        self._listener = None
        self._conn = None
        self._helper_handle = None
        self._config_path = None
        self._thread = None
        self._stop = threading.Event()
        self.error = None

    def start(self, timeout=20):
        if not self.pktmon or not os.path.isfile(self.pktmon):
            raise RuntimeError("pktmon not found on this Windows version")
        if is_admin():
            self._direct = PktMonSession(self.on_packet, port=self.port, pktmon=self.pktmon)
            self._direct.start(timeout=timeout)
            return

        token = secrets.token_hex(32)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(2)
        ipc_port = self._listener.getsockname()[1]

        self._config_path = self.state_dir / f"pktmon-{secrets.token_hex(8)}.json"
        _write_private_json(self._config_path, {
            "port": ipc_port, "token": token, "pid": os.getpid(), "game_port": self.port,
        })
        helper_args = (["pktmon-helper", "--config", str(self._config_path)]
                       if getattr(sys, "frozen", False)
                       else ["-m", "stzb_warroom", "pktmon-helper", "--config", str(self._config_path)])
        try:
            self._helper_handle = _elevate(sys.executable, helper_args)
            self._conn = self._accept_helper(token, timeout=timeout)
            self._conn.settimeout(timeout)
            first = read_record(self._conn)
            if first is None:
                raise RuntimeError("pktmon helper exited before starting")
            _at, kind, payload = first
            if kind == RECORD_ERROR:
                raise RuntimeError(payload.decode("utf-8", "replace") or "pktmon helper failed")
            if kind != RECORD_READY:
                raise RuntimeError(f"pktmon helper sent unexpected handshake {kind}")
            self._conn.settimeout(None)
            self._thread = threading.Thread(target=self._read_loop, name="pktmon-ipc", daemon=True)
            self._thread.start()
            print(f"capture backend: pktmon (elevated helper) port {self.port}", flush=True)
        except Exception:
            self.close()
            raise

    def _accept_helper(self, token, timeout):
        deadline = time.monotonic() + timeout
        expected = token.encode("ascii")
        while time.monotonic() < deadline:
            if _process_exited(self._helper_handle):
                raise RuntimeError("pktmon helper exited before connecting")
            self._listener.settimeout(min(0.5, max(0.05, deadline - time.monotonic())))
            try:
                conn, _addr = self._listener.accept()
            except socket.timeout:
                continue
            try:
                conn.settimeout(5)
                line = b""
                while len(line) < len(expected) + 1:
                    chunk = conn.recv(len(expected) + 1 - len(line))
                    if not chunk:
                        break
                    line += chunk
                if line.endswith(b"\n") and hmac.compare_digest(line[:-1], expected):
                    return conn
            except OSError:
                pass
            conn.close()
        raise RuntimeError("pktmon helper timed out waiting to connect")

    def _read_loop(self):
        try:
            while not self._stop.is_set():
                record = read_record(self._conn)
                if record is None:
                    break
                captured_at, kind, payload = record
                if kind == RECORD_ERROR:
                    self.error = RuntimeError(payload.decode("utf-8", "replace") or "pktmon helper error")
                    return
                if kind < 0xFF00:
                    self.on_packet(captured_at, kind, payload)
        except Exception as exc:
            if not self._stop.is_set():
                self.error = exc
        finally:
            if not self._stop.is_set() and self.error is None:
                self.error = RuntimeError("pktmon capture stopped")

    def check(self):
        if self._direct is not None:
            self._direct.check()
            return
        if self.error is not None:
            raise RuntimeError(f"capture failed: {self.error}") from self.error

    def follow(self):
        return False

    def close(self):
        self._stop.set()
        if self._direct is not None:
            self._direct.close()
            self._direct = None
        if self._conn is not None:
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
            self._thread = None
        if self._helper_handle is not None:
            if os.name == "nt":
                import ctypes
                ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(self._helper_handle), 5000)
            _close_handle(self._helper_handle)
            self._helper_handle = None
        if self._config_path is not None:
            try:
                self._config_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._config_path = None


def run_helper(config_path):
    """Entry point for the elevated `pktmon-helper` subprocess."""
    path = Path(config_path)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    port = int(cfg["port"])
    token = str(cfg["token"])
    parent_pid = int(cfg.get("pid") or 0)
    game_port = int(cfg.get("game_port") or GAME_PORT)

    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(None)
    send_lock = threading.Lock()
    stop = threading.Event()

    def send_raw(data):
        with send_lock:
            sock.sendall(data)

    send_raw((token + "\n").encode("ascii"))

    def watch_socket():
        try:
            while not stop.is_set():
                if not sock.recv(16):
                    break
        except OSError:
            pass
        stop.set()

    def watch_parent():
        if not parent_pid or os.name != "nt":
            return
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        if not handle:
            stop.set()
            return
        try:
            while not stop.is_set():
                if kernel32.WaitForSingleObject(handle, 500) == 0:
                    stop.set()
                    return
        finally:
            kernel32.CloseHandle(handle)

    threading.Thread(target=watch_socket, daemon=True).start()
    threading.Thread(target=watch_parent, daemon=True).start()

    def forward(captured_at, linktype, packet):
        if stop.is_set():
            return
        try:
            send_raw(encode_record(captured_at, linktype, packet))
        except OSError:
            stop.set()

    session = PktMonSession(forward, port=game_port)
    try:
        try:
            session.start()
        except Exception as exc:
            try:
                send_raw(encode_record(time.time(), RECORD_ERROR, str(exc).encode("utf-8", "replace")))
            except OSError:
                pass
            return
        send_raw(encode_record(time.time(), RECORD_READY))
        while not stop.wait(0.5):
            try:
                session.check()
            except Exception as exc:
                try:
                    send_raw(encode_record(time.time(), RECORD_ERROR, str(exc).encode("utf-8", "replace")))
                except OSError:
                    pass
                break
    finally:
        stop.set()
        session.close()
        try:
            sock.close()
        except OSError:
            pass
