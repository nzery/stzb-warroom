"""Remove game credentials from the copy of a game frame queued for upload."""

import hashlib
import json
import re
import zlib

from .frames import CLIENT_HEADER, body, message_id

LOGIN = 99991
UPDATE = 90005
# Login variants, queue handshakes and session notices; account verification,
# migration and safety locks; service tickets, signatures and one-time codes.
PRIVATE_BODIES = {
    23, 90008, 90010, 96665, 96666, 98888, 99992, 99994, 99995, 99999,
    189, 331, 332, 696, 699, 830, 2311, 2540, 2541, 2542, 2543, 4929, 4935,
    4987, 5023, 5258, 40014,
    190, 1431, 1436, 2407, 3826, 3829, 3884, 3886, 3928, 4085, 4969, 4970,
    4971, 6073, 6074, 9008, 20011, 40016, 40021,
}
PRIVATE_COLUMNS = {
    "Tb_underage": {"underage_password"},
    "Tb_user_stuff": {"safe_lock_password"},
    "Tb_user_stuff_one": {"migrate_password", "io_server_ticket", "cloud_music_access_token",
                          "cloud_music_refresh_token"},
    "Tb_user_stuff_temp": {"auth_info"},
    "Tb_user_stuff_temp_ex": {"nile_token"},
}
FILTER_SERVER = PRIVATE_BODIES | {LOGIN, UPDATE}


class UnrecognizedSnapshot(ValueError):
    def __init__(self, version, layout, paths):
        self.version, self.layout, self.paths = version, layout, paths


def integer_paths(paths):
    if (isinstance(paths, list) and len(paths) <= 16
            and all(isinstance(pair, list) and len(pair) == 2 and
                    all(type(i) is int and 0 <= i < 16 for i in pair) for pair in paths)):
        return paths
    return []


_TYPES = {str: "s", int: "i", float: "f", bool: "b", type(None): "n"}


def _shape(value):
    """Keep every array position and object member. Object keys can themselves be private."""
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if isinstance(value, dict):
        return {"object": [_shape(item) for item in value.values()]}
    return _TYPES[type(value)]


def _diagnostic(value, issue, size, encoding):
    tree = _shape(value)
    result = {"captureStructure": 1, "shape": tree, "bytes": size, "encoding": encoding}
    if issue is None:
        return result
    result["format"] = issue.version
    result["cause"] = "width" if issue.layout else "layout"
    snapshot = value[4][0]
    target = tree[4][0]
    target[0] = issue.version
    for index, entry in enumerate(snapshot[1:], 1):
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        name, rows = entry
        if not isinstance(name, str) or not re.fullmatch(r"Tb_[a-zA-Z][a-zA-Z0-9_]{0,95}", name):
            continue
        target[index][0] = name
        rule = (issue.layout or {}).get(name)
        if rule and isinstance(rows, list) and all(isinstance(row, list) and len(row) == rule["width"] for row in rows):
            for row, output in zip(rows, target[index][1]):
                for i in rule["integers"]:
                    if type(row[i]) is int:
                        output[i] = row[i]
    # Only scoped rules for this envelope may restore integer header fields.
    for a, b in issue.paths:
        if (a < len(value) and isinstance(value[a], list) and b < len(value[a])
                and type(value[a][b]) is int):
            tree[a][b] = value[a][b]
    return result


def _encoded(value):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    packed = zlib.compress(raw, 1)
    if isinstance(value, dict) and value.get("captureStructure") == 1:
        # The transport has both an inflated-frame limit and a base64 batch-body limit.
        if len(raw) > 15 * 1024 * 1024 or len(packed) > 2 * 1024 * 1024:
            summary = {key: val for key, val in value.items() if key != "shape"}
            summary["omitted"] = "size"
            if value.get("format"):
                snapshot = value["shape"][4][0]
                summary["tableCount"] = len(snapshot) - 1
                summary["tables"] = [[entry[0], len(entry[1])] for entry in snapshot[1:1025]
                                     if isinstance(entry, list) and len(entry) == 2
                                     and isinstance(entry[0], str) and isinstance(entry[1], list)]
            packed = zlib.compress(json.dumps(summary, separators=(",", ":")).encode(), 1)
    return packed


def valid_layouts(layouts):
    """Check the whole reply before replacing a working cached layout."""
    if not isinstance(layouts, dict) or len(layouts) > 128:
        return False
    for version, tables in layouts.items():
        if not re.fullmatch(r"[0-9a-f]{32}", version) or not isinstance(tables, dict) or not tables:
            return False
        for name, rule in tables.items():
            if not name.startswith("Tb_") or not isinstance(rule, dict):
                return False
            width, keep = rule.get("width"), rule.get("integers")
            if (type(width) is not int or not 0 < width <= 4096 or not isinstance(keep, list)
                    or any(type(i) is not int or not 0 <= i < width for i in keep)):
                return False
    return True


def _compact_row(row, rule):
    if len(row) != rule["width"]:
        raise ValueError("unrecognized protected row")
    clean = [None] * len(row)
    for i in rule["integers"]:
        if type(row[i]) is int:
            clean[i] = row[i]
    row[:] = clean


def _compact_update(op, data, rule):
    if op == 1:
        for row in data if data and isinstance(data[0], list) else [data]:
            _compact_row(row, rule)
        return True
    if op != 2 or len(data) < 2 or 0 not in rule["integers"] or type(data[1]) is not int:
        return False
    fields = [0, data[1]]
    for i in range(2, len(data), 2):
        if type(data[i]) is int and data[i] in rule["integers"] and type(data[i + 1]) is int:
            fields.extend(data[i:i + 2])
    data[:] = fields
    return len(fields) > 2


def _snapshot(value, schema, lookup):
    schema.clear()
    if isinstance(value[4], str) and len(value) > 10 and isinstance(value[10], list):
        # A new role choosing a starting state has no tables.
        return False
    snapshot = value[4][0]
    definition = snapshot[0]
    if re.fullmatch(r"[0-9a-fA-F]{32}", definition):
        layout, paths = lookup(definition.lower()) if lookup else (None, ())
        if not layout or any(not isinstance(row, list) or len(row) != layout[table]["width"]
                             for table, rows in snapshot[1:] if table in layout for row in rows or ()):
            raise UnrecognizedSnapshot(definition.lower(), layout, paths)
        schema.update(layout)
        for table, rows in snapshot[1:]:
            if table in layout:
                for row in rows or ():
                    _compact_row(row, layout[table])
            elif table in PRIVATE_COLUMNS and rows:
                rows.clear()
        return True
    if not definition.startswith("Tb_") or "," not in definition:
        raise ValueError("unrecognized snapshot header")
    for entry in definition.split(";"):
        table, *columns = entry.split(",")
        if table in PRIVATE_COLUMNS:
            schema[table] = {i for i, name in enumerate(columns) if name in PRIVATE_COLUMNS[table]}
    changed = False
    for table, rows in snapshot[1:]:
        if table in PRIVATE_COLUMNS:
            for row in rows or ():
                for i in schema[table]:
                    if i < len(row) and row[i] is not None:
                        row[i] = None
                        changed = True
    return changed


def _updates(value, schema):
    if None in schema:
        value.clear()
        return True
    kept = []
    changed = False
    for change in value:
        op, table, data = change
        if (table in PRIVATE_COLUMNS or table in schema) and op != 3:
            if table not in schema or op not in (1, 2):
                changed = True  # a capture starting mid-session has no column definitions
                continue
            if isinstance(schema[table], dict):
                changed = True
                if not _compact_update(op, data, schema[table]):
                    continue
            elif op == 1:
                for row in data if data and isinstance(data[0], list) else [data]:
                    for i in schema[table]:
                        if i < len(row):
                            row[i] = None
                            changed = True
            else:
                for i in range(2, len(data), 2):
                    if data[i] in schema[table]:
                        data[i + 1] = None
                        changed = True
        kept.append(change)
    value[:] = kept
    return changed


def for_upload(frame, client, schema, lookup=None):
    command = message_id(frame, client)
    if command in PRIVATE_BODIES or (client and command == LOGIN):
        frame = frame[:CLIENT_HEADER if client else 12]
        frame = (len(frame) - 4).to_bytes(4, "big") + frame[4:]
    elif not client and command in (LOGIN, UPDATE):
        value = None
        try:
            value = body(frame, False)[1]
            if command == LOGIN:
                try:
                    changed = _snapshot(value, schema, lookup)
                except (ValueError, TypeError, IndexError, KeyError, AttributeError) as exc:
                    schema.clear()
                    schema[None] = True
                    issue = exc if isinstance(exc, UnrecognizedSnapshot) else None
                    value = _diagnostic(value, issue, len(frame), frame[12] if len(frame) > 12 else None)
                    changed = True
            else:
                changed = _updates(value, schema)
            if changed:
                payload = _encoded(value)
                frame = (len(payload) + 9).to_bytes(4, "big") + frame[4:12] + b"\x03" + payload
        except (ValueError, TypeError, IndexError, KeyError, AttributeError, RecursionError):
            if command == LOGIN:
                schema.clear()
                schema[None] = True
            frame = (8).to_bytes(4, "big") + frame[4:12]
    if client:
        session = frame[12:44]
        digest = hashlib.sha256(b"session:" + session).digest() if session.isalnum() else bytes(32)
        frame = frame[:4] + b"STW-H256" + digest + frame[44:]
    return frame
