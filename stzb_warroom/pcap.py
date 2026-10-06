"""pcap / pcapng stream reader (dumpcap writes pcapng to a pipe) and TCP/IP parsing."""

import socket
import struct


def _read_exact(stream, size):
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def read_timed_packets(stream):
    """Yield (unix time in seconds or None, linktype, packet bytes) from a pcap or pcapng stream."""
    head = _read_exact(stream, 4)
    if head is None:
        return
    if head == b"\x0a\x0d\x0d\x0a":
        yield from _read_pcapng(stream, head)
    elif head in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        yield from _read_pcap(stream, head)
    else:
        raise ValueError("not a pcap or pcapng capture")


def _read_pcap(stream, magic):
    order = "<" if magic[0] in (0xD4, 0x4D) else ">"
    tick = 1e-9 if magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d") else 1e-6
    header = _read_exact(stream, 20)
    if header is None:
        return
    linktype = struct.unpack(order + "HHiIII", header)[5] & 0x0FFFFFFF
    while True:
        record = _read_exact(stream, 16)
        if record is None:
            return
        seconds, fraction, captured, _length = struct.unpack(order + "IIII", record)
        data = _read_exact(stream, captured)
        if data is None:
            return
        yield seconds + fraction * tick, linktype, data


def _timestamp_tick(options, order):
    """Seconds per timestamp unit from an interface's if_tsresol option (default 1e-6)."""
    offset = 0
    while offset + 4 <= len(options):
        code, length = struct.unpack_from(order + "HH", options, offset)
        if code == 0:
            break
        if code == 9 and length >= 1:
            resolution = options[offset + 4]
            return 2.0 ** -(resolution & 0x7F) if resolution & 0x80 else 10.0 ** -resolution
        offset += 4 + length + (-length % 4)
    return 1e-6


def _read_pcapng(stream, first):
    order, linktypes, ticks = "<", [], []
    block_type = first
    while True:
        length_raw = _read_exact(stream, 4)
        if length_raw is None:
            return
        if block_type == b"\x0a\x0d\x0d\x0a":
            magic = _read_exact(stream, 4)
            if magic is None:
                return
            order = "<" if magic == b"\x4d\x3c\x2b\x1a" else ">"
            length = struct.unpack(order + "I", length_raw)[0]
            if _read_exact(stream, length - 12) is None:
                return
            linktypes, ticks = [], []  # a new section restarts interface numbering
        else:
            kind = struct.unpack(order + "I", block_type)[0]
            length = struct.unpack(order + "I", length_raw)[0]
            body = _read_exact(stream, length - 8)
            if body is None:
                return
            if kind == 1:  # interface description
                linktypes.append(struct.unpack_from(order + "H", body)[0])
                ticks.append(_timestamp_tick(body[8:-4], order))
            elif kind == 6:  # enhanced packet
                interface, high, low, captured = struct.unpack_from(order + "IIII", body)
                if interface < len(linktypes):
                    yield ((high << 32 | low) * ticks[interface], linktypes[interface],
                           body[20:20 + captured])
            elif kind == 3 and linktypes:  # simple packet, no timestamp
                yield None, linktypes[0], body[4:length - 8 - 4]
        block_type = _read_exact(stream, 4)
        if block_type is None:
            return


def tcp_segment(linktype, packet):
    """(src, sport, dst, dport, seq, syn, payload, end) of a TCP/IP packet, else None;
    `end` when it carries FIN or RST (the connection is closing)."""
    if linktype == 1:  # Ethernet, with optional VLAN tags
        if len(packet) < 14:
            return None
        offset, ethertype = 14, packet[12:14]
        while ethertype in (b"\x81\x00", b"\x88\xa8") and len(packet) >= offset + 4:
            ethertype, offset = packet[offset + 2:offset + 4], offset + 4
        if ethertype not in (b"\x08\x00", b"\x86\xdd"):
            return None
    elif linktype == 0:  # BSD loopback (Npcap loopback adapter)
        offset = 4
    elif linktype in (113, 276):  # Linux cooked capture v1/v2 (dumpcap -i any)
        offset, protocol_at = (16, 14) if linktype == 113 else (20, 0)
        if len(packet) < offset or packet[protocol_at:protocol_at + 2] not in (b"\x08\x00", b"\x86\xdd"):
            return None
    elif linktype in (101, 228, 229):  # raw IP
        offset = 0
    else:
        return None
    ip = packet[offset:]
    if len(ip) < 20:
        return None
    if ip[0] >> 4 == 4:
        header_len, total = (ip[0] & 0x0F) * 4, struct.unpack_from(">H", ip, 2)[0]
        if ip[9] != 6 or struct.unpack_from(">H", ip, 6)[0] & 0x3FFF:
            return None  # not TCP, or a fragment
        src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
        tcp = ip[header_len:total]
    elif ip[0] >> 4 == 6 and len(ip) >= 40 and ip[6] == 6:
        src = socket.inet_ntop(socket.AF_INET6, ip[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, ip[24:40])
        tcp = ip[40:40 + struct.unpack_from(">H", ip, 4)[0]]
    else:
        return None
    if len(tcp) < 20:
        return None
    sport, dport, seq = struct.unpack_from(">HHI", tcp)
    data_offset, flags = (tcp[12] >> 4) * 4, tcp[13]
    return src, sport, dst, dport, seq, bool(flags & 0x02), tcp[data_offset:], bool(flags & 0x05)
